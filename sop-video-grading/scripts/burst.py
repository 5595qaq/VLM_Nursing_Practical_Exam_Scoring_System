"""Dense frames from the ORIGINAL video over a short span: a zoom with high time resolution.

usage: python burst.py ORIGINAL_VIDEO --start 140.5 --end 142.0 --n 9 --box x0,y0,x1,y1 \
           --rel-to-crop CONTEXT_BOX --out burst.jpg

zoom shows 1-4 instants the agent picks; an action lasting a few frames is easy to miss between them.
burst takes --n frames (3-12) evenly over [start, end] (at most 3 s, so >= ~4 fps), crops --box
(normalized to CONTEXT_BOX, like zoom.region) at original resolution and tiles them, each labelled
#k and its time. Prints {"image": path, "times": [...]}. Exit code 2 = bad request.

Uses the frame helpers of sop-video-skill/scripts/_video.py (SOP_SKILL_DIR, or the sibling folder).
"""
import argparse
import json
import os
import sys
from pathlib import Path

MAX_SPAN, MIN_SPAN = 3.0, 0.3
MIN_N, MAX_N = 3, 12


def _skill_scripts() -> Path:
    root = os.environ.get("SOP_SKILL_DIR") or str(Path(__file__).resolve().parents[2] / "sop-video-skill")
    return Path(root) / "scripts"


def burst_times(start: float, end: float, n: int) -> list[float]:
    if not MIN_SPAN <= end - start <= MAX_SPAN:
        raise ValueError(f"burst span {end - start:.2f}s outside {MIN_SPAN:g}-{MAX_SPAN:g}s")
    if not MIN_N <= n <= MAX_N:
        raise ValueError(f"burst n={n} outside {MIN_N}-{MAX_N}")
    return [round(start + (end - start) * i / (n - 1), 3) for i in range(n)]


def compose(outer, inner):
    ox0, oy0, ox1, oy1 = outer
    ix0, iy0, ix1, iy1 = inner
    w, h = ox1 - ox0, oy1 - oy0
    return (ox0 + ix0 * w, oy0 + iy0 * h, ox0 + ix1 * w, oy0 + iy1 * h)


def render(video: str, times: list[float], region, context, out: str) -> None:
    sys.path.insert(0, str(_skill_scripts()))
    from _video import crop_norm, grab, label, save_jpg, tile

    box = compose(context, region) if context else region
    cells = [label(crop_norm(grab(video, t), box), f"#{k} {int(t // 60):02d}:{t % 60:05.2f}")
             for k, t in enumerate(times)]
    cols = 3 if len(times) <= 9 else 4
    save_jpg(tile(cells, cols, 520), out, max_bytes=3_000_000)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--box", required=True)
    ap.add_argument("--rel-to-crop")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[1:])
    try:
        times = burst_times(a.start, a.end, a.n)
    except ValueError as exc:
        print(f"REJECTED burst: {exc}", file=sys.stderr)
        return 2
    region = [float(v) for v in a.box.split(",")]
    context = [float(v) for v in a.rel_to_crop.split(",")] if a.rel_to_crop else None
    render(a.video, times, region, context, a.out)
    print(json.dumps({"image": a.out, "times": times}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
