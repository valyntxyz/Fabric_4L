from __future__ import annotations

import pytest

from layer4_agents.harness.models import ActionClass, GateStatus, GateType, HumanGate
from layer4_agents.harness.policies import enforce_action_approval
from layer4_agents.policies.approval_actions import ACTION_APPROVAL_POLICIES, ApprovalRequiredError


@pytest.mark.parametrize("action_class", list(ACTION_APPROVAL_POLICIES.keys()))
def test_required_action_class_blocked_without_approval(action_class: ActionClass) -> None:
    with pytest.raises(ApprovalRequiredError):
        enforce_action_approval(run_id="run_1", action_class=action_class, gate=None)


@pytest.mark.parametrize("action_class,policy", list(ACTION_APPROVAL_POLICIES.items()))
def test_required_action_class_allowed_with_matching_gate(
    action_class: ActionClass, policy
) -> None:
    gate = HumanGate(
        run_id="run_1",
        tenant_id="tenant_1",
        gate_type=policy.required_gate_type,
        status=GateStatus.APPROVED,
    )
    evidence = enforce_action_approval(run_id="run_1", action_class=action_class, gate=gate)
    assert evidence is not None
    assert evidence["run_id"] == "run_1"
    assert evidence["gate_id"] == gate.id
    assert evidence["gate_type"] == policy.required_gate_type.value


def test_hostile_mismatched_gate_type_fails_closed() -> None:
    gate = HumanGate(
        run_id="run_1",
        tenant_id="tenant_1",
        gate_type=GateType.APPROVE_ASSUMPTIONS,
        status=GateStatus.APPROVED,
    )
    with pytest.raises(ApprovalRequiredError):
        enforce_action_approval(
            run_id="run_1",
            action_class=ActionClass.PUBLISH_BUSINESS_CASE,
            gate=gate,
        )
