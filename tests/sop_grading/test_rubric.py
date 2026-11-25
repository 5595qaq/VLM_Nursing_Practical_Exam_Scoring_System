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
    assert by_id["1a"]["category"] == "visual" and by_id["1a"]["min_seconds"] == 10
    assert by_id["1a"]["cumulative_seconds"] is True  # D-01-01: washing resumed after a new pump of soap adds up
    # 1b missing a motion is not key: long enough washing makes the step 部分正確 (DECISIONS D-01-03)
    assert by_id["1a"]["key"] is True and by_id["1b"]["key"] is False
    assert by_id["1b"]["allow_partial"] is False
    assert by_id["1b"]["category"] == "visual" and by_id["1b"]["sample_interval"] == 0.5
    for cid in ("2a", "4a", "29a"):  # gaze >= 10 s is judged from frames
        assert by_id[cid]["category"] == "visual"
    for cid in ("17a", "27a", "15b", "25b"):  # flicking is judged with audio
        assert by_id[cid]["audio"] is True
    # pad before flicking is key: flicking first makes the whole step wrong (teacher 2026-11-08, rubric 1.28)
    assert by_id["15b"]["key"] is True and by_id["25b"]["key"] is True
    for cid in ("3b", "14b", "24b"):
        assert by_id[cid]["category"] == "not_visible"
    # needle direction stands in for the bevel, which the camera cannot resolve (rubric 1.4)
    assert by_id["13b"]["category"] == "visual" and by_id["13b"]["key"] is False and by_id["13b"]["window_steps"] == [13]
    # the teacher dropped pulling the plunger back (16a, 26a) and drawing air after uncapping (21a) (rubric 1.25)
    assert not {"16a", "21a", "26a"} & by_id.keys()
    assert by_id["26b"]["window_steps"] == [26] and by_id["26b"]["key"] is False
    # turning the needle at the hub cannot be told from the video yet: not graded until confirmed (rubric 1.27)
    assert by_id["13c"]["category"] == "not_visible" and by_id["13c"]["reason"] == "資訊不足"
    step13 = next(s for s in load_rubric()["steps"] if s["step_id"] == 13)
    assert [c["id"] for c in step13["criteria"]] == ["13a", "13b", "13c"]
    # 13c (always undetermined) must not drag step 13 down
    assert aggregate(step13, {"13a": CORRECT, "13b": CORRECT, "13c": UNDETERMINED}) == CORRECT
    assert aggregate(step13, {"13a": CORRECT, "13b": WRONG, "13c": UNDETERMINED}) == PARTIAL
    # rolling the vial is watched on a clip, not inferred from the step being located (rubric 1.8)
    assert by_id["20a"]["category"] == "visual" and by_id["20a"]["key"] is True
    assert by_id["20a"]["window_steps"] == [20] and by_id["20a"]["clip"] is True
    # only fixing after the syringe is out of the wrapper counts (teacher 2026-11-08, rubric 1.29)
    assert "取出時" not in by_id["10b"]["text"] and "取出後" in by_id["10b"]["text"]
    assert "隔著包裝或在包裝裡旋轉不算" in by_id["10b"]["text"]
    assert by_id["10b"]["category"] == "visual" and by_id["10b"]["key"] is False
    # rubric 1.18-pkgspan.1: the agent gets the wrapper spans and judges before and after removal
    assert "手拿空針包裝時段" in by_id["10b"]["text"] and by_id["10b"]["package_spans"] is True
    # teacher 2026-11-08 (rubric 1.29): vial on the table, two-handed recapping and drawing below
    # vertical fail the whole step
    for cid in ("18d", "22d", "19b", "28b", "24c"):
        assert by_id[cid]["category"] == "visual" and by_id[cid]["key"] is True
    assert by_id["24c"]["window_steps"] == [23, 24]
    assert by_id["10b"]["window_steps"] == [10]
    # pointing at the package belongs to the second read and counts until the package is torn open
    # 4d and 4e are key: not checking the diluent or not pointing at the package fails step 4 (D-02-03, D-02-04)
    assert by_id["4e"]["category"] == "visual" and by_id["4e"]["key"] is True and by_id["4d"]["key"] is True
    assert by_id["4e"]["window_steps"] == [4, 5, 6, 7, 8, 9] and by_id["4e"]["after_step"] is True
    assert by_id["4d"]["window_steps"] == [2, 4, 5, 13] and by_id["4d"]["after_step"]
    assert "9b" not in by_id and by_id["9a"]["category"] == "program"
    # 5a and 6a share one task but keep their own windows (rubric 1.18); so do 7a, 8a and 8b, the order of
    # disinfecting and opening the diluent (DECISIONS D-00-03)
    assert by_id["5a"]["task"] == by_id["6a"]["task"] == "5-6"
    assert by_id["5a"]["window_steps"] == [5] and by_id["6a"]["window_steps"] == [6]
    assert by_id["7a"]["task"] == by_id["8a"]["task"] == by_id["8b"]["task"] == "7-8"
    assert by_id["8b"]["key"] is True and by_id["8b"]["window_steps"] == [7, 8]
    # doing an order backwards fails every related step (DECISIONS D-00-03, D-05-03)
    assert {cid: c["wrong_with"] for cid, c in by_id.items() if "wrong_with" in c} == {
        "7a": "8b", "17a": "15b", "27a": "25b"}
    # pushing the air out is its own non-key criterion, in the same task as the flick (rubric 1.7)
    for flick, expel in (("17a", "17b"), ("27a", "27b")):
        assert by_id[flick]["key"] is True and by_id[expel]["key"] is False
        assert by_id[expel]["category"] == "visual" and by_id[expel]["window_steps"] == by_id[flick]["window_steps"]


def test_removed_step_has_no_criteria():
    step11 = load_rubric()["steps"][10]
    assert step11["removed"] is True and step11["criteria"] == []


def test_dropped_steps_16_21_and_step26_only_26b():
    # rubric 1.25: steps 16 and 21 lose their only criterion and are removed like step 11;
    # step 26 keeps the non-key 26b, which alone decides it
    steps = {s["step_id"]: s for s in load_rubric()["steps"]}
    for sid in (16, 21):
        assert steps[sid]["removed"] is True and steps[sid]["criteria"] == []
        assert aggregate(steps[sid], {}) == NOT_APPLICABLE
    assert [c["id"] for c in steps[26]["criteria"]] == ["26b"]
    assert aggregate(steps[26], {"26b": "錯誤"}) == "錯誤"
    assert aggregate(steps[26], {"26b": "正確"}) == "正確"


def broken(mutate) -> list[str]:
    rubric = copy.deepcopy(load_rubric())
    mutate(rubric)
    return check_rubric(rubric)


def test_duplicate_criterion_id_is_rejected():
    def mutate(r):
        r["steps"][5]["criteria"][0]["id"] = "6a"
        r["steps"][5]["criteria"].append(dict(r["steps"][5]["criteria"][0]))
    assert any("duplicate id" in e for e in broken(mutate))


@pytest.mark.parametrize("target, source", [
    ("17a", "99z"),  # no such criterion
    ("17a", "17b"),  # same step
    ("17a", "13b"),  # not key
    ("17b", "15b"),  # the linked criterion itself is not key
    ("15b", "17a"),  # chained: 17a already follows 15b
])
def test_wrong_with_must_name_a_key_visual_criterion_of_another_step(target, source):
    def mutate(r):
        for step in r["steps"]:
            for c in step["criteria"]:
                if c["id"] == target:
                    c["wrong_with"] = source
    assert any(f"criterion {target}: wrong_with" in e for e in broken(mutate))


def test_linked_steps_are_regraded_together():
    from rubric import with_linked_steps
    rubric = load_rubric()
    assert with_linked_steps(None, rubric) is None
    # the source's step brings the target's; the target alone is linked again from the saved source
    assert with_linked_steps({15}, rubric) == {15, 17}
    assert with_linked_steps({17, 3}, rubric) == {3, 17}
    assert with_linked_steps({8}, rubric) == {7, 8}
    assert with_linked_steps({7}, rubric) == {7}
    assert with_linked_steps({25, 1}, rubric) == {1, 25, 27}


def test_criterion_id_must_match_step():
    assert any("step number plus a letter" in e
               for e in broken(lambda r: r["steps"][4]["criteria"][0].update(id="6z")))


def test_program_criterion_needs_known_rule():
    assert any("rule.type" in e
               for e in broken(lambda r: r["steps"][8]["criteria"][0].update(rule={"type": "guess"})))


def test_visual_criterion_needs_window_steps():
    assert any("window_steps" in e
               for e in broken(lambda r: r["steps"][11]["criteria"][0].pop("window_steps")))


def test_clip_flag_needs_a_visual_criterion():
    assert any("clip needs a visual criterion" in e
               for e in broken(lambda r: r["steps"][8]["criteria"][0].update(clip=True)))  # 9a (program)
    assert any("clip needs a visual criterion" in e
               for e in broken(lambda r: r["steps"][19]["criteria"][0].update(clip="yes")))  # 20a


def test_after_step_needs_a_visual_criterion_and_a_bool():
    assert any("after_step needs" in e for e in broken(lambda r: r["steps"][8]["criteria"][0].update(after_step=True)))  # 9a
    assert any("after_step needs" in e for e in broken(lambda r: r["steps"][3]["criteria"][4].update(after_step="yes")))  # 4e


def test_search_steps_needs_a_visual_criterion_and_step_numbers():
    assert any("search_steps needs" in e
               for e in broken(lambda r: r["steps"][8]["criteria"][0].update(search_steps=[8, 10])))  # 9a (program)
    assert any("search_steps needs" in e
               for e in broken(lambda r: r["steps"][5]["criteria"][0].update(search_steps=[5, 31])))  # 6a
    for bad in ([], 18, [5, 18, 20], [True, 18], [7, 18], [1, 5], [5.0, 18]):  # 6a must lie between first and last
        assert any("search_steps needs" in e
                   for e in broken(lambda r: r["steps"][5]["criteria"][0].update(search_steps=bad))), bad
    assert any("search_steps needs" in e  # the search range must cover the criterion's own window
               for e in broken(lambda r: r["steps"][5]["criteria"][0].update(window_steps=[6, 20])))


def test_task_needs_a_visual_criterion_and_a_name():
    assert any("task needs" in e for e in broken(lambda r: r["steps"][8]["criteria"][0].update(task="9")))  # 9a
    for bad in ("", 5, None):
        assert any("task needs" in e for e in broken(lambda r: r["steps"][4]["criteria"][0].update(task=bad))), bad
    assert any("task 5–6 has only" in e  # a typo leaves each name with one criterion
               for e in broken(lambda r: r["steps"][5]["criteria"][0].update(task="5–6")))


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


def test_first_and_second_comparison_have_independent_required_thresholds():
    rubric = load_rubric()
    first, second = rubric["steps"][1], rubric["steps"][3]
    assert [c["id"] for c in first["criteria"]] == ["2a", "2b", "2d"]
    assert first["criteria"][0]["key"] and first["criteria"][0]["min_seconds"] == 10
    first_pass = {"2a": CORRECT, "2b": CORRECT, "2d": CORRECT}
    assert aggregate(first, {**first_pass, "2a": WRONG}) == WRONG
    assert aggregate(first, {**first_pass, "2d": WRONG}) == WRONG
    assert aggregate(first, {**first_pass, "2b": WRONG}) == PARTIAL
    assert aggregate(first, {**first_pass, "2a": UNDETERMINED, "2b": WRONG}) == WRONG
    assert aggregate(first, {**first_pass, "2a": UNDETERMINED, "2b": UNDETERMINED}) == WRONG
    # the record sheet out of frame: the whole step cannot be judged (criteria/02)
    hidden_sheet = {"2a": "藥單看不見"}
    assert aggregate(first, {**first_pass, "2a": UNDETERMINED, "2b": WRONG}, reasons=hidden_sheet) == UNDETERMINED
    assert aggregate(first, {**first_pass, "2a": UNDETERMINED, "2d": WRONG}, reasons=hidden_sheet) == UNDETERMINED
    assert aggregate(first, {**first_pass, "2a": UNDETERMINED}, reasons={"2a": "切分未定位"}) == WRONG
    assert aggregate(first, {**first_pass, "2a": PENDING}) == PENDING
    assert aggregate(first, {**first_pass, "2d": UNDETERMINED}) == UNDETERMINED
    assert second["criteria"][0]["key"] and second["criteria"][0]["min_seconds"] == 10
    passed = {"4a": CORRECT, "4b": CORRECT, "4c": UNDETERMINED, "4d": CORRECT, "4e": CORRECT}
    assert aggregate(second, {**passed, "4a": WRONG}) == WRONG
    assert aggregate(second, {**passed, "4b": WRONG}) == PARTIAL
    assert aggregate(second, {**passed, "4a": UNDETERMINED, "4b": WRONG}) == UNDETERMINED
    assert aggregate(second, {**passed, "4d": WRONG}) == WRONG
    assert aggregate(second, {**passed, "4e": WRONG}) == WRONG
    assert aggregate(second, {**passed, "4e": UNDETERMINED}) == UNDETERMINED  # hidden: a key unseen
    # an unseen 4d/4e leaves the step 無法判定 even when 4b failed (it used to be 部分正確) ...
    assert aggregate(second, {**passed, "4b": WRONG, "4e": UNDETERMINED}) == UNDETERMINED
    assert aggregate(second, {**passed, "4d": UNDETERMINED}, reasons={"4d": "切分未定位"}) == UNDETERMINED
    # ... but a failed key still wins
    assert aggregate(second, {**passed, "4a": WRONG, "4e": UNDETERMINED}) == WRONG
    assert aggregate(second, {**passed, "4d": WRONG, "4e": UNDETERMINED}) == WRONG
    # the sheet out of frame settles step 4; criteria still pending cannot change that
    hidden4 = {"4a": "藥單看不見"}
    assert aggregate(second, {**passed, "4a": UNDETERMINED}, reasons=hidden4) == UNDETERMINED
    assert aggregate(second, {**passed, "4a": UNDETERMINED, "4e": PENDING}, reasons=hidden4) == UNDETERMINED
    # only 藥單看不見 settles the step; 畫面被遮擋 (head or gaze hidden) keeps step 2 錯誤
    assert aggregate(first, {**first_pass, "2a": UNDETERMINED}, reasons={"2a": "畫面被遮擋"}) == WRONG


def test_unconfirmed_step_reason_must_be_a_schema_reason():
    rubric = load_rubric()
    bad = copy.deepcopy(rubric)
    bad["steps"][1]["criteria"][0]["unconfirmed_step_reason"] = "藥單看不到"
    assert any("not a reason the grading agent can give" in e for e in check_rubric(bad))
    bad["steps"][1]["criteria"][0]["unconfirmed_step_reason"] = "切分未定位"  # only the program sets it
    assert any("not a reason the grading agent can give" in e for e in check_rubric(bad))
    assert check_rubric(rubric) == []


def test_step29_needs_the_record_sheet():
    step29 = load_rubric()["steps"][28]
    # looked at the empty vial and discarded it, but never looked at the record sheet
    assert [c["id"] for c in step29["criteria"]] == ["29a", "29b", "29d"]
    passed = {"29a": CORRECT, "29b": CORRECT, "29d": CORRECT}
    assert aggregate(step29, passed) == CORRECT
    assert aggregate(step29, {**passed, "29a": WRONG}) == WRONG
    assert aggregate(step29, {**passed, "29d": WRONG}) == WRONG
    assert aggregate(step29, {**passed, "29b": WRONG}) == PARTIAL
    assert aggregate(step29, {**passed, "29a": UNDETERMINED, "29b": WRONG}) == UNDETERMINED
    assert aggregate(step29, {**passed, "29a": UNDETERMINED, "29d": WRONG}) == WRONG
    assert aggregate(step29, {**passed, "29a": UNDETERMINED, "29d": WRONG},
                     reasons={"29a": "藥單看不見"}) == UNDETERMINED


def test_step30_is_removed():
    # rubric 1.31: the teacher (2026-11-23) cancelled step 30, so 30a/30b are gone like step 11
    step30 = load_rubric()["steps"][29]
    assert step30["removed"] is True and step30["criteria"] == []
    assert aggregate(step30, {}) == NOT_APPLICABLE


@pytest.mark.parametrize("seconds, text", [(0, "00:00"), (59.9, "00:59"), (125.4, "02:05")])
def test_fmt_time(seconds, text):
    assert fmt_time(seconds) == text


def test_cumulative_timing_names_the_action_timed():
    expected = "cumulative_seconds and timed_action"
    assert any(expected in e for e in broken(lambda r: r["steps"][0]["criteria"][1].update(timed_action="洗手")))  # 1b
    assert any(expected in e for e in broken(lambda r: r["steps"][0]["criteria"][0].update(timed_action="")))  # 1a
    assert any(expected in e for e in broken(lambda r: r["steps"][0]["criteria"][0].pop("timed_action")))


def test_segment_check_fields_are_checked():
    rubric = load_rubric()
    bad = copy.deepcopy(rubric)
    crit = {c["id"]: c for s in bad["steps"] for c in s["criteria"]}
    crit["4d"]["segment_check"] = "藥瓶"
    crit["7a"]["segment_check"] = "稀釋液"  # only step 4 entries carry check
    errors = check_rubric(bad)
    assert any("4d: segment_check needs" in e for e in errors)
    assert any("7a: segment_check needs" in e for e in errors)


def test_segment_checks_match_the_segmentation_schema():
    # rubric.py's SEGMENT_CHECKS and sop-video-skill's `check` enum must name the same kinds
    import json
    from pathlib import Path
    from rubric import SEGMENT_CHECKS
    schema = json.loads((Path(__file__).resolve().parents[2] / "sop-video-skill" / "schema.json")
                        .read_text(encoding="utf-8"))
    entry = schema["properties"]["steps"]["items"]["properties"]["check"]
    assert set(entry["enum"]) == SEGMENT_CHECKS
