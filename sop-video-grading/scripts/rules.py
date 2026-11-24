"""Grade the program criteria of rubric.json from a sop-video-skill segmentation result.

No model call. For every step it:
  - program criteria: decides 正確／錯誤／無法判定 from the step's segmentation status and span;
  - visual criteria: computes the window the grading agent must look at and leaves them 待判定
    (or decides them directly when segmentation says the step was not performed / has no span);
    in a step with a `search_steps` criterion the agent checks whether the step was done, so
    not_performed decides none of its visual criteria, and a criterion whose step segmentation did
    not find is searched for over every step from the first to the last of `search_steps`;
    an `after_step` criterion (4e) may happen after its own step, so not_performed does not decide it;
  - not_visible criteria: fixed 無法判定 with the rubric's reason;
then aggregates the step verdict (rubric.aggregate) and joins the explanations.

usage: python rules.py segmentation.json [-o grading.json]
"""

import argparse
import json
import re
import sys
from pathlib import Path

from rubric import (CHECK_STEP, CORRECT, NOT_APPLICABLE, PENDING, SOURCE_FOR, STEP_COUNT, UNDETERMINED, WRONG, aggregate,
                    criteria_by_id, fmt_time, load_rubric, search_range, contact_order, wash_end, wrong_with_links)

WINDOW_MARGIN = 2.0  # seconds added on both sides of a visual criterion's window
CHECK_MARGIN = 10.0  # a segment_check entry is a tagged moment that may be a few seconds off: look wider


def grade_from_segmentation(seg: dict, rubric: dict | None = None,
                            margin: float = WINDOW_MARGIN) -> dict:
    rubric = rubric or load_rubric()
    duration = seg["meta"]["duration"]
    entries: dict[int, list[dict]] = {}
    for entry in seg["steps"]:
        entries.setdefault(entry["step_id"], []).append(entry)

    steps = []
    for step in rubric["steps"]:
        search_step = any(c.get("search_steps") for c in step["criteria"])
        criteria = [_grade(c, step["step_id"], entries, duration, margin, search_step) for c in step["criteria"]]
        steps.append(build_step(step, criteria))
    return {"video_id": seg["video_id"], "rubric_version": rubric["version"],
            "steps": link_wrong(steps, rubric)}


_MMSS = re.compile(r"\d{2}:\d{2}(?:\.\d+)?")


def link_wrong(steps: list[dict], rubric: dict, *, segmentation: dict | None = None) -> list[dict]:
    """Doing an order backwards fails every related step (DECISIONS D-00-03, D-05-03): a criterion with
    `wrong_with` (17a with 15b, 27a with 25b, 7a with 8b) becomes 錯誤 when that criterion of another
    step is 錯誤, and its step is aggregated again. A criterion still 待判定 is left for the grading
    agent; once graded, the next call links it."""
    links = wrong_with_links(rubric)
    if not links:
        return steps
    specs = criteria_by_id(rubric)
    graded = {c["criterion_id"]: c for s in steps for c in s["criteria"]}
    by_id = {s["step_id"]: s for s in rubric["steps"]}
    out = []
    for step in steps:
        criteria, changed = [], False
        for c in step["criteria"]:
            src = graded.get(links.get(c["criterion_id"], ""))
            if src is not None and src["verdict"] == WRONG and c["verdict"] not in (WRONG, PENDING):
                c, changed = _linked_wrong(c, src, specs[src["criterion_id"]]), True
            criteria.append(c)
        out.append(build_step(by_id[step["step_id"]], criteria, segmentation=segmentation) if changed else step)
    return out


def _linked_wrong(c: dict, src: dict, spec: dict) -> dict:
    when = _MMSS.search(src["explanation"])
    note = (f"{spec['id']}「{spec['text']}」錯誤" + (f"（{when.group(0)} 起）" if when else "")
            + f"：順序做反，相關步驟全部錯誤，程式把 {c['criterion_id']} 改判錯誤")
    return {**c, "verdict": WRONG, "reason": None, "evidence": c["evidence"] or list(src["evidence"]),
            "explanation": c["explanation"].rstrip("。") + "。" + note + "。"}


def build_step(step: dict, criteria: list[dict], *, segmentation: dict | None = None) -> dict:
    """Step entry from graded criteria; also used after the grading agent fills visual criteria."""
    if step.get("removed"):
        return {"step_id": step["step_id"], "verdict": NOT_APPLICABLE,
                "explanation": "評分表已移除此步驟，不評分。", "confidence": 1.0, "criteria": []}
    if segmentation is not None and step["step_id"] == 1 and any(
        c["id"] == "1c" and c["category"] == "program" for c in step["criteria"]
    ):  # 1c was a program check of the segmentation's step starts before rubric wash_before_contact
        end = next((wash_end(c) for c in criteria if c["criterion_id"] == "1a"), None)
        if end is not None:
            later = [e for e in segmentation["steps"] if e["step_id"] >= 2]
            criteria = [_wash_before_later_steps(end, later) if c["criterion_id"] == "1c" else c
                        for c in criteria]
    if step["step_id"] == 1:
        criteria = _order_from_contacts(step, criteria)
    texts = {c["id"]: c["text"] for c in step["criteria"]}
    verdict = aggregate(step, {c["criterion_id"]: c["verdict"] for c in criteria},
                        reasons={c["criterion_id"]: c.get("reason") for c in criteria})
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


def searched(c: dict, entries: list[dict]) -> bool:
    """The criterion is looked for over its search range: it has search_steps and segmentation did not find
    its step (`entries` are that step's segmentation entries). The grading service uses this too."""
    return bool(c.get("search_steps")) and _status(entries) != "detected"


def search_note(sid: int, steps: list[int]) -> str:
    """How the window was chosen; criteria/10-presence.md keys off 「切分未找到步驟 N」."""
    return f"切分未找到步驟 {sid}，要在整個區間（步驟 {steps[0]}–{steps[-1]} 之間）內找"


def _order_from_contacts(step: dict, criteria: list[dict]) -> list[dict]:
    """1c (wash_before_contact) is graded in a task of its own, which does not know when 1a measured the
    end of washing: compare its contact points with that end here. 1a 無法判定 -> 1c 無法判定."""
    spec = next((c for c in step["criteria"] if c["id"] == "1c"), None)
    rule = (spec or {}).get("rule", {})
    graded = {c["criterion_id"]: c for c in criteria}
    order, wash = graded.get("1c"), graded.get("1a")
    if (rule.get("type") != "wash_before_contact" or not order or order.get("source") != "vlm"
            or order["verdict"] not in (CORRECT, WRONG) or not wash or wash["verdict"] == PENDING):
        return criteria
    end = wash_end(wash)
    if end is None:
        fixed = {**order, "verdict": UNDETERMINED, "reason": "資訊不足",
                 "explanation": order["explanation"].rstrip("。") + "。1a 沒有量到洗手結束時間，無法比較接觸順序。"}
    else:
        expected = contact_order(order.get("times", []), end, rule["tolerance_seconds"])
        if expected == order["verdict"]:
            return criteria
        fixed = {**order, "verdict": expected,
                 "explanation": order["explanation"].rstrip("。") + f"。程式以 1a 量到的洗手結束 {fmt_time(end)}"
                                f"（容許 {rule['tolerance_seconds']:g} 秒）比較接觸時間，判{expected}。"}
    return [fixed if c is order else c for c in criteria]


def _wash_before_later_steps(wash_end: float | None, later: list[dict]) -> dict:
    evidence = ["seg:step 1"]
    first = min(_detected(later), key=lambda e: (e["start"], e["step_id"]), default=None)
    if wash_end is not None and first is not None:
        sid, start = first["step_id"], first["start"]
        evidence.append(f"seg:step {sid}")
        verdict = WRONG if start < wash_end else CORRECT
        detail = f"洗手於 {fmt_time(wash_end)}（{wash_end:g} 秒）結束，最早的後續步驟 {sid} 於 {fmt_time(start)}（{start:g} 秒）開始。"
        reason = None
    elif wash_end is not None and {e["step_id"] for e in later if e["status"] == "not_performed"} == set(range(2, STEP_COUNT + 1)):
        verdict, detail, reason = CORRECT, f"洗手於 {fmt_time(wash_end)} 結束，未執行任何後續步驟。", None
    else:
        verdict, detail, reason = UNDETERMINED, "無法定位洗手結束或後續步驟開始。", "切分未定位"
    return _criterion({"id": "1c", "category": "program"}, verdict, detail, evidence=evidence,
                      reason=reason, confidence=0.0 if reason else (first["confidence"] if first else 1.0))


def _grade(c: dict, sid: int, entries: dict[int, list[dict]], duration: float, margin: float,
           search_step: bool = False) -> dict:
    tagged = entries.get(CHECK_STEP, [])
    if not c.get("segment_check") and any("check" in e for e in tagged):
        # A tagged step 4 (issue #196): only 4d/4e (segment_check) use the diluent and package rows. For 4a/4b
        # they say nothing about reading the sheet, and for 5a/7a/8a, whose search ranges include step 4, their
        # logical windows (vial out → package torn) would widen the window; everyone else sees the 讀單 rows.
        entries = {**entries, CHECK_STEP: [e for e in tagged if e.get("check", "讀單") == "讀單"]}
    own = entries.get(sid, [])
    status = _status(own)
    seg_ref = [f"seg:step {sid}"]

    if c["category"] == "not_visible":
        return _criterion(c, UNDETERMINED, f"{c['note']}，無法判定。", confidence=1.0,
                          reason=c["reason"])

    if c.get("rule", {}).get("type") == "wash_before_later_steps" and status != "not_performed":
        wash = _detected(own)
        later = [e for step_id, items in entries.items() if step_id >= 2 for e in items]
        return _wash_before_later_steps(wash[0]["end"] if wash else None, later)

    if status == "not_performed" and not ((search_step or c.get("after_step")) and c["category"] == "visual"):
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
        return _criterion(c, CORRECT, f"切分結果：步驟 {sid} 於 {_spans_text(found)}（{_seg_evidence(found)}）。",
                          evidence=seg_ref, confidence=conf)

    if c.get("segment_check"):
        checked = _check_result(c, sid, own, duration, margin, seg_ref)
        if checked:
            return checked
    search = searched(c, own)
    window_steps = search_range(c) if search else c["window_steps"]
    window = _window(window_steps, entries, duration, margin)
    if window is None:
        steps = f"{window_steps[0]}–{window_steps[-1]}" if search else "、".join(map(str, window_steps))
        return _criterion(c, UNDETERMINED, f"切分結果未定位步驟 {steps}，無法取得要看的畫面區間。",
                          source="rule", evidence=seg_ref, reason="切分未定位")
    if c.get("rule", {}).get("type") == "wash_before_contact":
        window[0] = 0.0  # contacts before washing count too: look from the start of the video
    where = f"{search_note(sid, window_steps)}；" if search else ""
    return _criterion(c, PENDING, f"等待評分 agent 看畫面判定（{where}區間 {fmt_time(window[0])}–{fmt_time(window[1])}）。",
                      window=window)


def _check_result(c: dict, sid: int, own: list[dict], duration: float, margin: float,
                  seg_ref: list[str]) -> dict | None:
    """A criterion with `segment_check` (4d, 4e) graded from the step-4 entries the segmentation tagged with
    that `check` (issue #196): finding the moment is the segmentation's job, the grading agent only looks at it.
      detected → 待判定, window = those entries ± CHECK_MARGIN;
      all not_performed → 錯誤 (the segmentation looked and saw no such check);
      any not_visually_determinable → 無法判定 (切分未定位).
    None when no entry carries that check (an older segmentation): the caller falls back to window_steps."""
    check = c["segment_check"]
    rows = [e for e in own if e.get("check") == check]
    if not rows:
        return None
    found = _detected(rows)
    if found:
        pad = max(margin, CHECK_MARGIN)
        start, end = min(e["start"] for e in found), max(e["end"] for e in found)
        window = [round(max(0.0, start - pad), 2), round(min(duration, end + pad), 2)]
        return _criterion(c, PENDING, f"等待評分 agent 看畫面判定（切分步驟 {sid}「{check}」段，"
                                      f"區間 {fmt_time(window[0])}–{fmt_time(window[1])}）。", window=window)
    conf = max(e["confidence"] for e in rows)
    if all(e["status"] == "not_performed" for e in rows):  # any unclear row → 無法判定, not 錯誤
        return _criterion(c, WRONG, f"切分結果：步驟 {sid}「{check}」未執行（{_seg_evidence(rows)}）。",
                          source="rule", evidence=seg_ref, confidence=conf)
    return _criterion(c, UNDETERMINED, f"切分結果：步驟 {sid}「{check}」無法定位（{_seg_evidence(rows)}）。",
                      source="rule", evidence=seg_ref, reason="切分未定位")


def _window(step_ids: list[int], entries: dict[int, list[dict]], duration: float,
            margin: float) -> list[float] | None:
    """From the earliest start to the latest end of the detected spans and logical windows of `step_ids`,
    padded by `margin`; a step split in two also covers whatever happened between its entries."""
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
