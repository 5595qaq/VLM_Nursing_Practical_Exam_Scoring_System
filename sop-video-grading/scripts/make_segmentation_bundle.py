"""Write a saved-segmentation bundle for `sop_grading_cli --segmentation-file` (grade without segmenting again).

usage: python make_segmentation_bundle.py RECORD_DIR -o bundle.json
           [--replace-step 4 --from EXCERPT.json --note "where the excerpt came from"]

RECORD_DIR is a run record folder (sop-video-grading/runs/<VID>/<version>/) with record.json and
segmentation.json; its whole segmentation is the base. With --replace-step, that step's entries are
replaced by the entries of the same step in EXCERPT.json (a segmentation, or an excerpt with a `steps`
list, of the same video), e.g. a step segmented with newer rules than the rest. Nothing else changes.
The bundle's `provenance` names the record, its segmentation and run, and the excerpt's SHA-256 and note,
so the grading record shows exactly what was graded. The grading CLI validates the result again.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(record_dir: Path, replace_step: int | None = None, excerpt: Path | None = None,
          note: str = "") -> dict:
    record = json.loads((record_dir / "record.json").read_text(encoding="utf-8"))
    segmentation = json.loads((record_dir / "segmentation.json").read_text(encoding="utf-8"))
    if segmentation.get("video_id") != record["video_id"]:
        raise ValueError(f"{record_dir}: segmentation.json is for {segmentation.get('video_id')}, "
                         f"record.json for {record['video_id']}")
    first = record["runs"][0]
    provenance = {
        "base": {"record": record_dir.name, "segmentation": record["versions"]["segmentation"],
                 "segmentation_sha256": sha256(record_dir / "segmentation.json"),
                 "run": first.get("run"), "by": first.get("by"), "date": first.get("date"),
                 "commit": first.get("commit"), "source": first.get("segmentation")},
    }
    if replace_step is not None:
        if excerpt is None:
            raise ValueError("--replace-step needs --from")
        part = json.loads(excerpt.read_text(encoding="utf-8"))
        if part.get("video_id") not in (None, record["video_id"]):
            raise ValueError(f"{excerpt} is for {part.get('video_id')}, not {record['video_id']}")
        entries = [s for s in part["steps"] if s["step_id"] == replace_step]
        if not entries:
            raise ValueError(f"{excerpt} has no step {replace_step}")
        steps = [s for s in segmentation["steps"] if s["step_id"] != replace_step] + entries
        segmentation = {**segmentation, "steps": sorted(steps, key=lambda s: (s["step_id"], s.get("start") or 0))}
        provenance["replaced_step"] = {"step_id": replace_step, "from": excerpt.name,
                                       "from_sha256": sha256(excerpt), "entries": len(entries), "note": note}
    return {"source_kind": "saved_model_segmentation", "video_id": record["video_id"],
            "video_sha256": record["video_sha256"], "segmentation": segmentation, "provenance": provenance}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("record_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--replace-step", type=int)
    ap.add_argument("--from", dest="excerpt", type=Path)
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    try:
        bundle = build(a.record_dir, a.replace_step, a.excerpt, a.note)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    a.out.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {a.out} ({bundle['video_id']}, base {bundle['provenance']['base']['record']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
