#!/usr/bin/env python3
"""Report database migration status and fail on migration drift.

The status mode is read-only: it inspects migration files, Alembic version
tables, SQLAlchemy metadata diffs, rollback governance, and tenant/RLS posture.
It never runs Alembic upgrade/downgrade or creates/drops databases.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import os
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "database"
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/valuefabric"


@dataclasses.dataclass(frozen=True)
class MigrationService:
    name: str
    service_dir: Path
    config_path: Path | None
    versions_dir: Path
    metadata_module: str | None = None
    metadata_attr: str = "Base.metadata"
    default_database: str | None = None
    env_urls: tuple[str, ...] = ()
    async_env_urls: tuple[str, ...] = ()
    file_managed: bool = False


ALEMBIC_SERVICES: tuple[MigrationService, ...] = (
    MigrationService(
        name="layer1-ingestion",
        service_dir=Path("services/layer1-ingestion"),
        config_path=Path("alembic.ini"),
        versions_dir=Path("migrations/versions"),
        metadata_module="src.shared.models",
        default_database="ingestion",
        env_urls=("LAYER1_DATABASE_URL_SYNC", "LAYER1_DATABASE_URL", "DATABASE_URL"),
    ),
    MigrationService(
        name="layer2-extraction",
        service_dir=Path("services/layer2-extraction"),
        config_path=Path("alembic.ini"),
        versions_dir=Path("migrations/versions"),
        metadata_module="layer2_extraction.db.models",
        default_database="extraction",
        env_urls=("LAYER2_DATABASE_URL_SYNC",),
        async_env_urls=("LAYER2_DATABASE_URL", "DATABASE_URL"),
    ),
    MigrationService(
        name="layer2-5-signal-refinery",
        service_dir=Path("services/layer2-5-signal-refinery"),
        config_path=Path("alembic.ini"),
        versions_dir=Path("src/layer2_5_signal_refinery/migrations/versions"),
        metadata_module="layer2_5_signal_refinery.models.db_models",
        default_database="signal_refinery",
        env_urls=("LAYER2_5_DATABASE_URL_SYNC",),
        async_env_urls=("LAYER2_5_DATABASE_URL", "DATABASE_URL"),
    ),
    MigrationService(
        name="layer4-agents",
        service_dir=Path("services/layer4-agents"),
        config_path=Path("alembic.ini"),
        versions_dir=Path("migrations/versions"),
        metadata_module="layer4_agents.database",
        default_database="layer4_agents",
        env_urls=("LAYER4_DATABASE_URL_SYNC", "LAYER4_DATABASE_URL", "CHECKPOINT_DATABASE_URL"),
    ),
    MigrationService(
        name="layer5-ground-truth",
        service_dir=Path("services/layer5-ground-truth"),
        config_path=Path("alembic.ini"),
        versions_dir=Path("src/layer5_ground_truth/migrations/versions"),
        metadata_module="layer5_ground_truth.models",
        default_database="ground_truth",
        env_urls=("LAYER5_DATABASE_URL_SYNC", "DATABASE_URL_SYNC", "DATABASE_URL"),
    ),
    MigrationService(
        name="api",
        service_dir=Path("services/api/migrations"),
        config_path=Path("alembic.ini"),
        versions_dir=Path("versions"),
        metadata_module=None,
        default_database="valuefabric",
        env_urls=("API_DATABASE_URL", "DATABASE_URL"),
    ),
)

FILE_MANAGED_SERVICES: tuple[MigrationService, ...] = (
    MigrationService(
        name="layer3-knowledge",
        service_dir=Path("services/layer3-knowledge"),
        config_path=None,
        versions_dir=Path("src/migrations"),
        file_managed=True,
    ),
    MigrationService(
        name="layer6-benchmarks",
        service_dir=Path("services/layer6-benchmarks"),
        config_path=None,
        versions_dir=Path("migrations/versions"),
        file_managed=True,
    ),
)


class MigrationStatusError(RuntimeError):
    """Raised for expected migration-report failures."""


def _json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def _literal_assignment(tree: ast.Module, name: str) -> object | None:
    for node in tree.body:
        value: ast.expr | None = None
        matched = False
        if isinstance(node, ast.Assign):
            matched = any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            matched = isinstance(node.target, ast.Name) and node.target.id == name
            value = node.value
        if matched and value is not None:
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError):
                return None
    return None


def _normalize_revisions(value: object | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (tuple, list)):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def extract_revision_graph(versions_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    revisions: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    revision_files: dict[str, list[str]] = defaultdict(list)
    files = sorted(path for path in versions_dir.glob("*.py") if not path.name.startswith("__"))
    if not files:
        for path in sorted(p for p in versions_dir.iterdir() if p.is_file() and not p.name.startswith((".", "__"))):
            revisions[path.stem] = {"revision": path.stem, "down_revision": (), "file": path.name}
        return revisions, errors

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(REPO_ROOT)}: syntax error: {exc}")
            continue
        revision = _literal_assignment(tree, "revision")
        down_revision = _literal_assignment(tree, "down_revision")
        if not isinstance(revision, str) or not revision:
            errors.append(f"{path.relative_to(REPO_ROOT)}: missing literal revision")
            continue
        revisions[revision] = {
            "revision": revision,
            "down_revision": _normalize_revisions(down_revision),
            "file": path.name,
        }
        revision_files[revision].append(path.name)

    for revision, names in revision_files.items():
        if len(names) > 1:
            errors.append(f"duplicate revision ID {revision!r} in files: {names}")
    for revision, info in revisions.items():
        for parent in info["down_revision"]:
            if parent not in revisions:
                errors.append(f"{revision}: down_revision {parent!r} is not present in migration files")
    return revisions, errors


def graph_heads(revisions: dict[str, dict[str, Any]]) -> list[str]:
    parents = {parent for info in revisions.values() for parent in info["down_revision"]}
    return sorted(revision for revision in revisions if revision not in parents)


def applied_history(revisions: dict[str, dict[str, Any]], current: str | None) -> list[str]:
    if not current or current not in revisions:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def visit(revision: str) -> None:
        if revision in seen or revision not in revisions:
            return
        for parent in revisions[revision]["down_revision"]:
            visit(parent)
        seen.add(revision)
        out.append(revision)

    visit(current)
    return out


def pending_revisions(revisions: dict[str, dict[str, Any]], current: str | None) -> list[str]:
    if not revisions:
        return []
    if current is None:
        return sorted(revisions)
    if current not in revisions:
        return []

    children: dict[str, list[str]] = defaultdict(list)
    for revision, info in revisions.items():
        for parent in info["down_revision"]:
            children[parent].append(revision)

    pending: list[str] = []
    queue: deque[str] = deque(sorted(children[current]))
    seen = {current}
    while queue:
        revision = queue.popleft()
        if revision in seen:
            continue
        seen.add(revision)
        pending.append(revision)
        queue.extend(sorted(children[revision]))
    return pending


def _make_url(raw_url: str):
    from sqlalchemy.engine import make_url

    return make_url(raw_url)


def _sync_url(raw_url: str) -> str:
    url = _make_url(raw_url)
    if url.drivername.startswith("postgresql+"):
        return url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)
    if url.drivername in {"postgresql", "postgres"}:
        return url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)
    return raw_url


def _url_for_database(raw_url: str, database: str | None) -> str:
    if not database:
        return raw_url
    return _make_url(raw_url).set(database=database).render_as_string(hide_password=False)


def _service_database_url(service: MigrationService, base_url: str) -> str:
    for key in (*service.env_urls, *service.async_env_urls):
        value = os.environ.get(key)
        if value:
            return _sync_url(value)
    return _sync_url(_url_for_database(base_url, service.default_database))


def read_db_revision(database_url: str) -> tuple[str | None, str | None]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar()
            if not exists:
                return None, "alembic_version table is missing"
            rows = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all()
            if not rows:
                return None, "alembic_version table is empty"
            if len(rows) > 1:
                return str(rows[-1]), f"multiple alembic_version rows found: {rows}"
            return str(rows[0]), None
    except SQLAlchemyError as exc:
        return None, f"database unavailable: {exc}"
    finally:
        engine.dispose()


def compare_metadata(service: MigrationService, database_url: str) -> tuple[list[str], str | None]:
    if service.metadata_module is None:
        return [], "metadata comparison skipped for SQL-managed migration root"
    try:
        from scripts.ci.check_migration_drift import _compare_metadata

        return _compare_metadata(
            dataclasses.replace(
                service,  # type: ignore[arg-type]
                config_path=service.config_path or Path("alembic.ini"),
            ),
            database_url,
        ), None
    except TypeError:
        # The imported helper expects its own dataclass type.
        try:
            from scripts.ci.check_migration_drift import ServiceMigrationCheck, _compare_metadata

            drift_service = ServiceMigrationCheck(
                name=service.name,
                service_dir=service.service_dir,
                config_path=service.config_path or Path("alembic.ini"),
                metadata_module=service.metadata_module,
                metadata_attr=service.metadata_attr,
                env_urls=service.env_urls,
                async_env_urls=service.async_env_urls,
            )
            return _compare_metadata(drift_service, database_url), None
        except Exception as exc:  # noqa: BLE001 - report, do not hide comparison failure
            return [], f"metadata comparison failed: {exc}"
    except Exception as exc:  # noqa: BLE001 - report, do not hide comparison failure
        return [], f"metadata comparison failed: {exc}"


def validate_tenant_rls(database_url: str) -> tuple[dict[str, Any], list[str]]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            tenant_tables = conn.execute(
                text(
                    """
                    SELECT table_schema, table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND column_name IN ('tenant_id', 'organization_id')
                    ORDER BY table_name, column_name
                    """
                )
            ).mappings().all()
            table_names = sorted({row["table_name"] for row in tenant_tables})
            if not table_names:
                return {"tenant_scoped_tables": [], "policy_count": 0}, []

            table_state = conn.execute(
                text(
                    """
                    SELECT c.relname AS table_name, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'r'
                      AND c.relname = ANY(:tables)
                    """
                ),
                {"tables": table_names},
            ).mappings().all()
            policies = conn.execute(
                text(
                    """
                    SELECT tablename, policyname, qual, with_check
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = ANY(:tables)
                    ORDER BY tablename, policyname
                    """
                ),
                {"tables": table_names},
            ).mappings().all()
    except SQLAlchemyError as exc:
        return {"tenant_scoped_tables": [], "policy_count": 0}, [f"tenant/RLS validation unavailable: {exc}"]
    finally:
        engine.dispose()

    columns_by_table: dict[str, list[str]] = defaultdict(list)
    for row in tenant_tables:
        columns_by_table[row["table_name"]].append(row["column_name"])
    state_by_table = {row["table_name"]: row for row in table_state}
    policies_by_table: dict[str, list[dict[str, str | None]]] = defaultdict(list)
    for policy in policies:
        policies_by_table[policy["tablename"]].append(
            {
                "name": policy["policyname"],
                "qual": policy["qual"],
                "with_check": policy["with_check"],
            }
        )

    failures: list[str] = []
    table_reports: list[dict[str, Any]] = []
    for table in table_names:
        state = state_by_table.get(table)
        table_policies = policies_by_table.get(table, [])
        policy_text = "\n".join(
            str(policy.get("qual") or "") + "\n" + str(policy.get("with_check") or "")
            for policy in table_policies
        )
        enabled = bool(state and state["relrowsecurity"])
        forced = bool(state and state["relforcerowsecurity"])
        uses_tenant_guc = "app.tenant_id" in policy_text
        if not enabled:
            failures.append(f"{table}: tenant-scoped table does not have RLS enabled")
        if not forced:
            failures.append(f"{table}: tenant-scoped table does not FORCE ROW LEVEL SECURITY")
        if not table_policies:
            failures.append(f"{table}: tenant-scoped table has no RLS policies")
        elif not uses_tenant_guc:
            failures.append(f"{table}: RLS policies do not reference app.tenant_id")
        table_reports.append(
            {
                "table": table,
                "columns": sorted(columns_by_table[table]),
                "rls_enabled": enabled,
                "rls_forced": forced,
                "policy_count": len(table_policies),
                "uses_app_tenant_id": uses_tenant_guc,
            }
        )
    return {"tenant_scoped_tables": table_reports, "policy_count": len(policies)}, failures


def rollback_policy_status() -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "scripts/ci/check_migration_rollback_policy.py"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "status": "pass" if result.returncode == 0 else "fail",
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def build_report(
    *,
    mode: str,
    database_url: str,
    allow_database_unavailable: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": mode,
        "read_only": True,
        "database_url_source": "redacted",
        "services": [],
        "file_managed_services": [],
        "rollback_metadata": rollback_policy_status(),
        "failures": [],
        "warnings": [],
    }

    rollback = report["rollback_metadata"]
    if rollback["status"] != "pass":
        report["failures"].append("rollback metadata policy failed")

    for service in ALEMBIC_SERVICES:
        versions_dir = REPO_ROOT / service.service_dir / service.versions_dir
        revisions, graph_errors = extract_revision_graph(versions_dir)
        heads = graph_heads(revisions)
        service_url = _service_database_url(service, database_url)
        current, db_warning = read_db_revision(service_url)
        metadata_diffs: list[str] = []
        metadata_warning: str | None = None
        rls_report: dict[str, Any] = {"tenant_scoped_tables": [], "policy_count": 0}
        rls_failures: list[str] = []

        if db_warning and db_warning.startswith("database unavailable"):
            if allow_database_unavailable:
                report["warnings"].append(f"{service.name}: {db_warning}")
            else:
                report["failures"].append(f"{service.name}: {db_warning}")
        elif db_warning:
            report["warnings"].append(f"{service.name}: {db_warning}")

        if current is not None:
            metadata_diffs, metadata_warning = compare_metadata(service, service_url)
            rls_report, rls_failures = validate_tenant_rls(service_url)

        service_failures: list[str] = []
        if graph_errors:
            service_failures.extend(graph_errors)
        if len(heads) != 1:
            service_failures.append(f"expected exactly one filesystem head, found {len(heads)}")
        if current and current not in revisions:
            service_failures.append(f"database revision {current!r} is not present in migration files")
        if metadata_diffs:
            service_failures.append("metadata/schema drift detected")
        if metadata_warning:
            report["warnings"].append(f"{service.name}: {metadata_warning}")
        service_failures.extend(rls_failures)
        report["failures"].extend(f"{service.name}: {failure}" for failure in service_failures)

        report["services"].append(
            {
                "name": service.name,
                "type": "alembic",
                "versions_dir": str((service.service_dir / service.versions_dir).as_posix()),
                "filesystem_heads": heads,
                "current_db_revision": current,
                "pending_migrations": pending_revisions(revisions, current),
                "applied_migration_history": applied_history(revisions, current),
                "unknown_db_revision": bool(current and current not in revisions),
                "graph_errors": graph_errors,
                "metadata_drift": metadata_diffs,
                "tenant_rls": rls_report,
                "failures": service_failures,
            }
        )

    for service in FILE_MANAGED_SERVICES:
        versions_dir = REPO_ROOT / service.service_dir / service.versions_dir
        files = []
        if versions_dir.exists():
            files = sorted(
                path.name for path in versions_dir.iterdir() if path.is_file() and not path.name.startswith(".")
            )
        else:
            report["failures"].append(f"{service.name}: file-managed migration directory missing")
        report["file_managed_services"].append(
            {
                "name": service.name,
                "type": "file-managed",
                "versions_dir": str((service.service_dir / service.versions_dir).as_posix()),
                "migration_files": files,
                "file_count": len(files),
                "status": "present" if files else "missing",
            }
        )
        if not files:
            report["failures"].append(f"{service.name}: no file-managed migrations found")

    report["status"] = "fail" if report["failures"] else "pass"
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Database Migration Status",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Read only: `{str(report['read_only']).lower()}`",
        f"- Status: `{report['status']}`",
        "",
        "## Alembic Services",
        "",
    ]
    for service in report["services"]:
        lines.extend(
            [
                f"### {service['name']}",
                "",
                f"- Filesystem head: `{', '.join(service['filesystem_heads']) or 'none'}`",
                f"- Current DB revision: `{service['current_db_revision'] or 'none'}`",
                f"- Pending migrations: `{len(service['pending_migrations'])}`",
                f"- Applied migration history: `{len(service['applied_migration_history'])}` revisions",
                f"- Metadata drift findings: `{len(service['metadata_drift'])}`",
                f"- Tenant/RLS checked tables: `{len(service['tenant_rls']['tenant_scoped_tables'])}`",
                f"- Failures: `{len(service['failures'])}`",
                "",
            ]
        )
    lines.extend(["## File-Managed Migration Areas", ""])
    for service in report["file_managed_services"]:
        lines.extend(
            [
                f"### {service['name']}",
                "",
                f"- Migration files: `{service['file_count']}`",
                f"- Status: `{service['status']}`",
                "",
            ]
        )
    lines.extend(["## Rollback Metadata", ""])
    rollback = report["rollback_metadata"]
    lines.append(f"- Status: `{rollback['status']}`")
    if rollback["stderr"]:
        lines.append(f"- Error: `{rollback['stderr']}`")
    lines.extend(["", "## Failures", ""])
    if report["failures"]:
        lines.extend(f"- {failure}" for failure in report["failures"])
    else:
        lines.append("- None")
    lines.extend(["", "## Warnings", ""])
    if report["warnings"]:
        lines.extend(f"- {warning}" for warning in report["warnings"])
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "migration-status.json"
    md_path = output_dir / "migration-status.md"
    json_path.write_text(json.dumps(report, indent=2, default=_json_safe) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("status", "check"), default="status")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DB_MIGRATION_STATUS_DATABASE_URL")
        or os.environ.get("DB_MIGRATION_DATABASE_URL")
        or DEFAULT_DATABASE_URL,
        help="PostgreSQL URL used for read-only migration inspection.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("DB_MIGRATION_STATUS_ARTIFACT_DIR") or str(DEFAULT_OUTPUT_DIR),
        help="Directory for Markdown and JSON artifacts.",
    )
    parser.add_argument(
        "--allow-database-unavailable",
        action="store_true",
        help="Write artifacts and warn instead of failing when the active DB cannot be reached.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        mode=args.mode,
        database_url=args.database_url,
        allow_database_unavailable=args.allow_database_unavailable,
    )
    json_path, md_path = write_artifacts(report, Path(args.output_dir))
    def display(path: Path) -> str:
        try:
            return str(path.relative_to(REPO_ROOT))
        except ValueError:
            return str(path)

    print(f"Migration status JSON: {display(json_path)}")
    print(f"Migration status Markdown: {display(md_path)}")
    if args.mode == "check" and report["failures"]:
        print("Migration check failed:")
        for failure in report["failures"]:
            print(f" - {failure}")
        return 1
    print(f"Migration {args.mode} status: {report['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
