"""Compare grading systems ("arms") on the same videos against the label sheet (no model call).

usage: python compare_arms.py --labels LABELS.csv --arm NAME=RECORD_FOLDER [--arm ...]
           [--group NAME=VID_0091,VID_0095 ...] [--corrections FIX.json] [--exclude-steps 2,3,4,29]
           [--runs-dir DIR] [--json out.json]

An arm is a record folder name under runs/VID_*/ (e.g. the tool-using agent's
rubric1.12_skill0.22_gemini-4-argon_seg-skill1.4.7_gemini-4-argon and sop_agentic_cli's
rubric1.12_skillagentic0.1-e2e-s0.22_gemini-4-argon-agentic_seg-agentic-e2e). Each run of an arm is
runN/grading.json when the run kept one (sop_agentic_cli does), else the record's grading.json as run 1.
Only videos every arm graded are compared. Labels are read like eval_grading.py; --corrections is a JSON
{"VID_0095": {"10": "部分正確"}} of label verdicts a person corrected (kept in the gitignored review/).
--exclude-steps leaves those steps out of every number (e.g. 2,3,4,29: the old videos did not film the
medication record sheet, so the label sheet and the models disagree on what is visible).

Prints, on run 1 of every arm:
  - per arm and group: agreement, error recall and false alarms (eval_grading.summarize), all / V / P steps;
  - paired with the first arm: steps only it got right vs only the other got right, with an exact McNemar
    p-value (two-sided binomial; few videos, so read it as a hint, not a verdict);
  - every step where the arms disagree with each other or all miss the label;
and per arm: run-to-run verdict flips (videos with two or more runs), input / output tokens per video
(the tool-using agent's segmentation tokens are counted from the segmentation transcript, following
segmentation_from when it was copied) and seconds per video (record usage wall_seconds when present,
else the sum of the transcripts' per-turn model seconds).
"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

from eval_grading import DEFAULT_EXCLUDE, compare, load_labels, summarize

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


def arm_runs(runs_dir: Path, folder: str) -> dict[str, list[dict]]:
    """{video_id: [grading of run 1, run 2, ...]} of one arm."""
    found = {}
    for record in sorted(runs_dir.glob(f"VID_*/{folder}/record.json")):
        base = record.parent
        kept = sorted(base.glob("run*/grading.json"), key=lambda p: int(p.parent.name[3:]))
        paths = kept or ([base / "grading.json"] if (base / "grading.json").exists() else [])
        if paths:
            found[base.parent.name] = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    return found


def _transcript_cost(paths) -> tuple[int, int, float]:
    tin = tout = 0
    secs = 0.0
    for path in paths:
        for turn in json.loads(path.read_text(encoding="utf-8")):
            tin += turn.get("input_tokens", 0)
            tout += turn.get("output_tokens", 0)
            secs += turn.get("seconds", 0.0)
    return tin, tout, secs


def run_cost(runs_dir: Path, folder: str, video_id: str) -> dict:
    """Cost of run 1: grading usage + segmentation (when the run or its source record segmented)."""
    base = runs_dir / video_id / folder
    record = json.loads((base / "record.json").read_text(encoding="utf-8"))
    run = record["runs"][0]
    grading_in, grading_out = run["usage"]["input_tokens"], run["usage"]["output_tokens"]
    _, _, grading_secs = _transcript_cost(sorted((base / "run1" / "grading").glob("transcript_task_*.json")))
    seg = base / "run1" / "seg" / "transcript.json"
    source = record.get("segmentation_from")
    while not seg.exists() and source:  # copied: count the record that paid for it
        seg = runs_dir / video_id / source["record"] / f"run{source['run']}" / "seg" / "transcript.json"
        source = source.get("copied_from")
    seg_in, seg_out, seg_secs = _transcript_cost([seg]) if seg.exists() else (0, 0, 0.0)
    wall = run["usage"].get("wall_seconds")
    return {"input": grading_in + seg_in, "output": grading_out + seg_out, "segmentation_input": seg_in,
            "seconds": wall if wall is not None else round(grading_secs + seg_secs, 1),
            "seconds_kind": "wall" if wall is not None else "model"}


def mcnemar(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value for b vs c discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def flips(runs: list[dict], skip: set[int] = frozenset()) -> tuple[int, int]:
    """(steps whose verdict differs between any two runs, steps compared), without the `skip` steps."""
    if len(runs) < 2:
        return 0, 0
    verdicts = [{s["step_id"]: s["verdict"] for s in g["steps"] if s["step_id"] not in skip} for g in runs]
    common = set.intersection(*(set(v) for v in verdicts))
    return sum(len({v[sid] for v in verdicts}) > 1 for sid in common), len(common)


def _line(s: dict) -> str:
    return (f"{s['agreement']}/{s['compared']}（錯誤召回 {s['error_recall']}，誤報 {s['false_alarms']}）"
            if s["compared"] else "—")


def report(labels: dict, arms: dict[str, dict[str, list[dict]]], groups: dict[str, list[str]],
           costs: dict[str, dict[str, dict]], skip: set[int] = frozenset()) -> tuple[str, dict]:
    names = list(arms)
    videos = sorted(set.intersection(*(set(a) for a in arms.values())) & set(labels))
    rows = {n: {v: {r["step_id"]: r for r in compare(arms[n][v][0], labels[v])} for v in videos} for n in names}
    groups = {g: [v for v in vids if v in videos] for g, vids in groups.items()} or {"全部": videos}
    out, data = [f"比對影片（{len(videos)} 支）：{', '.join(videos)}"
                 + (f"；排除步驟 {','.join(map(str, sorted(skip)))}" if skip else ""), ""], {"videos": videos, "arms": {}}

    out += ["## 一致率（run 1）", "", "| 組 | 影片組 | 全部步驟 | 看畫面（V） | 程式（P） |", "|---|---|---|---|---|"]
    for n in names:
        data["arms"][n] = {}
        for g, vids in groups.items():
            flat = [r for v in vids for r in rows[n][v].values()]
            s = {k: summarize([r for r in flat if k == "all" or r["kind"] == k]) for k in ("all", "V", "P")}
            data["arms"][n][g] = s
            out.append(f"| {n} | {g} | {_line(s['all'])} | {_line(s['V'])} | {_line(s['P'])} |")

    first = names[0]
    out += ["", f"## 成對比較（對 {first}，run 1，V 步驟）", "", "| 組 | 只有它對 | 只有對照對 | McNemar p |",
            "|---|---|---|---|"]
    for n in names[1:]:
        pairs = [(rows[n][v][sid]["match"], rows[first][v][sid]["match"]) for v in videos
                 for sid, r in rows[first][v].items() if r["kind"] == "V" and sid in rows[n][v]]
        b, c = sum(x and not y for x, y in pairs), sum(y and not x for x, y in pairs)
        data["arms"][n]["paired"] = {"only_this": b, "only_first": c, "p": mcnemar(b, c)}
        out.append(f"| {n} | {b} | {c} | {mcnemar(b, c):.3f} |")

    out += ["", "## 穩定性與成本", "", "| 組 | run 間判定翻轉 | input／支 | output／支 | 秒／支 |", "|---|---|---|---|---|"]
    for n in names:
        f = [flips(arms[n][v], skip) for v in videos]
        changed, compared = sum(x for x, _ in f), sum(y for _, y in f)
        cs = [costs[n][v] for v in videos]
        avg = lambda k: sum(c[k] for c in cs) / len(cs) if cs else 0  # noqa: E731
        kind = "、".join(sorted({c["seconds_kind"] for c in cs}))
        data["arms"][n]["flips"] = {"changed": changed, "compared": compared}
        data["arms"][n]["cost"] = {"input": avg("input"), "output": avg("output"), "seconds": avg("seconds"),
                                   "segmentation_input": avg("segmentation_input"), "seconds_kind": kind}
        out.append(f"| {n} | {f'{changed}/{compared}' if compared else '（只有 1 次）'} | {avg('input'):,.0f}"
                   f"（切分 {avg('segmentation_input'):,.0f}） | {avg('output'):,.0f} | {avg('seconds'):,.0f}（{kind}） |")

    out += ["", "## 不一致的步驟（各組判定不同，或都和標記表不同）", "",
            "| 影片 | 步驟 | 類型 | 標記表 | " + " | ".join(names) + " |", "|---" * (4 + len(names)) + "|"]
    data["disagreements"] = []
    for v in videos:
        for sid, r in sorted(rows[first][v].items()):
            got = {n: rows[n][v].get(sid, {}).get("model") for n in names}
            if len(set(got.values())) > 1 or all(g != r["human"] for g in got.values()):
                data["disagreements"].append({"video_id": v, "step_id": sid, "kind": r["kind"],
                                              "human": r["human"], **got})
                out.append(f"| {v} | {sid} | {r['kind']} | {r['human']} | "
                           + " | ".join(f"{g}{'' if g == r['human'] else ' ✗'}" for g in got.values()) + " |")
    kinds = Counter(d["kind"] for d in data["disagreements"])
    out += ["", f"共 {len(data['disagreements'])} 步（V {kinds['V']}、P {kinds['P']}）"]
    return "\n".join(out), data


def apply_corrections(labels: dict, path: Path | None) -> None:
    if not path:
        return
    for vid, steps in json.loads(path.read_text(encoding="utf-8")).items():
        for sid, verdict in steps.items():
            if vid in labels and int(sid) in labels[vid]:
                labels[vid][int(sid)]["verdict"] = verdict


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--arm", action="append", required=True, help="NAME=RECORD_FOLDER; the first is the reference")
    ap.add_argument("--group", action="append", default=[], help="NAME=VID_0001,VID_0002 (reported separately)")
    ap.add_argument("--corrections", type=Path, help="JSON of corrected label verdicts")
    ap.add_argument("--exclude", default=",".join(sorted(DEFAULT_EXCLUDE)))
    ap.add_argument("--exclude-steps", default="", help="comma-separated steps left out of every number")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv[1:])
    labels = load_labels(a.labels, {v for v in a.exclude.split(",") if v})
    apply_corrections(labels, a.corrections)
    skip = {int(s) for s in a.exclude_steps.split(",") if s.strip()}
    labels = {v: {sid: x for sid, x in steps.items() if sid not in skip} for v, steps in labels.items()}
    arms = {}
    costs = {}
    for spec in a.arm:
        name, _, folder = spec.partition("=")
        arms[name] = arm_runs(a.runs_dir, folder)
        costs[name] = {v: run_cost(a.runs_dir, folder, v) for v in arms[name]}
        if not arms[name]:
            ap.error(f"no records named {folder} in {a.runs_dir}")
    groups = {g.partition("=")[0]: g.partition("=")[2].split(",") for g in a.group}
    text, data = report(labels, arms, groups, costs, skip)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if a.json:
        a.json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
