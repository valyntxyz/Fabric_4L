from __future__ import annotations

"""Pytest configuration for Layer 4 Agents tests.
"""


import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Path Setup ─────────────────────────────────────────────────────────────
_tests_dir = Path(__file__).parent.resolve()
_layer4_dir = _tests_dir.parent.resolve()
_repo_root = _layer4_dir.parent.parent.resolve()  # layer4-agents -> services -> repo root

# Add repo root to path BEFORE any imports
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Add tests dir so helper modules (e.g. ``_wait_utils``) can be imported by name.
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))

# Add src/ so the canonical ``layer4_agents`` package resolves during collection.
_src_dir = _layer4_dir / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

# Ensure the models packages are present before collection-time tests
# that register targeted models mocks with sys.modules.setdefault().
import layer4_agents.models  # noqa: E402,F401

# Settings are instantiated by several service imports during collection.
# Keep tests hermetic while still allowing callers to provide real endpoints.
os.environ.setdefault("LAYER4_LAYER1_API_URL", "http://localhost:8001")
os.environ.setdefault("LAYER4_LAYER2_API_URL", "http://localhost:8002")
os.environ.setdefault("LAYER4_LAYER3_API_URL", "http://localhost:8003")
os.environ.setdefault("LAYER4_LAYER5_API_URL", "http://localhost:8005")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://localhost:3001")
os.environ.setdefault("NEO4J_PASSWORD", "test-neo4j-password")
# Allow insecure HTTP for test environment (local development only)
os.environ.setdefault("ALLOW_INSECURE_SERVICE_HTTP_IN_DEVELOPMENT", "true")

# ── Stripe Mocking (must happen before any stripe imports) ───────────────────
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake_key_for_testing")

# Mock stripe module before any imports
mock_stripe = MagicMock()
mock_stripe.api_key = "sk_test_fake_key_for_testing"
mock_stripe.error = MagicMock()
mock_stripe.error.StripeError = Exception
mock_stripe.error.SignatureVerificationError = Exception
mock_stripe.Webhook = MagicMock()
mock_stripe.Webhook.construct_event = MagicMock()
mock_stripe.Webhook.verify_header = MagicMock()
mock_stripe.Customer = MagicMock()
mock_stripe.checkout = MagicMock()
mock_stripe.billing_portal = MagicMock()

sys.modules['stripe'] = mock_stripe
sys.modules['stripe._error'] = mock_stripe.error
sys.modules['stripe._webhook'] = mock_stripe.Webhook

# ── PostgreSQL Test Lane ─────────────────────────────────────────────────────
try:
    from testcontainers.postgres import PostgresContainer
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False


def _docker_available() -> bool:
    """Check whether a Docker daemon is reachable from the test environment."""
    try:
        import docker

        docker.from_env().version()
        return True
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()


def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line("markers", "postgres: Tests requiring PostgreSQL (JSONB, RLS, etc.)")
    config.addinivalue_line("markers", "integration: Integration tests requiring external services")
    config.addinivalue_line("markers", "docker: Tests requiring a Docker daemon")


def _testcontainers_required() -> bool:
    """Whether the operator forces the Docker/testcontainers lane (fail closed)."""
    return os.environ.get("LAYER4_REQUIRE_TESTCONTAINERS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def pytest_collection_modifyitems(config, items):
    """Skip postgres/docker tests when their runtime dependencies are unavailable.

    This keeps local ``make test-layer4`` deterministic when Docker is not running
    while still allowing CI to run the full Docker/PostgreSQL integration lane via
    ``make test-layer4-live``.

    The skip is never silent: a config-time warning reports how many gated items
    were skipped, and setting ``LAYER4_REQUIRE_TESTCONTAINERS=1`` fails closed at
    collection when the environment cannot actually run them (VF-SKIP-119/120).
    """
    if _testcontainers_required() and not (POSTGRES_AVAILABLE and DOCKER_AVAILABLE):
        raise pytest.UsageError(
            "LAYER4_REQUIRE_TESTCONTAINERS=1 is set, but testcontainers/Docker are "
            "not available in this environment. Refusing to run with postgres/docker "
            "coverage silently skipped (see VF-SKIP-119/VF-SKIP-120); provide the "
            "runtime dependencies or unset the variable."
        )

    skipped = 0
    if not POSTGRES_AVAILABLE:
        skip_postgres = pytest.mark.skip(reason="testcontainers.postgres not installed - run: pip install testcontainers")
        for item in items:
            if "postgres" in item.keywords:
                item.add_marker(skip_postgres)
                skipped += 1
    elif not DOCKER_AVAILABLE:
        skip_docker = pytest.mark.skip(
            reason="Docker daemon not available locally; run Docker or use CI for postgres/docker tests"
        )
        for item in items:
            if "postgres" in item.keywords or "docker" in item.keywords:
                item.add_marker(skip_docker)
                skipped += 1

    if skipped:
        if not POSTGRES_AVAILABLE:
            coverage_note = "postgres coverage is not exercised in this environment"
        else:
            coverage_note = "postgres/docker coverage is not exercised in this environment"

        config.issue_config_time_warning(
            RuntimeWarning(
                f"Skipped {skipped} gated test item(s); {coverage_note}. Run the "
                "appropriate postgres/docker lane in CI or via `make test-layer4-live`, "
                "or set LAYER4_REQUIRE_TESTCONTAINERS=1 to fail closed instead of "
                "skipping."
            ),
            stacklevel=2,
        )


@pytest.fixture(scope="session")
def postgres_container():
    """Shared PostgreSQL container for all postgres-marked tests."""
    if not POSTGRES_AVAILABLE:
        pytest.skip("testcontainers.postgres not installed")
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker daemon not available; cannot start PostgreSQL testcontainer")

    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres


# ── External Dependency Mocking ───────────────────────────────────────────────
@pytest.fixture(scope="session")
def fake_crm_provider():
    """Provide a fake CRM provider for Salesforce/HubSpot tests."""
    from layer4_agents.models.account import CRMProvider
    
    mock_crm = MagicMock()
    mock_crm.SALESFORCE = CRMProvider.SALESFORCE
    mock_crm.HUBSPOT = CRMProvider.HUBSPOT
    
    # Mock Salesforce OAuth client
    mock_salesforce_client = MagicMock()
    mock_salesforce_client.refresh_token.return_value = {
        "access_token": "fake_access_token",
        "instance_url": "https://test.salesforce.com",
    }
    mock_crm.salesforce_client = mock_salesforce_client
    
    # Mock HubSpot client
    mock_hubspot_client = MagicMock()
    mock_crm.hubspot_client = mock_hubspot_client
    
    yield mock_crm


# MagicMock imported above
from value_fabric.shared.identity.context import RequestContext
from value_fabric.shared.identity.permissions import Role

# ── Shared Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_tenant_context():
    """Fixture that provides a mock RequestContext with tenant context for tests.
    
    This fixture uses RequestContextManager to set a test RequestContext,
    allowing tests that depend on tenant context to run without full auth middleware.
    """
    from value_fabric.shared.identity.context import RequestContextManager
    
    ctx = RequestContext(
        tenant_id="test-tenant-001",
        user_id="test-user-001",
        roles=[Role.TENANT_ADMIN.value]
    )
    with RequestContextManager(ctx):
        yield

# Stub optional heavy deps before any imports that transitively require them

# neo4j — pulled in by layer3-knowledge services; must be stubbed before any
# import that transitively reaches layer3-knowledge.
try:
    import neo4j  # noqa: F401
except ImportError:
    import types as _types
    from importlib.machinery import ModuleSpec
    _neo4j = _types.ModuleType("neo4j")
    _neo4j.__spec__ = ModuleSpec("neo4j", loader=None, is_package=True)
    _neo4j.__path__ = []  # type: ignore[attr-defined]
    _neo4j.AsyncDriver = MagicMock()
    _neo4j.AsyncSession = MagicMock()
    _neo4j.AsyncGraphDatabase = MagicMock()
    _neo4j_exc = _types.ModuleType("neo4j.exceptions")
    _neo4j_exc.__spec__ = ModuleSpec("neo4j.exceptions", loader=None)
    _neo4j_graph = _types.ModuleType("neo4j.graph")
    _neo4j_graph.__spec__ = ModuleSpec("neo4j.graph", loader=None)
    sys.modules["neo4j"] = _neo4j
    sys.modules["neo4j.exceptions"] = _neo4j_exc
    sys.modules["neo4j.graph"] = _neo4j_graph

try:
    import anthropic  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["anthropic"] = _types.ModuleType("anthropic")

# canonical.llm_output_parser and services.llm_output_parser —
# platform-contract package not installed in the test environment; stub both
# so governed_llm_client and signal_detection can be imported without the
# full platform-contract wheel or a services/__init__.py.
try:
    from canonical.llm_output_parser import parse_llm_json  # noqa: F401
except (ImportError, ModuleNotFoundError):
    import json as _json
    import types as _types

    def _parse_llm_json(text: str):  # type: ignore[return]
        try:
            return _json.loads(text)
        except Exception:
            return {}

    _canonical = _types.ModuleType("canonical")
    _canonical.__path__ = []  # type: ignore[attr-defined]
    _canonical_llm = _types.ModuleType("canonical.llm_output_parser")
    _canonical_llm.parse_llm_json = _parse_llm_json  # type: ignore[attr-defined]
    sys.modules["canonical"] = _canonical
    sys.modules["canonical.llm_output_parser"] = _canonical_llm


try:
    import jinja2  # noqa: F401
except ImportError:
    sys.modules["jinja2"] = MagicMock()

try:
    import redis.asyncio  # noqa: F401
except ImportError:
    import types
    _redis = types.ModuleType("redis")
    _redis.asyncio = MagicMock()
    sys.modules["redis"] = _redis
    sys.modules["redis.asyncio"] = _redis.asyncio

try:
    import opentelemetry  # noqa: F401
except ImportError:
    pass

import importlib.util
import types


def _make_pkg(name):
    m = types.ModuleType(name)
    m.__path__ = []
    spec = importlib.util.spec_from_loader(name, loader=None)
    spec.submodule_search_locations = []
    m.__spec__ = spec
    sys.modules[name] = m
    return m

# Idempotently ensure opentelemetry stubs exist (another conftest may have created a partial stub)
_otel = sys.modules.get("opentelemetry") or _make_pkg("opentelemetry")
if not hasattr(_otel, "trace"):
    _otel.trace = _make_pkg("opentelemetry.trace")

_exp = sys.modules.get("opentelemetry.exporter") or _make_pkg("opentelemetry.exporter")
_otlp = sys.modules.get("opentelemetry.exporter.otlp") or _make_pkg("opentelemetry.exporter.otlp")
_proto = sys.modules.get("opentelemetry.exporter.otlp.proto") or _make_pkg("opentelemetry.exporter.otlp.proto")
_http = sys.modules.get("opentelemetry.exporter.otlp.proto.http") or _make_pkg("opentelemetry.exporter.otlp.proto.http")
_txe = sys.modules.get("opentelemetry.exporter.otlp.proto.http.trace_exporter") or _make_pkg("opentelemetry.exporter.otlp.proto.http.trace_exporter")
_txe.OTLPSpanExporter = getattr(_txe, "OTLPSpanExporter", type("OTLPSpanExporter", (), {}))

_inst = sys.modules.get("opentelemetry.instrumentation") or _make_pkg("opentelemetry.instrumentation")
if not hasattr(_inst, "fastapi"):
    _inst.fastapi = _make_pkg("opentelemetry.instrumentation.fastapi")
_inst.fastapi.FastAPIInstrumentor = getattr(_inst.fastapi, "FastAPIInstrumentor", type("FastAPIInstrumentor", (), {}))

_sdk_res = sys.modules.get("opentelemetry.sdk.resources") or _make_pkg("opentelemetry.sdk.resources")
_sdk_res.SERVICE_NAME = getattr(_sdk_res, "SERVICE_NAME", "test")
_sdk_res.Resource = getattr(_sdk_res, "Resource", type("Resource", (), {}))

_sdk_trace = sys.modules.get("opentelemetry.sdk.trace") or _make_pkg("opentelemetry.sdk.trace")
_sdk_trace.TracerProvider = getattr(_sdk_trace, "TracerProvider", type("TracerProvider", (), {}))

_sdk_trace_exp = sys.modules.get("opentelemetry.sdk.trace.export") or _make_pkg("opentelemetry.sdk.trace.export")
_sdk_trace_exp.BatchSpanProcessor = getattr(_sdk_trace_exp, "BatchSpanProcessor", type("BatchSpanProcessor", (), {}))

try:
    import botocore  # noqa: F401
except ImportError:
    import types
    _botocore = types.ModuleType("botocore")
    _botocore.client = types.ModuleType("botocore.client")
    _botocore.client.BaseClient = MagicMock()
    _botocore.config = types.ModuleType("botocore.config")
    _botocore.config.Config = MagicMock()
    sys.modules["botocore"] = _botocore
    sys.modules["botocore.client"] = _botocore.client
    sys.modules["botocore.config"] = _botocore.config

try:
    import langgraph.checkpoint.postgres  # noqa: F401
except ImportError:
    import types
    _lg_pg = types.ModuleType("langgraph.checkpoint.postgres")
    _lg_pg_aio = types.ModuleType("langgraph.checkpoint.postgres.aio")
    _lg_pg_aio.AsyncPostgresSaver = MagicMock()
    _lg_pg.aio = _lg_pg_aio
    sys.modules["langgraph.checkpoint.postgres"] = _lg_pg
    sys.modules["langgraph.checkpoint.postgres.aio"] = _lg_pg_aio

try:
    import openai  # noqa: F401
except ImportError:
    import types
    _openai = types.ModuleType("openai")
    _openai.AsyncOpenAI = MagicMock
    sys.modules["openai"] = _openai


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def inmemory_checkpointer():
    """Provide LangGraph InMemorySaver for fast checkpoint testing."""
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()
    except ImportError:
        pytest.skip("langgraph InMemorySaver not available")


@pytest.fixture(scope="session")
def redis_container():
    """Provide Redis container for integration tests (requires Docker)."""
    try:
        from testcontainers.redis import RedisContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        with RedisContainer("redis:7-alpine") as redis:
            yield redis.get_connection_url()
    except Exception as e:
        pytest.fail(f"Redis container failed to start: {e}")


# ── Workflow Test Fixtures ─────────────────────────────────────────────────
# These fixtures reduce boilerplate in workflow tests by providing
# pre-configured mocks for common dependencies.

from unittest.mock import AsyncMock, MagicMock, Mock


@pytest.fixture
def mock_tool_registry():
    """Create a mocked ToolRegistry for workflow testing.

    Returns a Mock spec'd to ToolRegistry with execute method as AsyncMock.
    Usage:
        def test_workflow(self, mock_tool_registry):
            mock_tool_registry.execute.return_value = {"result": "mocked"}
            workflow = BusinessCaseGeneratorWorkflow(tool_registry=mock_tool_registry)
    """
    from layer4_agents.tools.registry import ToolRegistry
    registry = Mock(spec=ToolRegistry)
    registry.execute = AsyncMock()
    return registry


@pytest.fixture
def mock_openai_response():
    """Factory fixture for creating mock AsyncOpenAI responses."""
    def _make_response(content: str = "Mock LLM content") -> MagicMock:
        """Create a mock AsyncOpenAI chat completion response."""
        mock_choice = MagicMock()
        mock_choice.message.content = content
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        return mock_response
    return _make_response


@pytest.fixture
def mock_openai_client(mock_openai_response):
    """Create a mocked AsyncOpenAI client for workflow testing.

    Returns a mock client with chat.completions.create as AsyncMock.
    Usage:
        def test_workflow(self, mock_openai_client):
            mock_openai_client.chat.completions.create.return_value = mock_openai_response("content")
            workflow = MyWorkflow(openai_client=mock_openai_client)
    """
    client = Mock()
    client.chat = Mock()
    client.chat.completions = Mock()
    client.chat.completions.create = AsyncMock()
    client.chat.completions.create.return_value = mock_openai_response("Mock LLM content")
    return client


@pytest.fixture
def business_case_workflow(mock_tool_registry, mock_openai_client):
    """Create a BusinessCaseGeneratorWorkflow with mocked dependencies.

    This fixture provides a workflow instance ready for testing with
    all external calls (LLM, tools) pre-mocked.
    """
    from layer4_agents.workflows.business_case import BusinessCaseGeneratorWorkflow
    return BusinessCaseGeneratorWorkflow(
        tool_registry=mock_tool_registry,
        openai_client=mock_openai_client,
    )


@pytest.fixture
def roi_calculator_workflow(mock_tool_registry):
    """Create a ROICalculatorWorkflow with mocked dependencies."""
    from layer4_agents.workflows.roi_calculator import ROICalculatorWorkflow
    return ROICalculatorWorkflow(
        tool_registry=mock_tool_registry,
    )


# ── Checkpoint/Resume Test Fixtures ─────────────────────────────────────────
# These fixtures reduce duplication in test_checkpoint_resume.py

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver

from layer4_agents.engine.executor import OrchestrationController
from layer4_agents.engine.state_manager import StateManager
from layer4_agents.models.agent_state import BaseAgentState, WorkflowStatus

TEST_WORKFLOW_TYPE = "roi_calculator"


class MockCheckpointSaver(InMemorySaver):
    """Mock checkpoint saver extending InMemorySaver for testing.

    InMemorySaver provides full BaseCheckpointSaver implementation
    with in-memory storage - perfect for testing without Postgres.
    """

    @property
    def checkpoints(self) -> dict[str, Any]:
        """Expose underlying storage for test assertions."""
        return getattr(self, 'storage', {})

    @property
    def saved_threads(self) -> set:
        """Expose saved thread IDs for test assertions."""
        return set(self.checkpoints.keys())


@pytest.fixture
def mock_checkpoint_saver() -> MockCheckpointSaver:
    """Provide mock checkpoint saver."""
    return MockCheckpointSaver()


@pytest.fixture
def state_manager() -> StateManager:
    """Provide fresh StateManager instance."""
    return StateManager()


@pytest_asyncio.fixture
async def orchestrator_with_checkpoint(
    mock_tool_registry: Mock,
    mock_checkpoint_saver: MockCheckpointSaver
) -> OrchestrationController:
    """Provide OrchestrationController with checkpointing enabled."""
    state_manager = StateManager()
    controller = OrchestrationController(
        tool_registry=mock_tool_registry,
        state_manager=state_manager,
        checkpoint_saver=mock_checkpoint_saver
    )
    try:
        await controller.start()
        yield controller
    finally:
        await controller.stop()


@pytest.fixture
def controller_with_running_state(
    mock_tool_registry: Mock,
    mock_checkpoint_saver: MockCheckpointSaver,
    state_manager: StateManager
) -> tuple[OrchestrationController, str, BaseAgentState]:
    """Provide controller with pre-existing running workflow state.

    Returns:
        Tuple of (controller, workflow_id, existing_state)
    """
    workflow_id = "test-resume-wf"
    existing_state = BaseAgentState(tenant_id="test-tenant", 
        workflow_id=workflow_id,
        workflow_type=TEST_WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        current_node="middle",
        input_data={"test": "data"},
        output_data={"start": {"status": "completed"}},
        errors=[]
    )

    controller = OrchestrationController(
        tool_registry=mock_tool_registry,
        state_manager=state_manager,
        checkpoint_saver=mock_checkpoint_saver
    )
    controller._workflow_metadata[workflow_id] = {
        "workflow_type": TEST_WORKFLOW_TYPE,
        "started_at": datetime.now(UTC).isoformat()
    }

    return controller, workflow_id, existing_state


@pytest.fixture
def controller_with_paused_state(
    mock_tool_registry: Mock,
    mock_checkpoint_saver: MockCheckpointSaver,
    state_manager: StateManager
) -> tuple[OrchestrationController, str, BaseAgentState]:
    """Provide controller with pre-existing paused workflow state.

    Returns:
        Tuple of (controller, workflow_id, initial_state)
    """
    workflow_id = "lifecycle-wf"
    initial_state = BaseAgentState(tenant_id="test-tenant", 
        workflow_id=workflow_id,
        workflow_type=TEST_WORKFLOW_TYPE,
        status=WorkflowStatus.PAUSED,
        current_node="middle",
        input_data={"test": "lifecycle"},
        output_data={"start": {"status": "completed"}},
        errors=[]
    )

    controller = OrchestrationController(
        tool_registry=mock_tool_registry,
        state_manager=state_manager,
        checkpoint_saver=mock_checkpoint_saver
    )
    controller._workflow_metadata[workflow_id] = {
        "workflow_type": TEST_WORKFLOW_TYPE,
        "started_at": datetime.now(UTC).isoformat()
    }

    return controller, workflow_id, initial_state


@pytest.fixture
def completed_workflow_state() -> BaseAgentState:
    """Provide a completed workflow state fixture."""
    return BaseAgentState(tenant_id="test-tenant", 
        workflow_id="completed-wf",
        workflow_type=TEST_WORKFLOW_TYPE,
        status=WorkflowStatus.COMPLETED,
        input_data={},
        output_data={},
        errors=[]
    )


# ── SimpleTestWorkflow Fixture ─────────────────────────────────────────────
# Extracted from test_checkpoint_resume.py to reduce duplication

from value_fabric.shared.models.typed_dict import TypedDictModel

from layer4_agents.models.workflow_config import EdgeConfig, NodeConfig, NodeType
from layer4_agents.workflows.base import BaseWorkflow, WorkflowConfig


class SimpleTestWorkflow__execute_toolResult(TypedDictModel):
    node: Any
    status: str
    tool: Any | None = None


class SimpleTestWorkflow(BaseWorkflow):
    """Simple workflow for testing checkpoint/resume.

    This workflow tracks node execution in self.executed_nodes and can
    optionally pause after a specified node for testing resume scenarios.
    """

    def __init__(self, tool_registry, checkpoint_saver=None, pause_after_node: str | None = None):
        """Initialize with optional pause point."""
        config = WorkflowConfig(
            workflow_type=TEST_WORKFLOW_TYPE,
            name="Test Workflow",
            description="Simple workflow for testing",
            nodes=[
                NodeConfig(id="start", name="Start", node_type=NodeType.TOOL, tool_name="test_tool"),
                NodeConfig(id="middle", name="Middle", node_type=NodeType.TOOL, tool_name="test_tool"),
                NodeConfig(id="end", name="End", node_type=NodeType.END),
            ],
            edges=[
                EdgeConfig(source="start", target="middle"),
                EdgeConfig(source="middle", target="end"),
            ],
            entry_point="start"
        )
        super().__init__(config, tool_registry, checkpoint_saver)
        self.pause_after_node = pause_after_node
        self.executed_nodes: list = []

    async def _execute_tool(self, tool_name: str, state, config: dict) -> dict[str, Any]:
        """Track node execution."""
        current_node = state.current_node
        self.executed_nodes.append(current_node)

        # Simulate pause after specified node
        if self.pause_after_node and current_node == self.pause_after_node:
            state.status = WorkflowStatus.PENDING
            return SimpleTestWorkflow__execute_toolResult.model_validate({"status": "paused", "node": current_node})

        return SimpleTestWorkflow__execute_toolResult.model_validate({"status": "completed", "node": current_node, "tool": tool_name})

    def create_initial_state(self, input_data: dict[str, Any], *, tenant_id: str | None = None):
        """Create initial state."""
        return BaseAgentState(tenant_id=tenant_id or "test-tenant",
            workflow_id=input_data.get("workflow_id", f"test-{datetime.now(UTC).timestamp()}"),
            workflow_type=TEST_WORKFLOW_TYPE,
            status=WorkflowStatus.PENDING,
            input_data=input_data,
            output_data={},
            errors=[]
        )


@pytest.fixture
def simple_test_workflow(mock_tool_registry, mock_checkpoint_saver):
    """Provide SimpleTestWorkflow instance for checkpoint/resume testing.

    Returns a factory function that accepts optional checkpoint_saver and pause_after_node parameters.
    Usage:
        workflow = simple_test_workflow(pause_after_node="middle")
        workflow = simple_test_workflow(checkpoint_saver=None)
    """
    def _make_workflow(checkpoint_saver=None, pause_after_node: str | None = None):
        if checkpoint_saver is None:
            checkpoint_saver = mock_checkpoint_saver
        return SimpleTestWorkflow(mock_tool_registry, checkpoint_saver, pause_after_node)
    return _make_workflow


try:
    from tests.utils.workflow_helpers import setup_workflow_metadata  # noqa: F401
except (ImportError, ModuleNotFoundError):
    import importlib.util
    from pathlib import Path
    _helper_path = Path(__file__).resolve().parent / "utils" / "workflow_helpers.py"
    if _helper_path.exists():
        _spec = importlib.util.spec_from_file_location("layer4_test_workflow_helpers", _helper_path)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        setup_workflow_metadata = _mod.setup_workflow_metadata
    else:
        raise
