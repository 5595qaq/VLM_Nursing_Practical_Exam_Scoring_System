import pytest
from app import sop_cli_guard as guard


def test_no_policy_keeps_the_original_cli(monkeypatch):
    monkeypatch.delenv('SOP_CLI_CREDIT_POLICY', raising=False)
    assert guard.guarded_call() is None


@pytest.mark.parametrize('maximum', [None, 0, 32769])
def test_unbounded_output_cannot_use_a_smaller_cost_reservation(monkeypatch, maximum):
    monkeypatch.setenv('SOP_CLI_CREDIT_POLICY', 'unused-policy.json')
    monkeypatch.setattr(guard.settings, 'SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS', maximum)
    with pytest.raises(ValueError, match='MAX_OUTPUT_TOKENS'):
        guard.guarded_call()
