"""Compare grading results with the human label sheet (no model call).

usage: python eval_grading.py --labels LABELS.csv GRADING.json [GRADING.json ...] [--json out.json]

The label sheet is the CSV export of 影片標記表 (gitignored; it contains student names). Columns used:
  B 步驟編號, D 判定, E 問題說明, F 問題類型, O 影片ID, P 備注.
The human explanation shown next to the model's is E + F + P joined (most rows only fill F,
e.g. 步驟遺漏). Rows are skipped when the verdict is blank or 不適用, when E mentions "AI判定"
(not a human judgement), or when the video is in --exclude.

Prints a per-step table for each video and a summary, overall and for the steps that have a visual
criterion (類型 V). Steps with only program criteria (類型 P) are decided from the segmentation, so
when it was built from the label times they say nothing about the grading agent:
  agreement   model verdict == human verdict
  error recall  human 錯誤/部分正確 that the model also marked 錯誤/部分正確
  false alarms  human 正確 that the model marked 錯誤/部分正確
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from rubric import load_rubric

DEFAULT_EXCLUDE = {"VID_0061", "VID_0062", "VID_0004", "VID_0008"}  # missing explanation / AI判定
COMPARED = {"正確", "錯誤", "部分正確", "無法判定"}
FAULTS = {"錯誤", "部分正確"}


def load_labels(path: Path, exclude: set[str]) -> dict[str, dict[int, dict]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = next(i for i, r in enumerate(rows) if len(r) > 1 and r[1] == "步驟編號")
    labels: dict[str, dict[int, dict]] = {}
    for r in rows[header + 1:]:
        if len(r) < 15 or not r[1].strip().isdigit():
            continue
        vid, verdict = r[14].strip(), r[3].strip()
        if vid in exclude or verdict not in COMPARED or "AI判定" in r[4]:
            continue
        note = r[15].strip() if len(r) > 15 else ""
        why = "；".join(x for x in (r[4].strip(), r[5].strip(), note) if x and x != "無")
        labels.setdefault(vid, {})[int(r[1])] = {"verdict": verdict, "why": why}
    return labels


def visual_steps() -> set[int]:
    return {s["step_id"] for s in load_rubric()["steps"]
            if any(c["category"] == "visual" for c in s["criteria"])}


def compare(grading: dict, labels: dict[int, dict]) -> list[dict]:
    visual = visual_steps()
    rows = []
    for step in grading["steps"]:
        human = labels.get(step["step_id"])
        if human is None:
            continue
        rows.append({"step_id": step["step_id"], "kind": "V" if step["step_id"] in visual else "P",
                     "human": human["verdict"], "human_why": human["why"],
                     "model": step["verdict"], "model_why": step["explanation"],
                     "match": human["verdict"] == step["verdict"]})
    return rows


def summarize(rows: list[dict]) -> dict:
    faults = [r for r in rows if r["human"] in FAULTS]
    correct = [r for r in rows if r["human"] == "正確"]
    return {
        "compared": len(rows),
        "agreement": sum(r["match"] for r in rows),
        "error_recall": f"{sum(r['model'] in FAULTS for r in faults)}/{len(faults)}",
        "false_alarms": f"{sum(r['model'] in FAULTS for r in correct)}/{len(correct)}",
        "confusion": dict(Counter(f"{r['human']}→{r['model']}" for r in rows if not r["match"])),
    }


def _short(text: str, limit: int = 140) -> str:
    text = text.replace("\n", " ").replace("|", "/")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _line(s: dict) -> str:
    return (f"一致 {s['agreement']}/{s['compared']}；錯誤召回 {s['error_recall']}；誤報 {s['false_alarms']}；"
            f"不一致：{s['confusion'] or '無'}")


def _totals(rows: list[dict]) -> str:
    return ("全部：" + _line(summarize(rows)) + "\n看畫面的步驟（V）："
            + _line(summarize([r for r in rows if r["kind"] == "V"])))


def render(video_id: str, rows: list[dict]) -> str:
    out = [f"## {video_id}", "", "| 步驟 | 類型 | 人工 | 模型 | 一致 | 人工說明 | 模型說明 |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['step_id']} | {r['kind']} | {r['human']} | {r['model']} | {'✓' if r['match'] else '✗'} | "
                   f"{_short(r['human_why'], 60)} | {_short(r['model_why'])} |")
    out += ["", _totals(rows), ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("grading", nargs="+", type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--exclude", default=",".join(sorted(DEFAULT_EXCLUDE)),
                    help="comma-separated video ids to skip")
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
        rows = compare(grading, labels[vid])
        all_rows += rows
        report[vid] = {"rows": rows, "summary": summarize(rows),
                       "visual_summary": summarize([r for r in rows if r["kind"] == "V"])}
        print(render(vid, rows))
    if len(report) > 1:
        print("## 合計\n\n" + _totals(all_rows))
    if a.json:
        a.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
