from __future__ import annotations

import pytest

from layer4_agents.integration.layer1_client import Layer1ClientError, Layer1IngestionClient
from layer4_agents.integration.layer2_client import Layer2ClientError, Layer2ExtractionClient
from layer4_agents.integration.layer5_client import Layer5GroundTruthClient


@pytest.mark.asyncio
async def test_layer1_create_website_target_requires_tenant(monkeypatch):
    client = Layer1IngestionClient(base_url="http://example.test")

    async def _fail(*args, **kwargs):  # pragma: no cover
        raise AssertionError("outbound call should not run")

    monkeypatch.setattr(client.client, "post", _fail)

    with pytest.raises(Layer1ClientError, match="Missing tenant context"):
        await client.create_website_target(url="https://example.com")

    await client.close()


@pytest.mark.asyncio
async def test_layer2_extract_value_attributes_propagates_tenant(monkeypatch):
    client = Layer2ExtractionClient(base_url="http://example.test", tenant_id="tenant-123")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    captured = {}

    async def _post(url, json, headers):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(client.client, "post", _post)

    result = await client.extract_value_attributes(
        content_id="content-1",
        source_url="https://example.com",
        markdown_content="hello",
    )

    assert result["ok"] is True
    assert captured["headers"]["X-Tenant-ID"] == "tenant-123"

    await client.close()


@pytest.mark.asyncio
async def test_layer2_extract_value_attributes_blocks_without_tenant(monkeypatch):
    client = Layer2ExtractionClient(base_url="http://example.test")

    async def _fail(*args, **kwargs):  # pragma: no cover
        raise AssertionError("outbound call should not run")

    monkeypatch.setattr(client.client, "post", _fail)

    with pytest.raises(Layer2ClientError, match="Missing tenant context"):
        await client.extract_value_attributes(
            content_id="content-1",
            source_url="https://example.com",
            markdown_content="hello",
        )

    await client.close()


@pytest.mark.asyncio
async def test_layer5_list_truths_requires_tenant_and_allows_privileged_with_reason(monkeypatch):
    client = Layer5GroundTruthClient(base_url="http://example.test", service_token="token")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [], "total": 0}

    captured = {}

    async def _get(url, params):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(client._client, "get", _get)

    blocked = await client.list_truths()
    assert "Missing tenant context" in blocked["error"]

    ok = await client.list_truths(allow_system_call=True, audit_reason="nightly-reconciliation")
    assert ok["items"] == []
    assert "organization_id" not in captured["params"]

    await client.close()


@pytest.mark.asyncio
async def test_layer5_tenant_fallback_includes_service_auth_secret(monkeypatch):
    monkeypatch.setenv("SERVICE_AUTH_SECRET", "service-auth-secret-minimum-32-bytes!!")

    client = Layer5GroundTruthClient(
        base_url="http://example.test",
        tenant_id="12345678-1234-1234-1234-123456789abc",
    )

    assert client._client.headers["X-Tenant-ID"] == "12345678-1234-1234-1234-123456789abc"
    assert client._client.headers["X-Service-Auth"] == "service-auth-secret-minimum-32-bytes!!"
    assert "Authorization" not in client._client.headers

    await client.close()


def test_layer5_tenant_fallback_fails_closed_without_service_auth_secret(monkeypatch):
    monkeypatch.delenv("SERVICE_AUTH_SECRET", raising=False)

    with pytest.raises(ValueError, match="SERVICE_AUTH_SECRET is required"):
        Layer5GroundTruthClient(
            base_url="http://example.test",
            tenant_id="12345678-1234-1234-1234-123456789abc",
        )


@pytest.mark.asyncio
async def test_layer5_service_token_does_not_mix_tenant_fallback_secret(monkeypatch):
    monkeypatch.setenv("SERVICE_AUTH_SECRET", "different-service-auth-secret-minimum-32-bytes")

    client = Layer5GroundTruthClient(
        base_url="http://example.test",
        service_token="signed-service-jwt",
        tenant_id="12345678-1234-1234-1234-123456789abc",
    )

    assert client._client.headers["Authorization"] == "Bearer signed-service-jwt"
    assert "X-Tenant-ID" not in client._client.headers
    assert "X-Service-Auth" not in client._client.headers

    await client.close()
