"""Offline archive verification must use historical sources, never main's rules."""
import copy
import importlib.util
from pathlib import Path
import sys

import pytest


BUNDLE = (Path(__file__).resolve().parents[2] / "sop-video-grading" /
          "experiments/2026-11-23-grading30pdf185")


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location("archive192", BUNDLE / "verify_bundle.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def profiles(verifier):
    result = {}
    for name in verifier.read(BUNDLE / "source-code.json"):
        root = BUNDLE / "sources" / name / "sop-video-grading"
        result[name] = (verifier.read(root / "rubric.json"),
                        verifier.module(root / "scripts/rubric.py", name))
    return result


def saved_step(verifier, sid):
    item = verifier.read(BUNDLE / "index.json")[0]
    data = verifier.read(verifier.REPO / item["results"])
    return (next(s for s in data["steps"] if s["step_id"] == sid),
            item["source_by_step"][str(sid)])


def test_complete_archive_does_not_read_current_rules(verifier, monkeypatch):
    original_read = verifier.read
    original_module = verifier.module

    def archived_read(path):
        assert path.is_relative_to(BUNDLE) or path.is_relative_to(verifier.REPO / "sop-video-grading/runs")
        return original_read(path)

    def archived_module(path, name):
        assert path.is_relative_to(BUNDLE / "sources")
        return original_module(path, name)

    monkeypatch.setattr(verifier, "read", archived_read)
    monkeypatch.setattr(verifier, "module", archived_module)
    result = verifier.verify()
    assert result["archived_files"] == 1094
    assert result["source_files"] == 243
    assert result["steps_with_separate10"] == 210
    assert result["historical_step_aggregations"] == 180
    assert result["historical_selected_final_validations"] == 30
    assert result["model_calls"] == 0
    assert result["current_main_equivalence_checked"] is False
    assert result["new_video_regression"] is False


@pytest.mark.parametrize("sid", [1, 2, 4, 10, 29, 30])
def test_step_uses_recorded_profile(verifier, profiles, sid):
    step, meta = saved_step(verifier, sid)
    assert verifier.verify_archived_step(step, meta, profiles) == 1


@pytest.mark.parametrize("sid", [1, 2, 29])
def test_readsheet_cannot_be_replaced_by_separate174(verifier, profiles, sid):
    step, meta = saved_step(verifier, sid)
    wrong = dict(meta, rule_profile="separate174")
    with pytest.raises(AssertionError, match="rubric version"):
        verifier.verify_archived_step(step, wrong, profiles)


def test_original_step_is_not_claimed_as_new_aggregation(verifier, profiles):
    step, meta = saved_step(verifier, 3)
    assert verifier.verify_archived_step(step, meta, profiles) == 0


def test_changed_verdict_fails(verifier, profiles):
    step, meta = saved_step(verifier, 1)
    changed = dict(step, verdict="錯誤" if step["verdict"] != "錯誤" else "正確")
    with pytest.raises(AssertionError):
        verifier.verify_archived_step(changed, meta, profiles)


def test_missing_criterion_fails(verifier, profiles):
    step, meta = saved_step(verifier, 4)
    changed = copy.deepcopy(step)
    changed["criteria"].pop()
    with pytest.raises(AssertionError, match="criteria"):
        verifier.verify_archived_step(changed, meta, profiles)


def test_original_file_tampering_still_fails(verifier, monkeypatch):
    monkeypatch.setattr(verifier, "digest", lambda path: "changed")
    with pytest.raises(AssertionError):
        verifier.verify()


def test_historical_validator_restores_existing_rubric_import(verifier, profiles, monkeypatch):
    sentinel = object()
    monkeypatch.setitem(sys.modules, "rubric", sentinel)
    scripts = BUNDLE / "sources/separate174/sop-video-grading/scripts"
    validator = verifier.historical_validator(scripts, profiles["separate174"][1])
    assert sys.modules["rubric"] is sentinel
    assert validator.aggregate is profiles["separate174"][1].aggregate


def test_historical_validator_does_not_leave_new_rubric_import(verifier, profiles, monkeypatch):
    monkeypatch.delitem(sys.modules, "rubric", raising=False)
    scripts = BUNDLE / "sources/separate174/sop-video-grading/scripts"
    verifier.historical_validator(scripts, profiles["separate174"][1])
    assert "rubric" not in sys.modules
