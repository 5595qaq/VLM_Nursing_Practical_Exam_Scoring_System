"""Build a segmentation result from the human label sheet's times (skips running sop-video-skill).

usage: python labels_to_segmentation.py --labels LABELS.csv --video-id VID_0083 --duration 424.5 \
           [--box 0,0,1,1] [--hands-box 0,0,1,1] -o segmentation.json

For development: grading then starts from the human time segments instead of the model's.
Only the times are used (columns B 步驟編號, G 開始秒數, H 結束秒數); verdicts and explanations are
never copied, because the grading agent sees the segmentation evidence and must not see the answer.
Per step:
  - start and end filled  -> detected [start, end] (clamped to the video)
  - no times              -> not_visually_determinable, logical_window between the previous step's end
                             and the next step's start, so the agent still looks there (a missing step
                             is the agent's call, e.g. 17/27 flicking)
  - step 11               -> not_performed (removed from the score sheet)
The crop boxes are not in the label sheet: both default to the full frame.
Note: program criteria (step detected / washing >= 15 s) then follow the label times by construction,
so the visual criteria are what measure the grading agent (eval_grading.py reports them separately).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

REMOVED_STEP = 11
FLICK_STEPS = {17: 16, 27: 26}  # flick step -> outer step it is merged with (sop-video-skill 4.7)
FULL_FRAME = [0.0, 0.0, 1.0, 1.0]


def parse_time(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def load_rows(path: Path, video_id: str) -> dict[int, list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return {int(r[1]): r for r in rows if len(r) > 14 and r[14].strip() == video_id and r[1].strip().isdigit()}


def build(rows: dict[int, list[str]], video_id: str, duration: float,
          box=FULL_FRAME, hands_box=FULL_FRAME) -> dict:
    spans = {}
    for sid, r in rows.items():
        start, end = parse_time(r[6]), parse_time(r[7])
        if start is not None and end is not None:
            start, end = sorted((min(start, duration), min(end, duration)))
            spans[sid] = (start, end)

    steps = []
    for sid in range(1, 31):
        entry = {"step_id": sid, "start": None, "end": None, "confidence": 1.0, "evidence": "人工標註時間"}
        if sid == REMOVED_STEP:
            entry.update(status="not_performed", evidence="已自評分表移除")
        elif sid in spans:
            entry.update(status="detected", start=spans[sid][0], end=spans[sid][1])
            if sid in FLICK_STEPS:
                entry.update(handoff="flick_detector", merged_with=[FLICK_STEPS[sid]])
        else:
            before = [spans[s][1] for s in spans if s < sid]
            after = [spans[s][0] for s in spans if s > sid]
            lo, hi = max(before, default=0.0), min(after, default=duration)
            entry.update(status="not_visually_determinable", confidence=0.0,
                         evidence="人工標註沒有時間", logical_window=[lo, hi] if lo < hi else None)
        steps.append(entry)
    return {"video_id": video_id,
            "meta": {"duration": duration,
                     "crop": {"box": list(box), "hands_box": list(hands_box), "rounds": 0,
                              "notes": "由人工標註時間產生，裁切框未設定（整個畫面）"}},
            "steps": steps, "unassigned_spans": [], "observations": [], "zoom_log": []}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--duration", required=True, type=float)
    ap.add_argument("--box", default="0,0,1,1")
    ap.add_argument("--hands-box", default="0,0,1,1")
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv[1:])
    rows = load_rows(a.labels, a.video_id)
    if not rows:
        print(f"no label rows for {a.video_id}", file=sys.stderr)
        return 2
    seg = build(rows, a.video_id, a.duration, [float(v) for v in a.box.split(",")],
                [float(v) for v in a.hands_box.split(",")])
    text = json.dumps(seg, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
