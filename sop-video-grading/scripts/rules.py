"""Grade the program criteria of rubric.json from a sop-video-skill segmentation result.

No model call. For every step it:
  - program criteria: decides 正確／錯誤／無法判定 from the step's segmentation status and span;
  - visual criteria: computes the window the grading agent must look at and leaves them 待判定
    (or decides them directly when segmentation says the step was not performed / has no span);
  - not_visible criteria: fixed 無法判定 with the rubric's reason;
then aggregates the step verdict (rubric.aggregate) and joins the explanations.

usage: python rules.py segmentation.json [-o grading.json]
"""

import argparse
import json
import sys
from pathlib import Path

from rubric import (CORRECT, NOT_APPLICABLE, PENDING, SOURCE_FOR, UNDETERMINED, WRONG, aggregate,
                    fmt_time, load_rubric)

WINDOW_MARGIN = 2.0  # seconds added on both sides of a visual criterion's window


def grade_from_segmentation(seg: dict, rubric: dict | None = None,
                            margin: float = WINDOW_MARGIN) -> dict:
    rubric = rubric or load_rubric()
    duration = seg["meta"]["duration"]
    entries: dict[int, list[dict]] = {}
    for entry in seg["steps"]:
        entries.setdefault(entry["step_id"], []).append(entry)

    steps = []
    for step in rubric["steps"]:
        criteria = [_grade(c, step["step_id"], entries, duration, margin) for c in step["criteria"]]
        steps.append(build_step(step, criteria))
    return {"video_id": seg["video_id"], "rubric_version": rubric["version"], "steps": steps}


def build_step(step: dict, criteria: list[dict]) -> dict:
    """Step entry from graded criteria; also used after the grading agent fills visual criteria."""
    if step.get("removed"):
        return {"step_id": step["step_id"], "verdict": NOT_APPLICABLE,
                "explanation": "評分表已移除此步驟，不評分。", "confidence": 1.0, "criteria": []}
    texts = {c["id"]: c["text"] for c in step["criteria"]}
    verdict = aggregate(step, {c["criterion_id"]: c["verdict"] for c in criteria})
    scored = [c for c, r in zip(criteria, step["criteria"]) if r["category"] != "not_visible"]
    explanation = "；".join(
        f"{c['criterion_id']}「{texts[c['criterion_id']]}」{c['verdict']}：{c['explanation'].rstrip('。')}"
        + (f"（需人工複核：{c['review_reason'].rstrip('。')}）" if c.get("needs_review") else "")
        for c in criteria) + "。"
    out = {"step_id": step["step_id"], "verdict": verdict, "explanation": explanation,
           "confidence": min(c["confidence"] for c in scored), "criteria": criteria}
    if any(c.get("needs_review") for c in criteria):
        out["needs_review"] = True
    return out


def _status(entries: list[dict]) -> str:
    statuses = {e["status"] for e in entries}
    for status in ("detected", "not_performed"):
        if status in statuses:
            return status
    return "not_visually_determinable"


def _detected(entries: list[dict]) -> list[dict]:
    return sorted((e for e in entries if e["status"] == "detected"), key=lambda e: e["start"])


def _spans_text(entries: list[dict]) -> str:
    return "、".join(f"{fmt_time(e['start'])}–{fmt_time(e['end'])}" for e in entries)


def _seg_evidence(entries: list[dict]) -> str:
    return "；".join(e["evidence"] for e in entries if e["evidence"]) or "無"


def _criterion(c: dict, verdict: str, explanation: str, *, source: str | None = None,
               evidence: list[str] | None = None, confidence: float = 0.0,
               reason: str | None = None, window: list[float] | None = None) -> dict:
    return {"criterion_id": c["id"], "verdict": verdict, "source": source or SOURCE_FOR[c["category"]],
            "explanation": explanation, "evidence": evidence or [], "confidence": confidence,
            "reason": reason, "window": window}


def _grade(c: dict, sid: int, entries: dict[int, list[dict]], duration: float, margin: float) -> dict:
    own = entries.get(sid, [])
    status = _status(own)
    seg_ref = [f"seg:step {sid}"]

    if c["category"] == "not_visible":
        return _criterion(c, UNDETERMINED, f"{c['note']}，無法判定。", confidence=1.0,
                          reason=c["reason"])

    if status == "not_performed":
        conf = max(e["confidence"] for e in own)
        return _criterion(c, WRONG, f"切分結果：步驟 {sid} 未執行（{_seg_evidence(own)}）。",
                          source="rule", evidence=seg_ref, confidence=conf)

    if c["category"] == "program":
        if status != "detected":
            return _criterion(c, UNDETERMINED,
                              f"切分結果：步驟 {sid} 無法定位（{_seg_evidence(own) if own else '無此步驟'}）。",
                              evidence=seg_ref, reason="切分未定位")
        found = _detected(own)
        conf = max(e["confidence"] for e in found)
        rule = c["rule"]
        if rule["type"] == "min_duration":
            total = sum(e["end"] - e["start"] for e in found)
            ok = total >= rule["seconds"]
            return _criterion(
                c, CORRECT if ok else WRONG,
                f"步驟 {sid} 於 {_spans_text(found)}，共 {total:.1f} 秒，"
                f"{'達到' if ok else '不足'} {rule['seconds']} 秒（{_seg_evidence(found)}）。",
                evidence=seg_ref, confidence=conf)
        return _criterion(c, CORRECT, f"切分結果：步驟 {sid} 於 {_spans_text(found)}（{_seg_evidence(found)}）。",
                          evidence=seg_ref, confidence=conf)

    window = _window(c["window_steps"], entries, duration, margin)
    if window is None:
        return _criterion(c, UNDETERMINED,
                          f"切分結果未定位步驟 {'、'.join(map(str, c['window_steps']))}，無法取得要看的畫面區間。",
                          source="rule", evidence=seg_ref, reason="切分未定位")
    return _criterion(c, PENDING, f"等待評分 agent 看畫面判定（區間 {fmt_time(window[0])}–{fmt_time(window[1])}）。",
                      window=window)


def _window(step_ids: list[int], entries: dict[int, list[dict]], duration: float,
            margin: float) -> list[float] | None:
    """Union of the detected spans and logical windows of `step_ids`, padded by `margin`."""
    bounds = []
    for sid in step_ids:
        for e in entries.get(sid, []):
            if e["status"] == "detected":
                bounds.append((e["start"], e["end"]))
            elif e.get("logical_window"):
                bounds.append(tuple(e["logical_window"]))
    if not bounds:
        return None
    start = max(0.0, min(b[0] for b in bounds) - margin)
    end = min(duration, max(b[1] for b in bounds) + margin)
    return [round(start, 2), round(end, 2)]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("segmentation")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    a = ap.parse_args(argv[1:])
    seg = json.loads(Path(a.segmentation).read_text(encoding="utf-8"))
    text = json.dumps(grade_from_segmentation(seg), ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
