"""Shared builders for the sop_grading tests (a uniquely named module, unlike conftest.py)."""

import copy

_SEGMENTATION = None


def make_segmentation() -> dict:
    """A valid sop-video-skill result: step N detected at [10N, 10N+8], with a few special cases."""
    global _SEGMENTATION
    if _SEGMENTATION is None:
        steps = []
        for sid in range(1, 31):
            start = sid * 10.0
            steps.append({"step_id": sid, "start": start, "end": start + 8.0, "confidence": 0.8,
                          "status": "detected", "evidence": f"global #{sid}"})
        steps[0].update(start=0.0, end=18.0)  # washing long enough (>= 15 s)
        steps[1] = {"step_id": 2, "start": None, "end": None, "confidence": 0.0,
                    "status": "not_visually_determinable", "evidence": "未見注視紀錄單 ≥ 10 s",
                    "logical_window": [20.0, 40.0]}
        steps[10] = {"step_id": 11, "start": None, "end": None, "confidence": 1.0,
                     "status": "not_performed", "evidence": "已自評分表移除"}
        for sid in (17, 27):
            steps[sid - 1]["handoff"] = "flick_detector"
            steps[sid - 1]["merged_with"] = [sid - 1]
        _SEGMENTATION = {
            "video_id": "VID_0001",
            "meta": {"duration": 400.0,
                     "crop": {"box": [0.2, 0.25, 0.7, 1.0], "hands_box": [0.28, 0.42, 0.62, 0.95],
                              "rounds": 1, "notes": ""}},
            "steps": steps, "unassigned_spans": [], "observations": [], "zoom_log": [],
        }
    return copy.deepcopy(_SEGMENTATION)


def seg_step(seg: dict, sid: int) -> dict:
    return next(s for s in seg["steps"] if s["step_id"] == sid)


def result_step(result: dict, sid: int) -> dict:
    return next(s for s in result["steps"] if s["step_id"] == sid)


def criterion(result: dict, cid: str) -> dict:
    return next(c for c in result_step(result, int(cid.rstrip("abcdefghijklmnopqrstuvwxyz")))["criteria"]
                if c["criterion_id"] == cid)


def fill_visual(result: dict, verdict: str = "正確") -> dict:
    """Pretend the grading agent graded every 待判定 criterion, then rebuild the step entries."""
    from rubric import fmt_time, load_rubric
    from rules import build_step

    rubric = {s["step_id"]: s for s in load_rubric()["steps"]}
    for i, step in enumerate(result["steps"]):
        for c in step["criteria"]:
            if c["verdict"] == "待判定":
                start, end = c["window"]
                c.update(verdict=verdict, confidence=0.7, evidence=[f"clip@{start:g}-{end:g}"],
                         explanation=f"{fmt_time(start)}–{fmt_time(end)} 片段中看到該動作完成。")
        result["steps"][i] = build_step(rubric[step["step_id"]], step["criteria"])
    return result


def flag_review(result: dict, cid: str, reason: str = "學生插針 3 次，有對有錯") -> dict:
    """Mark one graded visual criterion as needing human review, then rebuild its step entry."""
    from rubric import load_rubric
    from rules import build_step

    c = criterion(result, cid)
    c.update(verdict="部分正確", needs_review=True, review_reason=reason,
             explanation="02:57 第一次插針三指握外管、垂直向下，符合；03:20 第二次改抓活塞柄插入，不符合。")
    sid = int(cid.rstrip("abcdefghijklmnopqrstuvwxyz"))
    spec = next(s for s in load_rubric()["steps"] if s["step_id"] == sid)
    i = next(i for i, s in enumerate(result["steps"]) if s["step_id"] == sid)
    result["steps"][i] = build_step(spec, result["steps"][i]["criteria"])
    return result
