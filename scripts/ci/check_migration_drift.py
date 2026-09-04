#!/usr/bin/env python3
"""Validate Alembic migrations against PostgreSQL and SQLAlchemy metadata.

The gate creates one disposable PostgreSQL database per Alembic-managed service,
runs migrations to head, optionally exercises a one-revision downgrade/upgrade
round trip, and compares the resulting live schema with each service's
SQLAlchemy metadata via Alembic autogenerate.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclasses.dataclass(frozen=True)
class ServiceMigrationCheck:
    name: str
    service_dir: Path
    config_path: Path
    metadata_module: str | None
    metadata_attr: str
    env_urls: tuple[str, ...]
    async_env_urls: tuple[str, ...] = ()
    supports_one_step_downgrade: bool = True
    rollback_doc_anchor: str | None = None


SERVICES: tuple[ServiceMigrationCheck, ...] = (
    ServiceMigrationCheck(
        name="layer1-ingestion",
        service_dir=Path("services/layer1-ingestion"),
        config_path=Path("alembic.ini"),
        metadata_module="src.shared.models",
        metadata_attr="Base.metadata",
        env_urls=("DATABASE_URL",),
    ),
    ServiceMigrationCheck(
        name="layer2-extraction",
        service_dir=Path("services/layer2-extraction"),
        config_path=Path("alembic.ini"),
        metadata_module="layer2_extraction.db.models",
        metadata_attr="Base.metadata",
        env_urls=(),
        async_env_urls=("DATABASE_URL",),
    ),
    ServiceMigrationCheck(
        name="layer2-5-signal-refinery",
        service_dir=Path("services/layer2-5-signal-refinery"),
        config_path=Path("alembic.ini"),
        metadata_module="layer2_5_signal_refinery.models.db_models",
        metadata_attr="Base.metadata",
        env_urls=(),
        async_env_urls=("DATABASE_URL",),
    ),
    ServiceMigrationCheck(
        name="layer4-agents",
        service_dir=Path("services/layer4-agents"),
        config_path=Path("alembic.ini"),
        metadata_module="layer4_agents.database",
        metadata_attr="Base.metadata",
        env_urls=("LAYER4_DATABASE_URL", "CHECKPOINT_DATABASE_URL"),
    ),
    ServiceMigrationCheck(
        name="layer5-ground-truth",
        service_dir=Path("services/layer5-ground-truth"),
        config_path=Path("alembic.ini"),
        metadata_module="layer5_ground_truth.models",
        metadata_attr="Base.metadata",
        env_urls=("DATABASE_URL_SYNC",),
    ),
    ServiceMigrationCheck(
        name="api",
        service_dir=Path("services/api/migrations"),
        config_path=Path("alembic.ini"),
        metadata_module=None,
        metadata_attr="target_metadata",
        env_urls=("DATABASE_URL", "API_DATABASE_URL"),
        supports_one_step_downgrade=True,
        rollback_doc_anchor="api-gateway-sql-migrations",
    ),
)

IGNORED_TABLES = {"alembic_version"}


class MigrationDriftError(RuntimeError):
    """Raised when migration validation fails."""


def _make_url(raw_url: str):
    from sqlalchemy.engine import make_url

    return make_url(raw_url)


def _postgres_sync_url(raw_url: str) -> str:
    url = _make_url(raw_url)
    drivername = url.drivername
    if drivername.startswith("postgresql+"):
        drivername = "postgresql+psycopg2"
    elif drivername == "postgresql":
        drivername = "postgresql+psycopg2"
    else:
        raise MigrationDriftError(f"Expected a PostgreSQL URL, got driver {url.drivername!r}")
    return url.set(drivername=drivername).render_as_string(hide_password=False)


def _postgres_async_url(raw_url: str) -> str:
    url = _make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise MigrationDriftError(f"Expected a PostgreSQL URL, got driver {url.drivername!r}")
    return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def _url_for_database(raw_url: str, database: str) -> str:
    return _make_url(raw_url).set(database=database).render_as_string(hide_password=False)


def _database_name(raw_url: str) -> str:
    database = _make_url(raw_url).database
    if not database:
        raise MigrationDriftError("PostgreSQL URL must include a maintenance database name")
    return database


def _temporary_database_name(service: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", service.lower()).strip("_")
    suffix = uuid.uuid4().hex[:10]
    return f"vf_migration_{slug}_{suffix}"[:63]


def _create_database(admin_url: str, database_name: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(_postgres_sync_url(admin_url), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine.dispose()


def _drop_database(admin_url: str, database_name: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(_postgres_sync_url(admin_url), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database AND pid <> pg_backend_pid()"
            ),
            {"database": database_name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    engine.dispose()


def _service_env(service: ServiceMigrationCheck, sync_url: str, async_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("ENVIRONMENT", "test")
    env.setdefault("APP_ENV", "test")
    env.setdefault("JWT_SECRET", "migration-drift-check-only")
    for key in service.env_urls:
        env[key] = sync_url
    for key in service.async_env_urls:
        env[key] = async_url
    return env


def _run_alembic(service: ServiceMigrationCheck, args: tuple[str, ...], env: dict[str, str]) -> None:
    cmd = ("alembic", "-c", str(service.config_path), *args)
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT / service.service_dir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise MigrationDriftError(
            f"{service.name}: {' '.join(cmd)} failed with exit {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def _import_metadata(service: ServiceMigrationCheck) -> Any | None:
    if service.metadata_module is None:
        return None

    service_root = REPO_ROOT / service.service_dir
    paths = [service_root, service_root / "src", REPO_ROOT / "packages" / "shared" / "src"]
    inserted: list[str] = []
    for path in paths:
        as_str = str(path)
        if path.exists() and as_str not in sys.path:
            sys.path.insert(0, as_str)
            inserted.append(as_str)

    with _isolated_modules(service.metadata_module):
        module = __import__(service.metadata_module, fromlist=["*"])
        value: Any = module
        for attr in service.metadata_attr.split("."):
            value = getattr(value, attr)

    for path in reversed(inserted):
        with contextlib.suppress(ValueError):
            sys.path.remove(path)

    from sqlalchemy import MetaData

    if not isinstance(value, MetaData):
        raise MigrationDriftError(
            f"{service.name}: {service.metadata_module}.{service.metadata_attr} did not resolve to MetaData"
        )
    return value


@contextlib.contextmanager
def _isolated_modules(module_name: str) -> Iterator[None]:
    """Remove a service module after metadata import to avoid cross-service collisions."""
    before = set(sys.modules)
    yield
    for loaded in set(sys.modules) - before:
        if loaded == module_name or loaded.startswith(module_name.split(".")[0] + "."):
            sys.modules.pop(loaded, None)


def _include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    del obj, reflected, compare_to
    if type_ == "table" and name in IGNORED_TABLES:
        return False
    return True


def _format_diff(diff: Any) -> str:
    return repr(diff)


def _compare_metadata(service: ServiceMigrationCheck, sync_url: str) -> list[str]:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    metadata = _import_metadata(service)
    if metadata is None:
        print(f"{service.name}: metadata comparison skipped (migration root is SQL-managed)")
        return []

    engine = create_engine(_postgres_sync_url(sync_url))
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "target_metadata": metadata,
                "compare_type": True,
                "compare_server_default": True,
                "include_object": _include_object,
            },
        )
        diffs = compare_metadata(context, metadata)
    engine.dispose()
    return [_format_diff(diff) for diff in diffs]


def _validate_service(
    service: ServiceMigrationCheck,
    admin_url: str,
    *,
    round_trip: bool,
    keep_databases: bool,
) -> None:
    temp_db = _temporary_database_name(service.name)
    sync_url = _url_for_database(_postgres_sync_url(admin_url), temp_db)
    async_url = _url_for_database(_postgres_async_url(admin_url), temp_db)
    print(f"\n==> {service.name}: creating disposable PostgreSQL database {temp_db}")
    _create_database(admin_url, temp_db)
    try:
        env = _service_env(service, sync_url, async_url)
        print(f"{service.name}: alembic upgrade head")
        _run_alembic(service, ("upgrade", "head"), env)

        if round_trip:
            if service.supports_one_step_downgrade:
                print(f"{service.name}: alembic downgrade -1")
                _run_alembic(service, ("downgrade", "-1"), env)
                print(f"{service.name}: alembic upgrade head after downgrade")
                _run_alembic(service, ("upgrade", "head"), env)
            else:
                print(
                    f"{service.name}: one-step downgrade is intentionally unsupported; "
                    f"see rollback strategy #{service.rollback_doc_anchor}"
                )

        print(f"{service.name}: comparing SQLAlchemy metadata to migrated schema")
        diffs = _compare_metadata(service, sync_url)
        if diffs:
            formatted = "\n".join(f"  - {diff}" for diff in diffs[:50])
            raise MigrationDriftError(f"{service.name}: metadata/schema drift detected:\n{formatted}")
        print(f"{service.name}: PASS")
    finally:
        if keep_databases:
            print(f"{service.name}: keeping disposable database {temp_db}")
        else:
            _drop_database(admin_url, temp_db)


def _selected_services(names: list[str] | None) -> tuple[ServiceMigrationCheck, ...]:
    if not names:
        return SERVICES
    requested = set(names)
    unknown = requested - {service.name for service in SERVICES}
    if unknown:
        raise MigrationDriftError(f"Unknown service(s): {', '.join(sorted(unknown))}")
    return tuple(service for service in SERVICES if service.name in requested)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MIGRATION_DRIFT_DATABASE_URL") or os.environ.get("DB_MIGRATION_DATABASE_URL"),
        help="PostgreSQL maintenance URL used to create disposable per-service databases.",
    )
    parser.add_argument("--service", action="append", help="Limit validation to a service name; may be repeated.")
    parser.add_argument("--round-trip", action="store_true", help="Run upgrade head, downgrade -1, then upgrade head.")
    parser.add_argument("--keep-databases", action="store_true", help="Keep disposable databases for debugging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.database_url:
        print(
            "ERROR: set MIGRATION_DRIFT_DATABASE_URL or pass --database-url with a PostgreSQL maintenance URL.",
            file=sys.stderr,
        )
        return 2

    try:
        _database_name(args.database_url)
        for service in _selected_services(args.service):
            _validate_service(
                service,
                args.database_url,
                round_trip=args.round_trip,
                keep_databases=args.keep_databases,
            )
    except MigrationDriftError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("\nAll PostgreSQL migration drift checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
