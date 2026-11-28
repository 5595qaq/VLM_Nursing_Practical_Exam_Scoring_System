"""Compare grading results with the human label sheet (no model call).

usage: python eval_grading.py --labels LABELS.csv GRADING.json [GRADING.json ...] [--json out.json]

The label sheet is the CSV export of 影片標記表 (gitignored; it contains student names). Columns used:
  B 步驟編號, D 判定, E 問題說明, F 問題類型, G 開始秒數, H 結束秒數, O 影片ID, P 備注.
G/H are mm:ss; several spans are written 01:47/02:49 in G and 01:51/02:50 in H (paired by position;
different counts in G and H, or a time that cannot be read, leave the step without a label time).
The human explanation shown next to the model's is E + F + P joined (most rows only fill F,
e.g. 步驟遺漏). Rows are skipped when the verdict is blank or 不適用, when E mentions "AI判定"
(not a human judgement), or when the video is in --exclude or UNUSABLE_VIDEOS (always skipped). Steps the current rubric removes (11, and 16 and 21
since rubric 1.25) are not compared, whatever the label says, also in records graded before.

Prints a per-step table for each video and a summary, overall and for the steps that have a visual
criterion (類型 V). Steps with only program criteria (類型 P) are decided from the segmentation, so
when it was built from the label times they say nothing about the grading agent:
  agreement   model verdict == human verdict
  error recall  human 錯誤/部分正確 that the model also marked 錯誤/部分正確
  false alarms  human 正確 that the model marked 錯誤/部分正確
  time          the model's times of the step against the label times: they match when a pair overlaps or the
                starts differ by at most 3 s, and a label time with no model time at all is a mismatch (a step
                whose time does not match is wrong even when the verdicts agree). The model's times are the
                grading agent's own criteria, 待判定 aside: a timed criterion's span, else its times field, else (older
                records, empty times) the mm:ss cited in the explanation (times after clip@a-b are clip-relative and become a + t, after a 片段內 with no clip@ dropped; a lone mm:ss is a
                point), or, when it cited none,
                the segmentation's detected spans (a step without one uses its logical_window). The
                segmentation is the segmentation.json next to the grading file or one folder up
                (runs/<video>/<version>/runN/grading.json), used only when its video_id is the grading's;
                without it, or without a label time, the time is not checked (–) and only the verdict counts.
  strict        agreement with time mismatches counted as wrong
"""
import argparse
import csv
import functools
import json
import re
import sys
from collections import Counter
from pathlib import Path

from rubric import load_rubric

DEFAULT_EXCLUDE = {"VID_0061", "VID_0062", "VID_0004", "VID_0008"}  # missing explanation / AI判定
# Never compared or reviewed, whatever --exclude says: the video file itself is broken (AGENTS.md 已知有問題的影片,
# issue #148). VID_0065 turns 90° at 03:00 until the end.
UNUSABLE_VIDEOS = {"VID_0065"}
# a video that ends early: steps after this one are not recorded and never counted (AGENTS.md, issue #148)
LAST_RECORDED_STEP = {"VID_0016": 22}
# AGENTS.md「影片分組」(暫定, user 2026-11-11): rules are tuned on these; every other labelled video is the test set
TUNING_SET = {"VID_0001", "VID_0008", "VID_0010", "VID_0011", "VID_0014", "VID_0018", "VID_0020", "VID_0030",
              "VID_0072", "VID_0073", "VID_0074", "VID_0076", "VID_0081", "VID_0083", "VID_0087", "VID_0089",
              "VID_0091", "VID_0095", "VID_0096", "VID_0097", "VID_0098", "VID_0099", "VID_0100", "VID_0103",
              "VID_0106", "VID_0111", "VID_0118", "VID_0119", "VID_0123", "VID_0134"}
COMPARED = {"正確", "錯誤", "部分正確", "無法判定"}
FAULTS = {"錯誤", "部分正確"}
TIME_SLACK = 3.0  # seconds between starts that still count as the same span
_TIME_RE = re.compile(r"^(\d+):(\d{0,2}(?:\.\d+)?)$")
_CITED = r"(\d{2}):(\d{2}(?:\.\d+)?)"
_CITED_RE = re.compile(_CITED + r"(?:\s*(?:[–\-~～至到])\s*" + _CITED + r")?")  # a range or a point
_CLIP_TIME_RE = re.compile(r"片段(?:內[^\d]{0,3}|\s*)" + _CITED_RE.pattern)  # time inside a clip, not the video
# times after clip@A-B and a short word (「clip@105.5-113.5 的 00:02.5–00:07.9 及 00:09」) are A + t in the video
_CLIP_AT_RE = re.compile(r"clip@(?P<a>\d+(?:\.\d+)?)-(?P<b>\d+(?:\.\d+)?)[^\d@（()），,。；;]{0,8}?" + _CITED_RE.pattern
                         + r"(?:\s*[、及和與]\s*" + _CITED_RE.pattern + r")*")
_CLIP_LIST_RE = re.compile(_CLIP_TIME_RE.pattern + r"(?:\s*[、及和與]\s*" + _CITED_RE.pattern + r")*")
_PAREN_RE = re.compile(r"[（(][^（()）]*[）)]")  # a list counts only in parentheses: 「（clip@a-b 片段內 A 及 B）」



def parse_time(text: str) -> float | None:
    """mm:ss or mm:ss.s; tolerates the sheet's typos 00;47, 08：59 and 06: (read as 06:00)."""
    m = _TIME_RE.match(text.strip().replace("：", ":").replace(";", ":"))
    if not m or float(m.group(2) or 0) >= 60:
        return None
    return int(m.group(1)) * 60 + float(m.group(2) or 0)


def parse_spans(start: str, end: str) -> list[tuple[float, float]]:
    """Label spans from columns G/H; a/b in both is two spans paired by position. Different counts or an
    unreadable cell give [] (no label time) rather than a guessed span."""
    starts = [parse_time(t) for t in start.replace("／", "/").split("/") if t.strip()]
    ends = [parse_time(t) for t in end.replace("／", "/").split("/") if t.strip()]
    if not starts or len(starts) != len(ends) or None in starts or None in ends:
        return []
    return [(min(a, b), max(a, b)) for a, b in zip(starts, ends)]


def spans_match(model: list[tuple[float, float]], human: list[tuple[float, float]]) -> bool:
    return any(ms <= he and hs <= me or abs(ms - hs) <= TIME_SLACK for ms, me in model for hs, he in human)


def clip_relative(m: re.Match, a: float, b: float, marked: bool) -> bool:
    """Whether a time cited after clip@a-b is inside the clip (see cited_spans); review_page.py has the same rule."""
    start = int(m.group(1)) * 60 + float(m.group(2))
    end = int(m.group(3)) * 60 + float(m.group(4)) if m.group(3) else start
    return marked or (max(start, end) <= b - a + 0.5 and not a - 1 <= start <= b + 1)


def cited_spans(text: str) -> list[tuple[float, float]]:
    """mm:ss ranges (–, -, ~, 至, 到) and points an explanation cites, in text order. Times after clip@A-B
    (「clip@27-35 片段內 00:02.5–00:05.0 及 00:06」) are relative to that clip and become A + t when 片段 follows
    the clip, or when the time fits in the clip's length and is not inside A-B as a video time (「clip@105.5-113.5
    的 00:02.5」); otherwise it is a video time (「clip@68-100 中：01:08.0」). Times after 片段內
    with no clip@ before it are dropped, with the times listed after them inside a parenthesis. A time with
    seconds >= 60 is a typo and dropped."""
    def blank(m):  # keeps positions, so the spans stay in text order
        return " " * len(m.group())

    found = []

    def add(m, pos, base=0.0):
        if float(m.group(2)) < 60 and (not m.group(3) or float(m.group(4)) < 60):
            a = base + int(m.group(1)) * 60 + float(m.group(2))
            b = base + int(m.group(3)) * 60 + float(m.group(4)) if m.group(3) else a
            found.append((pos, (min(a, b), max(a, b))))

    for at in _CLIP_AT_RE.finditer(text):
        head = text.index(at.group(1), at.start()) + len(at.group(1))  # skip the clip's own A-B
        tail = text[head:at.end()]
        skip = len(tail) - len(tail.lstrip("-0123456789."))
        a, b = float(at.group("a")), float(at.group("b"))
        for m in _CITED_RE.finditer(tail, skip):
            add(m, head + m.start(), a if clip_relative(m, a, b, "片段" in at.group()) else 0.0)
    rest = _CLIP_AT_RE.sub(blank, text)
    rest = _CLIP_TIME_RE.sub(blank, _PAREN_RE.sub(lambda m: _CLIP_LIST_RE.sub(blank, m.group()), rest))
    for m in _CITED_RE.finditer(rest):
        add(m, m.start())
    return [span for _, span in sorted(found, key=lambda f: f[0])]


def agent_spans(step: dict) -> list[tuple[float, float]]:
    """Times of the criteria the grading agent decided: a timed criterion's span, else its times, else the times
    cited in the explanation (records before skill 0.39, and 無法判定 with empty times). A contact check (1c,
    rule wash_before_contact) adds nothing: the label's time for step 1 is the washing, while 1c's times are
    contact points (a vial pickup next to the washing would count as a match), its explanation cites the whole
    stretch before washing and a 無法判定 lists what it watched."""
    spans = set()
    for c in step.get("criteria", []):
        if c.get("source") == "vlm" and c["verdict"] != "待判定":
            if c.get("criterion_id") in contact_checks():
                continue
            if c.get("span"):
                spans.add(tuple(c["span"]))
            else:
                spans.update(map(tuple, c["times"]) if c.get("times") else cited_spans(c["explanation"]))
    return sorted(spans)


@functools.cache
def contact_checks() -> frozenset[str]:
    return frozenset(c["id"] for s in load_rubric()["steps"] for c in s["criteria"]
                     if c.get("rule", {}).get("type") == "wash_before_contact")


def segmentation_spans(segmentation: dict) -> dict[int, list[tuple[float, float]]]:
    """Detected spans per step; a step with none falls back to its logical windows (what the grading
    agent is shown for it, e.g. step 29)."""
    detected: dict[int, list[tuple[float, float]]] = {}
    windows: dict[int, list[tuple[float, float]]] = {}
    for e in segmentation["steps"]:
        if e["status"] == "detected" and e.get("start") is not None and e.get("end") is not None:
            detected.setdefault(e["step_id"], []).append((e["start"], e["end"]))
        elif e.get("logical_window"):
            windows.setdefault(e["step_id"], []).append(tuple(e["logical_window"]))
    return {**windows, **detected}


def find_segmentation(grading_path: Path, video_id: str) -> tuple[dict | None, str]:
    """(segmentation, note): the segmentation.json next to the grading file or one folder up, when it is
    for the same video; otherwise None and why the time is not checked."""
    for folder in (grading_path.parent, grading_path.parent.parent):
        path = folder / "segmentation.json"
        if path.is_file():
            segmentation = json.loads(path.read_text(encoding="utf-8"))
            if segmentation.get("video_id") != video_id:
                return None, f"{path} 是 {segmentation.get('video_id')} 的切分，只比判定，不比時間"
            return segmentation, ""
    return None, "找不到 segmentation.json，只比判定，不比時間"


def load_labels(path: Path, exclude: set[str]) -> dict[str, dict[int, dict]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = next(i for i, r in enumerate(rows) if len(r) > 1 and r[1] == "步驟編號")
    labels: dict[str, dict[int, dict]] = {}
    for r in rows[header + 1:]:
        if len(r) < 15 or not r[1].strip().isdigit():
            continue
        vid, verdict = r[14].strip(), r[3].strip()
        if vid in exclude or vid in UNUSABLE_VIDEOS or verdict not in COMPARED or "AI判定" in r[4]:
            continue
        note = r[15].strip() if len(r) > 15 else ""
        why = "；".join(x for x in (r[4].strip(), r[5].strip(), note) if x and x != "無")
        labels.setdefault(vid, {})[int(r[1])] = {"verdict": verdict, "why": why,
                                                  "spans": parse_spans(r[6], r[7])}
    return labels


def visual_steps() -> set[int]:
    return {s["step_id"] for s in load_rubric()["steps"]
            if any(c["category"] == "visual" for c in s["criteria"])}


def removed_steps() -> set[int]:
    return {s["step_id"] for s in load_rubric()["steps"] if s.get("removed")}


def compare(grading: dict, labels: dict[int, dict], segmentation: dict | None = None) -> list[dict]:
    """One row per labelled step. `time_match` is None when the time was not checked (no segmentation or
    no label time); a label time without a model time is False. `time_source` says where the model's
    times came from: agent (agent_spans) or segmentation."""
    visual, removed = visual_steps(), removed_steps()
    seg = segmentation_spans(segmentation) if segmentation else {}
    rows = []
    for step in grading["steps"]:
        human = labels.get(step["step_id"])
        if human is None or step["step_id"] in removed:  # not scored in the current rubric, even in older records
            continue
        cited = agent_spans(step) if segmentation else []
        model_spans = cited or seg.get(step["step_id"], [])
        human_spans = human.get("spans", [])
        time_match = spans_match(model_spans, human_spans) if segmentation and human_spans else None
        match = human["verdict"] == step["verdict"]
        rows.append({"step_id": step["step_id"], "kind": "V" if step["step_id"] in visual else "P",
                     "human": human["verdict"], "human_why": human["why"],
                     "model": step["verdict"], "model_why": step["explanation"],
                     "match": match, "human_spans": human_spans, "model_spans": model_spans,
                     "time_source": "agent" if cited else "segmentation" if model_spans else None,
                     "time_match": time_match, "strict_match": match and time_match is not False,
                     "review": bool(step.get("needs_review"))})
    return rows


def summarize(rows: list[dict]) -> dict:
    faults = [r for r in rows if r["human"] in FAULTS]
    correct = [r for r in rows if r["human"] == "正確"]
    return {
        "compared": len(rows),
        "agreement": sum(r["match"] for r in rows),
        "strict_agreement": sum(r.get("strict_match", r["match"]) for r in rows),
        "time_mismatch": [r["step_id"] for r in rows if r["match"] and r.get("time_match") is False],
        "error_recall": f"{sum(r['model'] in FAULTS for r in faults)}/{len(faults)}",
        "false_alarms": f"{sum(r['model'] in FAULTS for r in correct)}/{len(correct)}",
        "confusion": dict(Counter(f"{r['human']}→{r['model']}" for r in rows if not r["match"])),
        "needs_review": [r["step_id"] for r in rows if r.get("review")],
    }


def _short(text: str, limit: int = 140) -> str:
    text = text.replace("\n", " ").replace("|", "/")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _mmss(t: float) -> str:
    t = round(t, 1)
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _spans(spans: list[tuple[float, float]]) -> str:
    return "、".join(f"{_mmss(a)}–{_mmss(b)}" for a, b in spans) or "無"


def _line(s: dict) -> str:
    line = (f"一致 {s['strict_agreement']}/{s['compared']}（只比判定 {s['agreement']}/{s['compared']}）；"
            f"錯誤召回 {s['error_recall']}；誤報 {s['false_alarms']}；不一致：{s['confusion'] or '無'}")
    if s["time_mismatch"]:
        line += f"；判定一致但時間對不上：步驟 {'、'.join(map(str, s['time_mismatch']))}"
    if s["needs_review"]:
        line += f"；需人工複核 {len(s['needs_review'])} 步"
    return line


def _totals(rows: list[dict]) -> str:
    return ("全部：" + _line(summarize(rows)) + "\n看畫面的步驟（V）："
            + _line(summarize([r for r in rows if r["kind"] == "V"])))


def render(video_id: str, rows: list[dict], note: str = "") -> str:
    out = [f"## {video_id}", "", *([f"（{note}）", ""] if note else []), "| 步驟 | 類型 | 人工 | 模型 | 一致 | 時間（人工／模型） | 人工說明 | 模型說明 |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        model = r["model"] + ("（需人工複核）" if r.get("review") else "")
        mark = {True: "✓", False: "✗", None: "–"}[r.get("time_match")]
        source = "（agent）" if r.get("time_source") == "agent" else ""
        times = f"{mark} {_spans(r.get('human_spans', []))}／{_spans(r.get('model_spans', []))}{source}"
        out.append(f"| {r['step_id']} | {r['kind']} | {r['human']} | {model} | {'✓' if r['match'] else '✗'} | "
                   f"{times} | {_short(r['human_why'], 60)} | {_short(r['model_why'])} |")
    out += ["", _totals(rows), ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("grading", nargs="+", type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--exclude", default=",".join(sorted(DEFAULT_EXCLUDE)),
                    help="comma-separated video ids to skip (UNUSABLE_VIDEOS are skipped too)")
    ap.add_argument("--json", type=Path, help="also write the rows and summaries here")
    a = ap.parse_args(argv[1:])
    labels = load_labels(a.labels, {v for v in a.exclude.split(",") if v})
    sys.stdout.reconfigure(encoding="utf-8")
    report, all_rows = {}, []
    for path in a.grading:
        grading = json.loads(path.read_text(encoding="utf-8"))
        vid = grading["video_id"]
        if vid not in labels:
            print(f"## {vid}\n\n沒有可比對的人工標註（未標記或已排除）\n")
            continue
        segmentation, note = find_segmentation(path, vid)
        rows = compare(grading, labels[vid], segmentation)
        all_rows += rows
        report[vid] = {"rows": rows, "summary": summarize(rows), "time_checked": segmentation is not None,
                       "time_note": note, "visual_summary": summarize([r for r in rows if r["kind"] == "V"])}
        print(render(vid, rows, note))
    if len(report) > 1:
        print("## 合計\n\n" + _totals(all_rows))
    if a.json:
        a.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
