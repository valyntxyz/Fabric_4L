"""Root conftest.py — Production Approval Suite shared fixtures.

This module provides the canonical fixture set for all tenant isolation,
security, state consistency, and integration tests.  Every fixture uses
deterministic, seeded values so tests are reproducible across environments.

Design Rules:
    1. Every test that touches persistence must seed mixed-tenant data in the
       same database, graph, or cache instance.
    2. Tests must never rely solely on route-level filtering — they must also
       verify the persistence layer.
    3. In CI (``CI=true``), missing infrastructure is a hard failure.
       Locally, tests that need unavailable infra are skipped.
    4. JWT tokens are signed with the same secret used by GovernanceMiddleware
       in test mode so ``TestClient`` requests pass authentication.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from uuid import UUID

import jwt as pyjwt
import pytest


@dataclass(frozen=True)
class InfraDependency:
    """Dependency metadata used for skip/fail messaging and reporting."""

    key: str
    display_name: str
    startup_command: str
    categories: tuple[str, ...]


INFRA_DEPENDENCIES: dict[str, InfraDependency] = {
    "postgres": InfraDependency(
        key="postgres",
        display_name="Postgres",
        startup_command="docker compose up -d postgres",
        categories=(
            "RLS and tenant-isolation persistence tests",
            "DB-backed integration contracts",
            "cross-tenant state consistency checks",
        ),
    ),
    "redis": InfraDependency(
        key="redis",
        display_name="Redis",
        startup_command="docker compose up -d redis",
        categories=(
            "cache isolation and cache consistency tests",
            "rate-limit and shared-state controls",
            "state lifecycle integration checks",
        ),
    ),
    "neo4j": InfraDependency(
        key="neo4j",
        display_name="Neo4j",
        startup_command="docker compose up -d neo4j",
        categories=(
            "graph traversal and retrieval contracts",
            "knowledge graph tenant-boundary tests",
            "graph-backed integration workflows",
        ),
    ),
    "docker": InfraDependency(
        key="docker",
        display_name="Docker",
        startup_command="Start Docker Desktop",
        categories=(
            "testcontainers-based integration tests",
            "containerized service tests",
        ),
    ),
}


def make_infra_skip_reason(dependency_key: str) -> str:
    """Return a standardized high-visibility reason for infra-gated skips."""
    dep = INFRA_DEPENDENCIES[dependency_key]
    categories = "; ".join(dep.categories)
    return (
        f"[INFRA_GATE:{dep.display_name.upper()}] {dep.display_name} is unavailable locally. "
        f"Start it with: `{dep.startup_command}`. "
        f"Affected categories: {categories}."
    )


def make_infra_ci_failure_reason(dependency_key: str) -> str:
    """Return a standardized reason for CI hard failures."""
    dep = INFRA_DEPENDENCIES[dependency_key]
    return (
        f"[INFRA_GATE:{dep.display_name.upper()}] {dep.display_name} is required in CI but unreachable. "
        f"Start it with: `{dep.startup_command}`."
    )


# ---------------------------------------------------------------------------
# Deterministic tenant / user identifiers
# ---------------------------------------------------------------------------
TENANT_A_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SYSTEM_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")

USER_A_ID = "user-alpha-001"
USER_B_ID = "user-bravo-002"
ADMIN_USER_ID = "admin-super-001"

# JWT secret shared with GovernanceMiddleware in test/dev mode
TEST_JWT_SECRET = os.getenv("JWT_SECRET", os.getenv("TEST_JWT_SECRET", "test-secret-key"))
TEST_JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

_DEBUG_VALUE = os.getenv("DEBUG", "").strip().lower()
if _DEBUG_VALUE and _DEBUG_VALUE not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
    os.environ["DEBUG"] = "false"

# Claim names matching GovernanceMiddleware defaults
JWT_TENANT_CLAIM = os.getenv("JWT_TENANT_CLAIM", "tenant_id")
JWT_USER_CLAIM = os.getenv("JWT_USER_CLAIM", "sub")
JWT_ROLES_CLAIM = os.getenv("JWT_ROLES_CLAIM", "roles")


# ---------------------------------------------------------------------------
# Ensure services packages are importable
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PATHS_TO_ADD = [
    str(_PROJECT_ROOT),
    # NOTE: Do NOT add packages/shared/src here — it shadows stdlib `secrets`.
    # The shared package is importable as `value_fabric.shared.identity`, etc.
    # via the namespace package setup.
    str(_PROJECT_ROOT / "services" / "layer4-agents" / "src"),
    str(_PROJECT_ROOT / "services" / "layer1-ingestion" / "src"),
    str(_PROJECT_ROOT / "packages" / "platform-contract" / "src" / "python"),
    # Layer 3 must be inserted last (so it ends up first on sys.path) to prevent
    # layer4 modules (e.g. config package) from shadowing layer3 top-level modules.
    str(_PROJECT_ROOT / "services" / "layer3-knowledge" / "src"),
]
for p in _PATHS_TO_ADD:
    if p not in sys.path:
        sys.path.insert(0, p)


def _prepend_namespace_path(module: types.ModuleType, paths: list[Path]) -> None:
    namespace_paths = list(getattr(module, "__path__", []))
    requested_paths = [str(path) for path in paths]
    namespace_paths = [path for path in namespace_paths if path not in requested_paths]
    for path in reversed(requested_paths):
        namespace_paths.insert(0, path)
    module.__path__ = namespace_paths  # type: ignore[attr-defined]


def _ensure_namespace_module(name: str, paths: list[Path]) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    _prepend_namespace_path(module, paths)
    return module


def _install_legacy_collection_import_aliases() -> None:
    """Stabilize legacy test import paths during repo-wide collection.

    Several historical Layer 3 tests still import bare ``src.*`` modules. Keep
    those imports collection-compatible without changing runtime
    source-of-truth modules.

    Layer 4 contributes nothing here: its legacy ``src.*`` compatibility tree
    and the ``services.layer4_agents.src.*`` alias were retired with the shim
    deletion (see ADR-048); Layer 4 tests import the canonical
    ``layer4_agents.*`` package directly.
    """
    layer3_src = _PROJECT_ROOT / "services" / "layer3-knowledge" / "src"
    layer4_src = _PROJECT_ROOT / "services" / "layer4-agents" / "src"

    src_module = _ensure_namespace_module("src", [layer3_src])
    src_agents_module = _ensure_namespace_module("src.agents", [layer3_src / "agents"])
    src_api_module = _ensure_namespace_module("src.api", [layer3_src / "api"])
    src_analytics_module = _ensure_namespace_module("src.analytics", [layer3_src / "analytics"])
    src_models_module = _ensure_namespace_module("src.models", [layer3_src / "models"])
    src_retrieval_module = _ensure_namespace_module("src.retrieval", [layer3_src / "retrieval"])
    src_services_module = _ensure_namespace_module("src.services", [layer3_src / "services"])
    src_module.agents = src_agents_module
    src_module.api = src_api_module
    src_module.analytics = src_analytics_module
    src_module.models = src_models_module
    src_module.retrieval = src_retrieval_module
    src_module.services = src_services_module

    layer4_agents_module = _ensure_namespace_module("layer4_agents", [layer4_src / "layer4_agents"])
    _ = layer4_agents_module  # noqa: F841 - registered for canonical test imports

    services_module = _ensure_namespace_module("services", [_PROJECT_ROOT / "services"])
    _ = services_module  # noqa: F841 - registered for cross-service test imports

    # Layer 3 still has runtime modules that use top-level ``from config``.
    # Pin that bare name to the Layer 3 config surface so later sys.path
    # mutations from service-specific tests cannot redirect it to Layer 4.
    config_init = layer3_src / "config" / "__init__.py"
    config_spec = importlib.util.spec_from_file_location(
        "src.config",
        config_init,
        submodule_search_locations=[str(layer3_src / "config")],
    )
    if config_spec is None or config_spec.loader is None:
        raise RuntimeError(f"Cannot load Layer 3 config compatibility module from {config_init}")
    layer3_config = importlib.util.module_from_spec(config_spec)
    sys.modules["src.config"] = layer3_config
    config_spec.loader.exec_module(layer3_config)
    sys.modules["config"] = layer3_config
    src_module.config = layer3_config

    if "value_fabric.shared.testing" not in sys.modules:
        try:
            import value_fabric.shared.testing.governance as _testing_governance  # noqa: F401
        except Exception:
            pass
        testing_module = sys.modules.setdefault(
            "value_fabric.shared.testing",
            types.ModuleType("value_fabric.shared.testing"),
        )
        if not hasattr(testing_module, "mark"):
            testing_module.mark = pytest.mark


_install_legacy_collection_import_aliases()


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> None:
    _install_legacy_collection_import_aliases()
    return None


# ---------------------------------------------------------------------------
# JWT Encoding
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def jwt_encode() -> Callable[..., str]:
    """Return a function that creates signed JWTs matching GovernanceMiddleware.

    Usage::

        token = jwt_encode(
            tenant_id=TENANT_A_ID,
            user_id=USER_A_ID,
            roles=["analyst"],
        )
    """

    def _encode(
        tenant_id: UUID,
        *,
        user_id: str | None = None,
        roles: list[str] | None = None,
        api_key_id: str | None = None,
        extra_claims: dict[str, Any] | None = None,
        expires_in: int = 3600,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            JWT_TENANT_CLAIM: str(tenant_id),
            "iat": now,
            "exp": now + expires_in,
        }
        if user_id is not None:
            payload[JWT_USER_CLAIM] = user_id
        if roles is not None:
            payload[JWT_ROLES_CLAIM] = roles
        if api_key_id is not None:
            payload["api_key_id"] = api_key_id
        if extra_claims:
            payload.update(extra_claims)
        return pyjwt.encode(payload, TEST_JWT_SECRET, algorithm=TEST_JWT_ALGORITHM)

    return _encode


# ---------------------------------------------------------------------------
# Tenant identity fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tenant_a_id() -> UUID:
    """Deterministic UUID for Tenant A."""
    return TENANT_A_ID


@pytest.fixture
def tenant_b_id() -> UUID:
    """Deterministic UUID for Tenant B."""
    return TENANT_B_ID


@pytest.fixture
def user_a_id() -> str:
    return USER_A_ID


@pytest.fixture
def user_b_id() -> str:
    return USER_B_ID


@pytest.fixture
def admin_user_id() -> str:
    return ADMIN_USER_ID


# ---------------------------------------------------------------------------
# JWT Token fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def jwt_token_a(jwt_encode) -> str:
    """Signed JWT for Tenant A / analyst role."""
    return jwt_encode(TENANT_A_ID, user_id=USER_A_ID, roles=["analyst"])


@pytest.fixture
def jwt_token_b(jwt_encode) -> str:
    """Signed JWT for Tenant B / analyst role."""
    return jwt_encode(TENANT_B_ID, user_id=USER_B_ID, roles=["analyst"])


@pytest.fixture
def jwt_token_admin(jwt_encode) -> str:
    """Signed JWT for super_admin (Tenant A context)."""
    return jwt_encode(TENANT_A_ID, user_id=ADMIN_USER_ID, roles=["super_admin"])


@pytest.fixture
def jwt_token_expired(jwt_encode) -> str:
    """JWT that expired 1 hour ago."""
    return jwt_encode(TENANT_A_ID, user_id=USER_A_ID, roles=["analyst"], expires_in=-3600)


@pytest.fixture
def jwt_token_no_tenant() -> str:
    """JWT with no tenant_id claim — must be rejected."""
    now = int(time.time())
    payload = {
        JWT_USER_CLAIM: USER_A_ID,
        JWT_ROLES_CLAIM: ["analyst"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, TEST_JWT_SECRET, algorithm=TEST_JWT_ALGORITHM)


@pytest.fixture
def jwt_token_malformed_tenant() -> str:
    """JWT with invalid (non-UUID) tenant_id — must be rejected."""
    now = int(time.time())
    payload = {
        JWT_TENANT_CLAIM: "not-a-uuid",
        JWT_USER_CLAIM: USER_A_ID,
        JWT_ROLES_CLAIM: ["analyst"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, TEST_JWT_SECRET, algorithm=TEST_JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# RequestContext fixtures (for unit tests that bypass HTTP)
# ---------------------------------------------------------------------------
@pytest.fixture
def context_a():
    """RequestContext for Tenant A — use with RequestContextManager."""
    from value_fabric.shared.identity.context import RequestContext
    from value_fabric.shared.identity.permissions import Role, get_role_permissions

    return RequestContext(
        tenant_id=TENANT_A_ID,
        user_id=USER_A_ID,
        roles=["analyst"],
        permissions=get_role_permissions(Role.ANALYST),
        source="jwt",
    )


@pytest.fixture
def context_b():
    """RequestContext for Tenant B — use with RequestContextManager."""
    from value_fabric.shared.identity.context import RequestContext
    from value_fabric.shared.identity.permissions import Role, get_role_permissions

    return RequestContext(
        tenant_id=TENANT_B_ID,
        user_id=USER_B_ID,
        roles=["analyst"],
        permissions=get_role_permissions(Role.ANALYST),
        source="jwt",
    )


@pytest.fixture
def context_admin():
    """RequestContext for super_admin."""
    from value_fabric.shared.identity.context import RequestContext
    from value_fabric.shared.identity.permissions import Role, get_role_permissions

    return RequestContext(
        tenant_id=TENANT_A_ID,
        user_id=ADMIN_USER_ID,
        roles=["super_admin"],
        permissions=get_role_permissions(Role.SUPER_ADMIN),
        source="jwt",
    )


# ---------------------------------------------------------------------------
# Auth header helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def auth_headers_a(jwt_token_a) -> dict[str, str]:
    """HTTP headers authenticating as Tenant A."""
    return {"Authorization": f"Bearer {jwt_token_a}"}


@pytest.fixture
def auth_headers_b(jwt_token_b) -> dict[str, str]:
    """HTTP headers authenticating as Tenant B."""
    return {"Authorization": f"Bearer {jwt_token_b}"}


@pytest.fixture
def auth_headers_admin(jwt_token_admin) -> dict[str, str]:
    """HTTP headers authenticating as super_admin."""
    return {"Authorization": f"Bearer {jwt_token_admin}"}


# ---------------------------------------------------------------------------
# Infrastructure availability helpers
# ---------------------------------------------------------------------------
def _check_postgres() -> bool:
    """Return True if test PostgreSQL is reachable."""
    try:
        import psycopg2
        db_url = os.getenv(
            "TEST_DATABASE_URL",
            "postgresql://localhost:5432/test_value_fabric",
        )
        conn = psycopg2.connect(db_url)
        conn.close()
        return True
    except Exception:
        return False


def _check_redis() -> bool:
    """Return True if test Redis is reachable."""
    try:
        import redis
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = redis.Redis(host=host, port=port, db=0)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def _check_neo4j() -> bool:
    """Return True if test Neo4j is reachable."""
    try:
        from neo4j import GraphDatabase
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "password")
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
        driver.close()
        return True
    except Exception:
        return False


def _check_docker() -> bool:
    """Return True if Docker daemon is available."""
    try:
        import docker
        client = docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def require_postgres():
    """Skip (local) or fail (CI) if PostgreSQL is unavailable."""
    if _check_postgres():
        return True
    if os.getenv("CI") == "true":
        pytest.fail(make_infra_ci_failure_reason("postgres"))
    pytest.skip(make_infra_skip_reason("postgres"))


@pytest.fixture(scope="session")
def require_redis():
    """Skip (local) or fail (CI) if Redis is unavailable."""
    if _check_redis():
        return True
    if os.getenv("CI") == "true":
        pytest.fail(make_infra_ci_failure_reason("redis"))
    pytest.skip(make_infra_skip_reason("redis"))


@pytest.fixture(scope="session")
def require_neo4j():
    """Skip (local) or fail (CI) if Neo4j is unavailable."""
    if _check_neo4j():
        return True
    if os.getenv("CI") == "true":
        pytest.fail(make_infra_ci_failure_reason("neo4j"))
    pytest.skip(make_infra_skip_reason("neo4j"))


@pytest.fixture(scope="session")
def require_docker():
    """Skip (local) or fail (CI) if Docker daemon is unavailable."""
    if _check_docker():
        return True
    if os.getenv("CI") == "true":
        pytest.fail(make_infra_ci_failure_reason("docker"))
    pytest.skip(make_infra_skip_reason("docker"))


# ---------------------------------------------------------------------------
# Module-level skip helper
# ---------------------------------------------------------------------------

def skip_if_infra_unavailable(*deps: str) -> None:
    """Module-level guard: skip entire module if listed deps are unreachable.

    Usage at top of a test module::
        skip_if_infra_unavailable("postgres", "neo4j")
    """
    for dep in deps:
        checker = {
            "postgres": _check_postgres,
            "redis": _check_redis,
            "neo4j": _check_neo4j,
            "docker": _check_docker,
        }.get(dep)
        if checker is None:
            continue
        if not checker():
            if os.getenv("CI") == "true":
                pytest.fail(make_infra_ci_failure_reason(dep))
            pytest.skip(make_infra_skip_reason(dep), allow_module_level=True)


# ---------------------------------------------------------------------------
# Database fixtures (require live PostgreSQL)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def postgres_test_db():
    """PostgreSQL test database for JSONB/RLS/security tests.

    Uses TEST_DATABASE_URL or defaults to docker-compose dev stack.
    Fails in CI if unavailable, skips locally with clear message.
    """
    if not _check_postgres():
        if os.getenv("CI") == "true":
            pytest.fail(make_infra_ci_failure_reason("postgres"))
        pytest.skip(make_infra_skip_reason("postgres"))

    from sqlalchemy import create_engine

    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/fabric_test",
    )
    engine = create_engine(db_url)

    # Verify dialect
    if engine.dialect.name != "postgresql":
        pytest.fail(
            f"PostgreSQL test configured with {engine.dialect.name} dialect. "
            f"Set TEST_DATABASE_URL to a PostgreSQL connection string."
        )

    yield engine
    engine.dispose()


@pytest.fixture
def db_connection(require_postgres):
    """Raw psycopg2 connection for RLS policy testing."""
    import psycopg2
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://localhost:5432/test_value_fabric",
    )
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def db_session_for_tenant(require_postgres):
    """Factory fixture: returns a function that creates a DB session scoped to a tenant.

    Usage::

        session = db_session_for_tenant(TENANT_A_ID)
        # session has SET LOCAL app.tenant_id = '<tenant_a_uuid>'
    """
    import psycopg2
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://localhost:5432/test_value_fabric",
    )

    connections = []

    def _create(tenant_id: UUID):
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SET LOCAL app.tenant_id = %s", (str(tenant_id),))
        connections.append(conn)
        return conn

    yield _create

    for conn in connections:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Redis fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def redis_client(require_redis):
    """Redis client for cache / rate-limit / state isolation testing."""
    import redis
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    db = int(os.getenv("REDIS_DB", "0"))
    client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
    client.ping()
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Codebase introspection helpers (for contract tests)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the repository root."""
    return _PROJECT_ROOT


@pytest.fixture(scope="session")
def all_route_files(project_root) -> list[Path]:
    """All Python route files across all layers."""
    return sorted(project_root.glob("services/**/api/routes/*.py"))


@pytest.fixture(scope="session")
def l4_route_files(project_root) -> list[Path]:
    """L4 route files only (highest risk for tenant bypass)."""
    return sorted(
        (project_root / "services" / "layer4-agents" / "src" / "layer4_agents" / "api" / "routes").glob(
            "*.py"
        )
    )


# ---------------------------------------------------------------------------
# Terminal summary reporting (infra-gated skips)
# ---------------------------------------------------------------------------
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Report infra-gated skip counts so coverage loss is explicit."""
    skipped = terminalreporter.stats.get("skipped", [])
    counts = dict.fromkeys(INFRA_DEPENDENCIES, 0)

    for report in skipped:
        longrepr = getattr(report, "longrepr", "")
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) >= 3 else str(longrepr)
        for key, dep in INFRA_DEPENDENCIES.items():
            token = f"[INFRA_GATE:{dep.display_name.upper()}]"
            if token in reason:
                counts[key] += 1

    total = sum(counts.values())
    if total == 0:
        return

    terminalreporter.section("infra-gated skip coverage", sep="-")
    for key, dep in INFRA_DEPENDENCIES.items():
        terminalreporter.write_line(f"{dep.display_name}: {counts[key]} skipped tests")
    terminalreporter.write_line(f"Total infra-gated skips: {total}")


@pytest.fixture
def deterministic_jwt_secret(monkeypatch):
    """Test-only deterministic JWT secret fixture."""
    secret = "test-jwt-secret-00000000000000000000000000000000"
    monkeypatch.setenv("JWT_SECRET", secret)
    return secret
