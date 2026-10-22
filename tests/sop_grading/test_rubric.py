"""Tests for rubric.py: rubric.json itself, the rubric checks, and step aggregation."""

import copy

import pytest

from rubric import (CORRECT, NOT_APPLICABLE, PARTIAL, PENDING, UNDETERMINED, WRONG, aggregate,
                    check_rubric, criteria_by_id, fmt_time, load_rubric)


def test_rubric_json_is_valid():
    rubric = load_rubric()
    assert [s["step_id"] for s in rubric["steps"]] == list(range(1, 31))


def test_rubric_classification_matches_the_plan():
    by_id = criteria_by_id(load_rubric())
    assert by_id["1a"]["category"] == "visual" and by_id["1a"]["min_seconds"] == 15  # timed on the clip (rubric 1.5)
    assert by_id["1a"]["key"] is True and by_id["1b"]["key"] is False
    assert by_id["1b"]["category"] == "visual" and by_id["1b"]["sample_interval"] == 0.5
    for cid in ("2a", "4a", "29a"):  # gaze >= 10 s is judged from frames
        assert by_id[cid]["category"] == "visual"
    for cid in ("17a", "27a", "15b", "25b"):  # flicking is judged with audio
        assert by_id[cid]["audio"] is True
    for cid in ("2c", "3b", "14b", "24b"):
        assert by_id[cid]["category"] == "not_visible"
    # needle direction stands in for the bevel, which the camera cannot resolve (rubric 1.4)
    assert by_id["13b"]["category"] == "visual" and by_id["13b"]["key"] is False and by_id["13b"]["window_steps"] == [13]
    # pulling the plunger back is checked on the frames, not inferred from the step being located (rubric 1.6)
    assert by_id["16a"]["category"] == "visual" and by_id["16a"]["key"] is True
    assert by_id["16a"]["window_steps"] == [15, 16, 17]  # same task as 15b + 17a
    assert by_id["26a"]["category"] == "visual" and by_id["26a"]["key"] is True
    assert by_id["26a"]["window_steps"] == by_id["26b"]["window_steps"] == [26]
    # rolling the vial is watched on a clip, not inferred from the step being located (rubric 1.8)
    assert by_id["20a"]["category"] == "visual" and by_id["20a"]["key"] is True
    assert by_id["20a"]["window_steps"] == [20] and by_id["20a"]["clip"] is True
    # fixing the needle may happen before, after or apart from taking the syringe out (rubric 1.9)
    assert "取出時" not in by_id["10b"]["text"] and "取出後" in by_id["10b"]["text"]
    assert "隔著包裝" in by_id["10b"]["text"]  # fixing through the wrapper counts (rubric 1.10)
    assert by_id["10b"]["category"] == "visual" and by_id["10b"]["key"] is False
    assert by_id["10b"]["window_steps"] == [10]
    # pushing the air out is its own non-key criterion, in the same task as the flick (rubric 1.7)
    for flick, expel in (("17a", "17b"), ("27a", "27b")):
        assert by_id[flick]["key"] is True and by_id[expel]["key"] is False
        assert by_id[expel]["category"] == "visual" and by_id[expel]["window_steps"] == by_id[flick]["window_steps"]


def test_removed_step_has_no_criteria():
    step11 = load_rubric()["steps"][10]
    assert step11["removed"] is True and step11["criteria"] == []


def broken(mutate) -> list[str]:
    rubric = copy.deepcopy(load_rubric())
    mutate(rubric)
    return check_rubric(rubric)


def test_duplicate_criterion_id_is_rejected():
    def mutate(r):
        r["steps"][5]["criteria"][0]["id"] = "6a"
        r["steps"][5]["criteria"].append(dict(r["steps"][5]["criteria"][0]))
    assert any("duplicate id" in e for e in broken(mutate))


def test_criterion_id_must_match_step():
    assert any("step number plus a letter" in e
               for e in broken(lambda r: r["steps"][4]["criteria"][0].update(id="6z")))


def test_program_criterion_needs_known_rule():
    assert any("rule.type" in e
               for e in broken(lambda r: r["steps"][4]["criteria"][0].update(rule={"type": "guess"})))


def test_visual_criterion_needs_window_steps():
    assert any("window_steps" in e
               for e in broken(lambda r: r["steps"][11]["criteria"][0].pop("window_steps")))


def test_clip_flag_needs_a_visual_criterion():
    assert any("clip needs a visual criterion" in e
               for e in broken(lambda r: r["steps"][4]["criteria"][0].update(clip=True)))  # 5a (program)
    assert any("clip needs a visual criterion" in e
               for e in broken(lambda r: r["steps"][19]["criteria"][0].update(clip="yes")))  # 20a


def test_not_visible_criterion_cannot_be_key():
    assert any("cannot be key" in e
               for e in broken(lambda r: r["steps"][13]["criteria"][1].update(key=True)))  # 14b


def test_step_needs_a_scored_criterion():
    def mutate(r):
        r["steps"][2]["criteria"] = [r["steps"][2]["criteria"][1]]  # only 3b (not_visible)
    assert any("at least one program or visual" in e for e in broken(mutate))


def test_load_rubric_raises_on_invalid_file(tmp_path):
    path = tmp_path / "rubric.json"
    path.write_text('{"version": "x", "steps": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid rubric"):
        load_rubric(path)


# --- aggregate --------------------------------------------------------------

STEP = {"step_id": 99, "criteria": [
    {"id": "k", "category": "program", "key": True},
    {"id": "n", "category": "visual", "key": False},
    {"id": "x", "category": "not_visible", "key": False},
]}


@pytest.mark.parametrize("k, n, expected", [
    (CORRECT, CORRECT, CORRECT),
    (WRONG, CORRECT, WRONG),          # key criterion wrong
    (CORRECT, WRONG, PARTIAL),        # non-key wrong, another correct
    (CORRECT, PARTIAL, PARTIAL),      # a criterion itself judged partially correct
    (WRONG, PARTIAL, WRONG),          # key wrong still wins
    (UNDETERMINED, WRONG, WRONG),     # wrong and nothing correct
    (PENDING, WRONG, PENDING),        # could still become partial
    (CORRECT, PENDING, PENDING),
    (CORRECT, UNDETERMINED, UNDETERMINED),
    (UNDETERMINED, UNDETERMINED, UNDETERMINED),
])
def test_aggregate(k, n, expected):
    assert aggregate(STEP, {"k": k, "n": n, "x": UNDETERMINED}) == expected


def test_not_visible_criterion_never_counts():
    assert aggregate(STEP, {"k": CORRECT, "n": CORRECT, "x": UNDETERMINED}) == CORRECT


def test_removed_step_aggregates_to_not_applicable():
    assert aggregate({"step_id": 11, "removed": True, "criteria": []}, {}) == NOT_APPLICABLE


def test_step29_needs_the_record_sheet():
    step29 = load_rubric()["steps"][28]
    # looked at the empty vial and discarded it, but never looked at the record sheet
    assert aggregate(step29, {"29a": WRONG, "29b": WRONG, "29c": CORRECT}) == WRONG
    assert aggregate(step29, {"29a": CORRECT, "29b": CORRECT, "29c": WRONG}) == PARTIAL
    assert aggregate(step29, {"29a": UNDETERMINED, "29b": UNDETERMINED, "29c": WRONG}) == WRONG


@pytest.mark.parametrize("v30a, v30b, expected", [
    ("錯誤", "正確", WRONG),        # placed on the table: wrong even if held by the plunger end
    ("部分正確", "正確", PARTIAL),  # put on the table first, then into the package
    ("正確", "錯誤", PARTIAL),      # into the package but held by the needle
    ("正確", "正確", CORRECT),
])
def test_step30_table_is_wrong(v30a, v30b, expected):
    step30 = load_rubric()["steps"][29]
    assert [c["key"] for c in step30["criteria"]] == [True, False]
    assert aggregate(step30, {"30a": v30a, "30b": v30b}) == expected


@pytest.mark.parametrize("seconds, text", [(0, "00:00"), (59.9, "00:59"), (125.4, "02:05")])
def test_fmt_time(seconds, text):
    assert fmt_time(seconds) == text
