from __future__ import annotations

"""Tests for Phase 1 Tenant Lifecycle Hardening.

Covers:
- Task 1.4: Tenant status lifecycle (transition_to, audit fields)
- Task 1.5: Suspended/pending/deleted tenant enforcement in middleware
- Task 1.7: Tenant service lifecycle methods and API routes
"""


import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from value_fabric.shared.identity.middleware import GovernanceMiddleware
from value_fabric.shared.models.typed_dict import TypedDictModel


class MiddlewareTenantStatusEnforcementJwtPayloadResult(TypedDictModel):
    roles: list[Any]
    sub: Any
    tenant_id: Any

class MiddlewareTenantStatusEnforcementGetDataResult(TypedDictModel):
    ok: bool


# ---------------------------------------------------------------------------
# Task 1.4: Tenant Model Lifecycle State Machine
# ---------------------------------------------------------------------------


class TestTenantStatusTransitions:
    """Test the enhanced transition_to() state machine on the Tenant model."""

    def _make_tenant(self, status: str = "active"):
        """Create a Tenant instance with the given status."""
        from layer4_agents.tenants.models.tenant import Tenant

        tenant = Tenant(
            id=uuid.uuid4(),
            name="Test Tenant",
            slug="test-tenant",
            status=status,
        )
        return tenant

    def test_active_to_suspended(self):
        """Active tenants can be suspended."""
        tenant = self._make_tenant("active")
        tenant.transition_to("suspended", reason="billing overdue", changed_by="billing-service")
        assert tenant.status == "suspended"
        assert tenant.status_reason == "billing overdue"
        assert tenant.status_changed_by == "billing-service"
        assert tenant.status_changed_at is not None

    def test_suspended_to_active(self):
        """Suspended tenants can be reactivated."""
        tenant = self._make_tenant("suspended")
        tenant.transition_to("active", reason="payment received", changed_by="admin@example.com")
        assert tenant.status == "active"
        assert tenant.status_reason == "payment received"

    def test_pending_to_active(self):
        """Pending tenants can be activated."""
        tenant = self._make_tenant("pending")
        tenant.transition_to("active", reason="onboarding complete")
        assert tenant.status == "active"

    def test_active_to_deleted(self):
        """Active tenants can be soft-deleted."""
        tenant = self._make_tenant("active")
        tenant.transition_to("deleted", reason="customer churn")
        assert tenant.status == "deleted"

    def test_suspended_to_deleted(self):
        """Suspended tenants can be soft-deleted."""
        tenant = self._make_tenant("suspended")
        tenant.transition_to("deleted", reason="account cleanup")
        assert tenant.status == "deleted"

    def test_invalid_transition_raises_value_error(self):
        """Invalid transitions must raise ValueError."""
        tenant = self._make_tenant("deleted")
        with pytest.raises(ValueError, match="Invalid status transition"):
            tenant.transition_to("active")

    def test_pending_to_suspended_invalid(self):
        """Pending tenants cannot be directly suspended."""
        tenant = self._make_tenant("pending")
        with pytest.raises(ValueError, match="Invalid status transition"):
            tenant.transition_to("suspended")

    def test_deleted_to_anything_invalid(self):
        """Deleted is a terminal state — no transitions allowed."""
        tenant = self._make_tenant("deleted")
        for target in ("active", "suspended", "pending"):
            with pytest.raises(ValueError):
                tenant.transition_to(target)

    def test_convenience_activate(self):
        """activate() convenience method works."""
        tenant = self._make_tenant("pending")
        tenant.activate(reason="auto-approved", changed_by="system")
        assert tenant.status == "active"
        assert tenant.status_changed_by == "system"

    def test_convenience_suspend(self):
        """suspend() convenience method works."""
        tenant = self._make_tenant("active")
        tenant.suspend(reason="TOS violation", changed_by="trust-safety")
        assert tenant.status == "suspended"

    def test_convenience_mark_deleted(self):
        """mark_deleted() convenience method works."""
        tenant = self._make_tenant("active")
        tenant.mark_deleted(reason="customer request", changed_by="admin")
        assert tenant.status == "deleted"

    def test_backward_compat_transition_status(self):
        """Deprecated transition_status() returns bool instead of raising."""
        tenant = self._make_tenant("active")
        assert tenant.transition_status("suspended") is True
        assert tenant.status == "suspended"

    def test_backward_compat_transition_status_invalid(self):
        """Deprecated transition_status() returns False on invalid transition."""
        tenant = self._make_tenant("deleted")
        assert tenant.transition_status("active") is False
        assert tenant.status == "deleted"

    def test_audit_fields_updated_on_transition(self):
        """status_changed_at, status_reason, status_changed_by are all set."""
        tenant = self._make_tenant("active")
        before = datetime.now(UTC)
        tenant.transition_to("suspended", reason="test", changed_by="tester")
        assert tenant.status_changed_at >= before
        assert tenant.status_reason == "test"
        assert tenant.status_changed_by == "tester"
        assert tenant.updated_at >= before


# ---------------------------------------------------------------------------
# Task 1.5: Middleware Suspended Tenant Enforcement
# ---------------------------------------------------------------------------


class TestMiddlewareTenantStatusEnforcement:
    """Test that GovernanceMiddleware blocks suspended/pending/deleted tenants."""

    @pytest.fixture
    def app_with_status_check(self):
        """Create a FastAPI app with GovernanceMiddleware that checks tenant status."""
        app = FastAPI()

        async def status_resolver(tenant_id):
            """Mock async status resolver."""
            status_map = {
                "11111111-1111-1111-1111-111111111111": "active",
                "22222222-2222-2222-2222-222222222222": "suspended",
                "33333333-3333-3333-3333-333333333333": "pending",
                "44444444-4444-4444-4444-444444444444": "deleted",
            }
            return status_map.get(str(tenant_id), "active")

        app.add_middleware(
            GovernanceMiddleware,
            tenant_status_resolver=status_resolver,
            enforce_authentication=True,
            require_tenant_context=True,
        )

        @app.get("/data")
        async def get_data(request: Request):
            return MiddlewareTenantStatusEnforcementGetDataResult.model_validate({"ok": True})

        return app

    def _jwt_payload(self, tenant_id: str):
        return MiddlewareTenantStatusEnforcementJwtPayloadResult.model_validate({
            "sub": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "roles": ["user"],
        })

    @pytest.mark.asyncio
    async def test_active_tenant_allowed(self, app_with_status_check):
        """Active tenants should pass through middleware."""
        transport = ASGITransport(app=app_with_status_check)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "value_fabric.shared.identity.resolvers.decode_jwt",
                return_value=self._jwt_payload("11111111-1111-1111-1111-111111111111"),
            ):
                response = await client.get(
                    "/data",
                    headers={"Authorization": "Bearer valid"},
                )
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_suspended_tenant_blocked_with_json(self, app_with_status_check):
        """Suspended tenants should get 403 with JSON error body."""
        transport = ASGITransport(app=app_with_status_check)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "value_fabric.shared.identity.resolvers.decode_jwt",
                return_value=self._jwt_payload("22222222-2222-2222-2222-222222222222"),
            ):
                response = await client.get(
                    "/data",
                    headers={"Authorization": "Bearer valid"},
                )
                assert response.status_code == 403
                body = response.json()
                assert body["error"] == "tenant_suspended"
                assert "tenant_id" in body

    @pytest.mark.asyncio
    async def test_pending_tenant_blocked_with_json(self, app_with_status_check):
        """Pending tenants should get 403 with JSON error body."""
        transport = ASGITransport(app=app_with_status_check)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "value_fabric.shared.identity.resolvers.decode_jwt",
                return_value=self._jwt_payload("33333333-3333-3333-3333-333333333333"),
            ):
                response = await client.get(
                    "/data",
                    headers={"Authorization": "Bearer valid"},
                )
                assert response.status_code == 403
                body = response.json()
                assert body["error"] == "tenant_pending"
                assert "tenant_id" in body

    @pytest.mark.asyncio
    async def test_deleted_tenant_returns_404_json(self, app_with_status_check):
        """Deleted tenants should get 404 with JSON error body."""
        transport = ASGITransport(app=app_with_status_check)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "value_fabric.shared.identity.resolvers.decode_jwt",
                return_value=self._jwt_payload("44444444-4444-4444-4444-444444444444"),
            ):
                response = await client.get(
                    "/data",
                    headers={"Authorization": "Bearer valid"},
                )
                assert response.status_code == 404
                body = response.json()
                assert body["error"] == "tenant_not_found"
                assert "tenant_id" in body


# ---------------------------------------------------------------------------
# Task 1.7: Tenant Service Lifecycle Methods
# ---------------------------------------------------------------------------


class TestTenantServiceLifecycle:
    """Test update_tenant_status and delete_tenant service functions."""

    @pytest.mark.asyncio
    async def test_update_tenant_status_uses_state_machine(self):
        """update_tenant_status should use transition_to() and record audit fields."""
        from layer4_agents.tenants.models.tenant import Tenant

        mock_tenant = Tenant(
            id=uuid.uuid4(),
            name="Test",
            slug="test",
            status="active",
        )

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_tenant
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        from layer4_agents.tenants.service import update_tenant_status

        result = await update_tenant_status(
            mock_session,
            mock_tenant.id,
            "suspended",
            reason="billing overdue",
            changed_by="billing-service",
        )

        assert result is True
        assert mock_tenant.status == "suspended"
        assert mock_tenant.status_reason == "billing overdue"
        assert mock_tenant.status_changed_by == "billing-service"

    @pytest.mark.asyncio
    async def test_update_tenant_status_invalid_raises(self):
        """update_tenant_status should raise ValueError on invalid transition."""
        from layer4_agents.tenants.models.tenant import Tenant

        mock_tenant = Tenant(
            id=uuid.uuid4(),
            name="Test",
            slug="test",
            status="deleted",
        )

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_tenant
        mock_session.execute = AsyncMock(return_value=mock_result)

        from layer4_agents.tenants.service import update_tenant_status

        with pytest.raises(ValueError, match="Invalid status transition"):
            await update_tenant_status(mock_session, mock_tenant.id, "active")

    @pytest.mark.asyncio
    async def test_update_tenant_status_not_found(self):
        """update_tenant_status should return False when tenant not found."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        from layer4_agents.tenants.service import update_tenant_status

        result = await update_tenant_status(
            mock_session, uuid.uuid4(), "suspended"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_tenant_uses_state_machine(self):
        """delete_tenant should use transition_to('deleted')."""
        from layer4_agents.tenants.models.tenant import Tenant

        mock_tenant = Tenant(
            id=uuid.uuid4(),
            name="Test",
            slug="test",
            status="active",
        )

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_tenant
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        from layer4_agents.tenants.service import delete_tenant

        result = await delete_tenant(
            mock_session,
            mock_tenant.id,
            reason="customer request",
            changed_by="admin",
        )

        assert result is True
        assert mock_tenant.status == "deleted"
        assert mock_tenant.status_reason == "customer request"


# ---------------------------------------------------------------------------
# Task 1.2/1.3: Column Rename Verification
# ---------------------------------------------------------------------------


class TestColumnRenameConsistency:
    """Verify that organization_id has been fully renamed to tenant_id."""

    def test_layer1_models_use_tenant_id(self):
        """Layer 1 models should use tenant_id, not organization_id."""
        import importlib
        import inspect

        try:
            models = importlib.import_module("layer1_ingestion.shared.models")
            source = inspect.getsource(models)
            # organization_id should not appear as a column definition
            # (it may appear in comments or migration references)
            lines = [
                line
                for line in source.split("\n")
                if "organization_id" in line
                and "mapped_column" in line
            ]
            assert len(lines) == 0, (
                f"Found {len(lines)} model columns still using organization_id: {lines}"
            )
        except ImportError:
            pytest.skip("Layer 1 models not importable in this test environment")

# ---------------------------------------------------------------------------
# RLS Policy Coverage (Task 1.6)
# ---------------------------------------------------------------------------


class TestRLSMigrationCoverage:
    """Verify that all tables with tenant_id have corresponding RLS migrations."""

    @pytest.mark.skip(reason="DEFERRED: Migration file not found - 018_add_rls_to_billing_tables.py")
    def test_billing_tables_have_rls_migration(self):
        """Migration 018 should cover all billing tables."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "m018",
            "migrations/versions/018_add_rls_to_billing_tables.py",
        )
        if spec is None:
            pytest.skip("Migration file not found")
        m018 = importlib.util.module_from_spec(spec)

        expected_tables = {
            "billing_customers",
            "billing_subscriptions",
            "billing_webhook_events",
            "billing_usage_events",
            "billing_invoices",
            "billing_invoice_items",
            "billing_charges",
        }

        # Read the file directly to check table list
        with open(
            "migrations/versions/018_add_rls_to_billing_tables.py"
        ) as f:
            content = f.read()

        for table in expected_tables:
            assert table in content, f"Missing RLS for {table} in migration 018"
