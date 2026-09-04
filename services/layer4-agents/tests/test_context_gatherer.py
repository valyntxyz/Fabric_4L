from __future__ import annotations

import pytest

from layer4_agents.services.context_gatherer import ContextGatheringService
from layer4_agents.services.tenant_cypher import TenantCypherValidationError, fetch_tenant_validated_records
from layer4_agents.services.tenant_query_helper import run_tenant_validated_query


class _FakeResult:
    def __init__(self, records):
        self._records = records
        self._idx = 0

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._records):
            raise StopAsyncIteration
        value = self._records[self._idx]
        self._idx += 1
        return value


class _FakeSession:
    def __init__(self, records, calls):
        self._records = records
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, params):
        self._calls.append({"query": query, "params": params})
        return _FakeResult(self._records)


class _FakeDriver:
    def __init__(self, records):
        self.calls = []
        self._records = records

    def session(self):
        return _FakeSession(self._records, self.calls)


class _SingleAccessRecord(dict):
    def __init__(self, payload: dict):
        super().__init__({"hypothesis": payload})
        self.hypothesis_get_count = 0

    def get(self, key, default=None):
        if key == "hypothesis":
            self.hypothesis_get_count += 1
        return super().get(key, default)


@pytest.mark.asyncio
async def test_get_account_hypotheses_maps_fields_once_without_duplicates():
    payload = {
        "id": "h1",
        "hypothesis_text": "Reduce cycle time",
        "status": "draft",
        "confidence_score": 0.72,
        "value_path_category": "efficiency",
        "estimated_impact_usd": 240000,
        "capability_name": "Workflow automation",
        "signal_name": "Backlog growth",
    }
    record = _SingleAccessRecord(payload)
    service = ContextGatheringService(neo4j_driver=_FakeDriver([record]))

    result = await service._get_account_hypotheses("acct-1", "tenant-a")

    assert result == [
        {
            "id": "h1",
            "text": "Reduce cycle time",
            "status": "draft",
            "confidence": 0.72,
            "value_path": "efficiency",
            "estimated_impact_usd": 240000,
            "capability": "Workflow automation",
            "signal": "Backlog growth",
        }
    ]
    assert record.hypothesis_get_count == 1


@pytest.mark.asyncio
async def test_get_account_hypotheses_uses_authenticated_tenant_context():
    driver = _FakeDriver([])
    service = ContextGatheringService(neo4j_driver=driver)

    hypotheses = await service._get_account_hypotheses("acct-1", "tenant-a")

    assert hypotheses == []
    assert driver.calls
    assert driver.calls[0]["params"]["tenant_id"] == "tenant-a"
    assert driver.calls[0]["params"]["account_id"] == "acct-1"


@pytest.mark.asyncio
async def test_run_tenant_validated_query_rejects_mismatched_tenant_context():
    driver = _FakeDriver([])

    with pytest.raises(ValueError, match="Tenant context mismatch"):
        await run_tenant_validated_query(
            driver=driver,
            query="RETURN 1",
            tenant_id="tenant-a",
            params={"tenant_id": "tenant-b"},
        )

    assert driver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "query", "params"),
    [
        (
            "context_gatherer.account_signals",
            """
            MATCH (s:Signal {tenant_id: $tenant_id})
            WHERE s.account_id = $account_id OR s.target_account_id = $account_id
            RETURN s {.*} AS signal
            """,
            {"tenant_id": "tenant-b", "account_id": "acct-1"},
        ),
        (
            "context_gatherer.account_hypotheses",
            """
            MATCH (vh:ValueHypothesis {tenant_id: $tenant_id, account_id: $account_id})
            RETURN vh {.*} AS hypothesis
            """,
            {"tenant_id": "tenant-b", "account_id": "acct-1"},
        ),
    ],
)
async def test_context_queries_reject_mismatched_tenant_context(
    operation: str,
    query: str,
    params: dict[str, Any],
) -> None:
    session = _FakeSession({"tenant-b": [{"signal": {"id": "sig-b"}}]}, [])

    with pytest.raises(TenantCypherValidationError):
        await fetch_tenant_validated_records(
            driver=_FakeDriver({"tenant-b": [{"signal": {"id": "sig-b"}}]}),
            query=query,
            params=params,
            tenant_id="tenant-a",
            operation=operation,
        )

    assert session._calls == []
