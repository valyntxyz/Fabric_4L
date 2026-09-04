from __future__ import annotations

"""Unit tests for Layer3Client.get_benchmark_variables() and
Layer3Client.get_value_driver_formulas().

These methods replace the Cypher-in-L4 pattern from workflows/queries.py.
Tests verify:
- Correct URL construction
- Correct query parameter encoding
- Tenant header propagation
- ValueError on empty driver_ids
- _make_request result is passed through unchanged
"""


import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_L4_SRC = Path(__file__).parents[1] / "src"
if str(_L4_SRC) not in sys.path:
    sys.path.insert(0, str(_L4_SRC))

from layer4_agents.integration.layer3_client import Layer3Client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = "http://layer3-knowledge:8000"
_TENANT = "tenant-test-001"


def _make_client() -> Layer3Client:
    return Layer3Client(base_url=_BASE, tenant_id=_TENANT)


# ---------------------------------------------------------------------------
# get_benchmark_variables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_benchmark_variables_calls_correct_url():
    """Calls /v1/knowledge/benchmarks/variables with industry param."""
    client = _make_client()
    expected_payload = {"industry": "SaaS", "variables": {}, "defaults": {}}

    with patch.object(client, "_make_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = expected_payload
        result = await client.get_benchmark_variables(industry="SaaS")

    mock_req.assert_called_once_with(
        "GET",
        f"{_BASE}/v1/knowledge/benchmarks/variables",
        _TENANT,
        params={"industry": "SaaS"},
    )
    assert result == expected_payload


@pytest.mark.asyncio
async def test_get_benchmark_variables_uses_provided_tenant_id():
    """Explicit tenant_id overrides the client default."""
    client = _make_client()
    override_tenant = "tenant-override"

    with patch.object(client, "_make_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {}
        await client.get_benchmark_variables(industry="Healthcare", tenant_id=override_tenant)

    _, _, called_tenant, *_ = mock_req.call_args.args
    assert called_tenant == override_tenant


# ---------------------------------------------------------------------------
# get_value_driver_formulas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_value_driver_formulas_calls_correct_url():
    """Calls /v1/knowledge/value-drivers/formulas with repeated driver_ids params."""
    client = _make_client()
    driver_ids = ["vd-1", "vd-2"]
    expected_payload = {"drivers": [], "missing_ids": []}

    with patch.object(client, "_make_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = expected_payload
        result = await client.get_value_driver_formulas(driver_ids=driver_ids)

    mock_req.assert_called_once_with(
        "GET",
        f"{_BASE}/v1/knowledge/value-drivers/formulas",
        _TENANT,
        params=[("driver_ids", "vd-1"), ("driver_ids", "vd-2")],
    )
    assert result == expected_payload


@pytest.mark.asyncio
async def test_get_value_driver_formulas_empty_list_raises_value_error():
    """Empty driver_ids raises ValueError before any HTTP call."""
    client = _make_client()

    with patch.object(client, "_make_request", new_callable=AsyncMock) as mock_req:
        with pytest.raises(ValueError, match="non-empty"):
            await client.get_value_driver_formulas(driver_ids=[])

    mock_req.assert_not_called()


@pytest.mark.asyncio
async def test_get_value_driver_formulas_uses_provided_tenant_id():
    """Explicit tenant_id overrides the client default."""
    client = _make_client()
    override_tenant = "tenant-override"

    with patch.object(client, "_make_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {}
        await client.get_value_driver_formulas(
            driver_ids=["vd-1"], tenant_id=override_tenant
        )

    _, _, called_tenant, *_ = mock_req.call_args.args
    assert called_tenant == override_tenant


@pytest.mark.asyncio
async def test_get_value_driver_formulas_single_id():
    """Single driver_id produces a single-element params list."""
    client = _make_client()

    with patch.object(client, "_make_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"drivers": [], "missing_ids": ["vd-only"]}
        await client.get_value_driver_formulas(driver_ids=["vd-only"])

    # _make_request("GET", url, tenant, params=[...]) — params is a keyword arg
    params = mock_req.call_args.kwargs.get("params")
    assert params == [("driver_ids", "vd-only")]
