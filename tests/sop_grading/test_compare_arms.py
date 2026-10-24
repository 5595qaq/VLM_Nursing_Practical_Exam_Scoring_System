"""Tests for compare_arms.py (comparing record folders of the same videos)."""

import json

import pytest

import compare_arms as ca


def test_mcnemar_exact_two_sided():
    assert ca.mcnemar(0, 0) == 1.0
    assert ca.mcnemar(6, 0) == pytest.approx(2 / 64)
    assert ca.mcnemar(3, 3) == 1.0


def test_flips_skip_and_merge():
    one = {"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "錯誤"}]}
    two = {"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "無法判定"}]}
    partial = {"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "部分正確"}]}
    assert ca.flips([one]) == (0, 0)
    assert ca.flips([one, two]) == (1, 2)
    assert ca.flips([one, two], skip={2}) == (0, 1)
    assert ca.flips([one, partial]) == (1, 2)
    assert ca.flips([one, partial], merged={2}) == (0, 2)  # 錯誤 = 部分正確 on step 2


def test_merge_faults_only_on_the_given_steps():
    rows = [{"step_id": 10, "human": "錯誤", "model": "部分正確", "match": False},
            {"step_id": 13, "human": "錯誤", "model": "部分正確", "match": False},
            {"step_id": 10, "human": "正確", "model": "部分正確", "match": False}]
    assert [r["match"] for r in ca.merge_faults(rows, {10})] == [True, False, False]


def write_record(folder, runs):
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps({"video_id": "VID_0001", "runs": runs}), encoding="utf-8")


def test_run_cost_prefers_segmentation_usage_and_knows_when_cached_is_missing(tmp_path):
    write_record(tmp_path / "VID_0001" / "new", [{"usage": {"input_tokens": 100, "cached_input_tokens": 30,
                                                            "output_tokens": 5},
                                                  "segmentation_usage": {"input_tokens": 50, "cached_input_tokens": 20,
                                                                         "output_tokens": 4}}])
    cost = ca.run_cost(tmp_path, "new", "VID_0001")
    assert (cost["input"], cost["output"], cost["segmentation_input"], cost["cached_input"]) == (150, 9, 50, 50)
    write_record(tmp_path / "VID_0001" / "old", [{"usage": {"input_tokens": 100, "output_tokens": 5}}])
    assert ca.run_cost(tmp_path, "old", "VID_0001")["cached_input"] is None  # an older record: unknown, not 0


def test_arm_runs_reads_each_runs_grading(tmp_path):
    base = tmp_path / "VID_0001" / "v"
    write_record(base, [{}, {}])
    for n in (1, 2):
        (base / f"run{n}").mkdir()
        (base / f"run{n}" / "grading.json").write_text(json.dumps({"n": n}), encoding="utf-8")
    (base / "grading.json").write_text(json.dumps({"n": 2}), encoding="utf-8")
    assert [g["n"] for g in ca.arm_runs(tmp_path, "v")["VID_0001"]] == [1, 2]
