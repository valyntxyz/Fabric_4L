"""
Contract and behavior tests for Pillar 4 (Deterministic Replay) and Pillar 5 (Layer 5 Feedback Loop).

Proves:
1. Conversation turns maintain deterministic governance audit lineage and tool-call replayability.
2. Narrative acceptance closes the self-improvement loop by emitting a Layer 5 TruthObject with full provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from layer4_agents.api.routes.narratives import NarrativeAcceptanceRequest, accept_narrative
from layer4_agents.contracts.artifacts import IntegrityPrecondition
from layer4_agents.workflows.replay import (
    Layer4WorkflowReplayHarness,
    ReplayAuthorizationContext,
    ReplayEventEnvelopeV1,
)
from layer4_agents.models.agent_state import WorkflowStatus, WorkflowType


class _InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, details: dict) -> None:
        self.records.append((action, details))


def _make_conversation_replay_envelope(event_id: str, event_type: str, step: int, payload: dict) -> ReplayEventEnvelopeV1:
    return ReplayEventEnvelopeV1(
        event_id=event_id,
        tenant_id="tenant-123",
        actor="se:user@example.com",
        timestamp=datetime(2026, 8, 21, 12, 0, step, tzinfo=UTC),
        correlation_id="corr-conv-999",
        schema_version="1.0",
        domain="layer4.workflow_state",
        event_type=event_type,
        payload_pointer=f"s3://replay/turns/{event_id}.json",
        payload_checksum=f"sha256:hash_{event_id}",
        payload_redacted=payload,
    )


@pytest.mark.unit
def test_conversation_governance_lineage_replay_determinism():
    """Prove that replaying conversation turn events produces identical, deterministic governance lineage."""
    sink1 = _InMemoryAuditSink()
    harness1 = Layer4WorkflowReplayHarness(sink1)

    sink2 = _InMemoryAuditSink()
    harness2 = Layer4WorkflowReplayHarness(sink2)

    authz = ReplayAuthorizationContext(
        tenant_id="tenant-123",
        actor="replay-runner",
        roles=("replay:execute",),
        environment="test",
    )

    events = [
        _make_conversation_replay_envelope("e2", "workflow.started", 2, {
            "journey_id": "journey_abc",
            "tier": "conversation_agent",
        }),
        _make_conversation_replay_envelope("e1", "workflow.created", 1, {
            "journey_id": "journey_abc",
            "user_message": "Calculate ROI for Cloud Migration",
        }),
        _make_conversation_replay_envelope("e3", "workflow.node_transition", 3, {
            "current_node": "roi_calculator",
            "tool_name": "roi_calculator",
        }),
        _make_conversation_replay_envelope("e4", "workflow.completed", 4, {
            "response_tier": "conversation_agent",
            "fallback": False,
            "degraded": False,
            "journey_id": "journey_abc",
        }),
    ]

    # Run replay 1 (in chronological order)
    res1 = harness1.replay(
        workflow_id="conv-session-1",
        workflow_type=WorkflowType.ROI_CALCULATOR,
        events=events,
        authz=authz,
    )

    # Run replay 2 (in reverse order to verify order-independence and determinism)
    res2 = harness2.replay(
        workflow_id="conv-session-1",
        workflow_type=WorkflowType.ROI_CALCULATOR,
        events=list(reversed(events)),
        authz=authz,
    )

    # Assert deterministic outcome
    assert res1.applied_event_ids == ["e1", "e2", "e3", "e4"]
    assert res1.applied_event_ids == res2.applied_event_ids
    assert res1.state.status == WorkflowStatus.COMPLETED
    assert res1.state.status == res2.state.status
    assert res1.state.current_node == res2.state.current_node

    # State equality excluding nondeterministic timestamps/IDs
    state1 = res1.state.model_dump(exclude={"run_id", "trace_id"})
    state2 = res2.state.model_dump(exclude={"run_id", "trace_id"})
    assert state1 == state2

    # Audit records determinism
    assert len(sink1.records) == len(sink2.records)
    for (act1, det1), (act2, det2) in zip(sink1.records, sink2.records):
        assert act1 == act2
        assert det1["workflow_id"] == det2["workflow_id"]
        assert det1["tenant_id"] == det2["tenant_id"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_narrative_acceptance_creates_l5_truth_object_feedback_loop():
    """Prove Pillar 5: narrative acceptance emits Layer 5 TruthObject with provenance back to turn."""
    mock_request = MagicMock()
    mock_driver = MagicMock()
    mock_request.app.state.neo4j_driver = mock_driver

    mock_svc = AsyncMock()
    mock_svc.get_narrative.return_value = {
        "id": "nar_987",
        "title": "Cloud Modernization Business Case",
        "sections": {
            "value_hypotheses": [
                {"id": "vh_1", "statement": "Reduce compute cost by 35%"},
                {"id": "vh_2", "statement": "Improve developer velocity by 20%"},
            ]
        },
    }
    mock_svc.update_status.return_value = {"status": "approved"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "layer4_agents.services.narrative_builder_service.NarrativeBuilderService",
            lambda d: mock_svc,
        )

        accept_req = NarrativeAcceptanceRequest(
            narrative_version=2,
            account_id="acc_acme",
            journey_id="journey_999",
            conversation_turn_id="turn_456",
            se_feedback_notes="Customer CFO accepted 3-year ROI model during review.",
        )

        result = await accept_narrative("nar_987", accept_req, mock_request, tenant_id="tenant_001")

        assert result["status"] == "accepted"
        assert result["narrative_id"] == "nar_987"
        truth_object = result["truth_object"]
        assert truth_object["truth_object_id"] == "truth_nar_987_2"
        assert truth_object["tenant_id"] == "tenant_001"
        assert truth_object["account_id"] == "acc_acme"
        assert truth_object["journey_id"] == "journey_999"
        assert truth_object["source_artifact_id"] == "nar_987"
        assert truth_object["conversation_turn_id"] == "turn_456"
        assert truth_object["claims_count"] == 2
        assert truth_object["provenance"]["feedback_notes"] == "Customer CFO accepted 3-year ROI model during review."
