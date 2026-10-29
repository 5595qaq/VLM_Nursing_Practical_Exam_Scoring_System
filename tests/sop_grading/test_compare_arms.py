"""Tests for compare_arms.py (comparing record folders of the same videos)."""

import json

import pytest

import compare_arms as ca


def test_mcnemar_exact_two_sided():
    assert ca.mcnemar(0, 0) == 1.0
    assert ca.mcnemar(6, 0) == pytest.approx(2 / 64)
    assert ca.mcnemar(3, 3) == 1.0


def test_flips_skip_and_merge():
    one = ({"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "錯誤"}]}, None)
    two = ({"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "無法判定"}]}, None)
    partial = ({"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "部分正確"}]}, None)
    assert ca.flips([one]) == (0, 0)
    assert ca.flips([one, two]) == (1, 2)
    assert ca.flips([one, two], skip={2}) == (0, 1)
    assert ca.flips([one, partial]) == (1, 2)
    assert ca.flips([one, partial], merged={2}) == (0, 2)  # 錯誤 = 部分正確 on step 2


def test_flips_counts_only_steps_every_run_graded():
    run1 = ({"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 10, "verdict": "正確"}]}, {1, 10})
    rerun = ({"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 10, "verdict": "錯誤"}]}, {10})  # --steps 10
    assert ca.flips([run1, rerun]) == (1, 1)  # step 1 was carried over, not regraded


def test_merge_faults_only_on_the_given_steps():
    rows = [{"step_id": 10, "human": "錯誤", "model": "部分正確", "match": False},
            {"step_id": 13, "human": "錯誤", "model": "部分正確", "match": False},
            {"step_id": 10, "human": "正確", "model": "部分正確", "match": False}]
    assert [r["match"] for r in ca.merge_faults(rows, {10})] == [True, False, False]
    rows[0]["time_match"] = False  # a merged verdict match is still wrong when the time does not match
    assert [r["strict_match"] for r in ca.merge_faults(rows, {10})] == [False, False, False]


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


def test_run_cost_counts_cached_tokens_of_a_copied_segmentation(tmp_path):
    seg = tmp_path / "VID_0001" / "a" / "run1" / "seg"
    seg.mkdir(parents=True)
    (seg / "transcript.json").write_text(json.dumps([{"input_tokens": 50, "cached_input_tokens": 20,
                                                      "output_tokens": 4, "seconds": 3.0}]), encoding="utf-8")
    base = tmp_path / "VID_0001" / "b"
    write_record(base, [{"usage": {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 5}}])
    record = json.loads((base / "record.json").read_text(encoding="utf-8"))
    record["segmentation_from"] = {"record": "a", "run": 1}
    (base / "record.json").write_text(json.dumps(record), encoding="utf-8")
    cost = ca.run_cost(tmp_path, "b", "VID_0001")
    assert (cost["input"], cost["cached_input"], cost["seconds"]) == (150, 50, 3.0)


def test_arm_runs_reads_each_runs_grading(tmp_path):
    base = tmp_path / "VID_0001" / "v"
    write_record(base, [{}, {}])
    for n in (1, 2):
        (base / f"run{n}").mkdir()
        (base / f"run{n}" / "grading.json").write_text(json.dumps({"n": n}), encoding="utf-8")
    (base / "grading.json").write_text(json.dumps({"n": 2}), encoding="utf-8")
    assert [g["n"] for g, _ in ca.arm_runs(tmp_path, "v")["VID_0001"]] == [1, 2]


def test_arm_runs_falls_back_to_the_latest_grading_when_early_runs_kept_none(tmp_path):
    base = tmp_path / "VID_0001" / "v"
    write_record(base, [{"steps": [1, 10]}, {"steps": [10]}])  # run1 predates runN/grading.json
    (base / "run2").mkdir()
    (base / "run2" / "grading.json").write_text(json.dumps({"n": 2}), encoding="utf-8")
    (base / "grading.json").write_text(json.dumps({"n": "latest"}), encoding="utf-8")
    assert ca.arm_runs(tmp_path, "v")["VID_0001"] == [({"n": "latest"}, None)]
