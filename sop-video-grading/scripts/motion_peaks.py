"""Find short bursts of hand movement inside one task window (no model call).

usage: python motion_peaks.py ORIGINAL_VIDEO --start 118 --end 147 --box HANDS_BOX [--fps 15] [--top 8] --out peaks.json

The grading agent's opening sheet samples the window every 2-4 s, so an action lasting a fraction of
a second (needle into the vial, recapping, the pad going on, a flick) can fall between two cells and
the agent never knows where to zoom. This script reads the window from the original video at --fps
(default 15, i.e. every 0.07 s), measures the mean frame difference inside --box (hands_box,
normalized to the full frame), lightly smooths it and returns the strongest local peaks, at least
--gap seconds apart. Peaks only say "something moved here"; they say nothing about which action it is.

Prints/writes {"fps", "start", "end", "peaks": [{"t", "e"}...]} with peaks sorted by time.
"""
import argparse
import json
import subprocess
import sys

import numpy as np

MAX_SPAN = 180.0


def read_gray(video: str, start: float, end: float, box, fps: float, width: int = 240) -> np.ndarray:
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", video]))["streams"][0]
    W, H = probe["width"], probe["height"]
    x0, y0, x1, y1 = box
    cw, ch = max(2, int((x1 - x0) * W) // 2 * 2), max(2, int((y1 - y0) * H) // 2 * 2)
    h = max(2, int(round(width * ch / cw / 2)) * 2)
    vf = f"fps={fps},crop={cw}:{ch}:{int(x0 * W)}:{int(y0 * H)},scale={width}:{h},format=gray"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{end - start:.3f}",
                          "-vf", vf, "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    n = len(raw) // (width * h)
    return np.frombuffer(raw[:n * width * h], np.uint8).reshape(n, h, width).astype(np.float32)


def find_peaks(frames: np.ndarray, fps: float, start: float, top: int = 8, gap: float = 0.5,
               smooth_sec: float = 0.2) -> list[dict]:
    if len(frames) < 3:
        return []
    energy = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)) / 255.0  # energy[i] = change into frame i+1
    k = max(1, int(round(smooth_sec * fps)))
    if k > 1:
        energy = np.convolve(np.pad(energy, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    floor = np.median(energy)
    order = max(1, int(round(gap * fps)))
    candidates = [i for i in range(len(energy))
                  if energy[i] > floor and energy[i] == energy[max(0, i - order):i + order + 1].max()]
    chosen: list[int] = []
    for i in sorted(candidates, key=lambda i: -energy[i]):
        if all(abs(i - j) > order for j in chosen):
            chosen.append(i)
        if len(chosen) == top:
            break
    return [{"t": round(start + (i + 1) / fps, 2), "e": round(float(energy[i]), 5)} for i in sorted(chosen)]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--box", required=True)
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--gap", type=float, default=0.5)
    ap.add_argument("--out")
    a = ap.parse_args(argv[1:])
    if not 0 <= a.start < a.end or a.end - a.start > MAX_SPAN:
        print(f"REJECTED: span [{a.start}, {a.end}] must be increasing and at most {MAX_SPAN:g} s", file=sys.stderr)
        return 2
    box = [float(v) for v in a.box.split(",")]
    frames = read_gray(a.video, a.start, a.end, box, a.fps)
    result = {"fps": a.fps, "start": a.start, "end": a.end,
              "peaks": find_peaks(frames, a.fps, a.start, a.top, a.gap)}
    text = json.dumps(result, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
