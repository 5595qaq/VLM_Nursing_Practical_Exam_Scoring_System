"""Find short, sharp sounds (flick clicks) inside one task window (no model call).

usage: python audio_peaks.py ORIGINAL_VIDEO --start 253 --end 277 [--top 16] [--rise 6] --out peaks.json

The flick of 17a/27a is often quiet (a few dB above the room noise) and lasts under a second, so the
grading agent can miss it in a 2 fps clip. This script decodes the window's audio as mono 16 kHz,
high-passes it (--highpass, default 2000 Hz, removes voices and hum; clicks are broadband), measures
the level every 10 ms and returns the strongest local peaks that rise at least --rise dB above the
window's median level, at least --gap seconds apart. Peaks only say "something clicked here"; putting
equipment down or touching the cart also clicks, so the agent still has to see the fingers flick.

Prints/writes {"has_audio", "silent", "max_db", "floor_db", "peaks": [{"t", "db"}...], "runs": [{"start",
"end", "n"}...]} with peaks sorted by time. A run is >= --run-min peaks each within --run-gap s of the
previous one: a flick is several clicks in a row, a dropped cap is one. has_audio=false: the video has no audio track. silent=true: there is a track but the
window is (near) digital silence, which some recordings have; treat it like no audio.
"""
import argparse
import json
import subprocess
import sys

import numpy as np

MAX_SPAN = 180.0
RATE = 16000
HOP = 0.01  # seconds per level sample
SILENT_DB = -60.0  # the loudest 10 ms below this = no usable sound


def has_audio(video: str) -> bool:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a",
                                   "-show_entries", "stream=index", "-of", "csv=p=0", video], text=True)
    return bool(out.strip())


def read_levels(video: str, start: float, end: float, highpass: float) -> np.ndarray:
    """dBFS of each 10 ms frame of the high-passed mono signal."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{end - start:.3f}",
                          "-vn", "-ac", "1", "-ar", str(RATE), "-af", f"highpass=f={highpass:g}",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(raw[:len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768.0
    hop = int(RATE * HOP)
    n = len(samples) // hop
    if n == 0:
        return np.zeros(0, np.float32)
    rms = np.sqrt((samples[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def find_runs(peaks: list[dict], run_gap: float = 0.8, run_min: int = 3) -> list[dict]:
    runs, current = [], peaks[:1]
    for p in peaks[1:]:
        if p["t"] - current[-1]["t"] <= run_gap:
            current.append(p)
        else:
            runs.append(current)
            current = [p]
    runs.append(current)
    return [{"start": r[0]["t"], "end": r[-1]["t"], "n": len(r)} for r in runs if len(r) >= run_min]


def find_peaks(levels: np.ndarray, start: float, top: int = 16, rise: float = 6.0, gap: float = 0.15) -> dict:
    if len(levels) == 0:
        return {"silent": True, "max_db": None, "floor_db": None, "peaks": [], "runs": []}
    floor, loudest = float(np.median(levels)), float(levels.max())
    order = max(1, int(round(gap / HOP)))
    candidates = [i for i in range(len(levels))
                  if levels[i] >= floor + rise and levels[i] == levels[max(0, i - order):i + order + 1].max()]
    chosen: list[int] = []
    for i in sorted(candidates, key=lambda i: -levels[i]):
        if all(abs(i - j) > order for j in chosen):
            chosen.append(i)
        if len(chosen) == top:
            break
    silent = loudest < SILENT_DB
    peaks = [] if silent else [{"t": round(start + i * HOP, 2), "db": round(float(levels[i]), 1)}
                               for i in sorted(chosen)]
    return {"silent": silent, "max_db": round(loudest, 1), "floor_db": round(floor, 1), "peaks": peaks,
            "runs": find_runs(peaks)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--top", type=int, default=16)
    ap.add_argument("--rise", type=float, default=6.0)
    ap.add_argument("--gap", type=float, default=0.15)
    ap.add_argument("--highpass", type=float, default=2000.0)
    ap.add_argument("--out")
    a = ap.parse_args(argv[1:])
    if not 0 <= a.start < a.end or a.end - a.start > MAX_SPAN:
        print(f"REJECTED: span [{a.start}, {a.end}] must be increasing and at most {MAX_SPAN:g} s", file=sys.stderr)
        return 2
    if has_audio(a.video):
        result = {"has_audio": True, **find_peaks(read_levels(a.video, a.start, a.end, a.highpass),
                                                  a.start, a.top, a.rise, a.gap)}
    else:
        result = {"has_audio": False, "silent": True, "max_db": None, "floor_db": None, "peaks": [], "runs": []}
    result.update(start=a.start, end=a.end)
    text = json.dumps(result, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
