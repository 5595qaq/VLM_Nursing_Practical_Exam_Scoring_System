"""Cut a short clip (with audio when the source has it) for the grading agent.

usage: python clip.py ORIGINAL_VIDEO --start 120 --end 150 [--box x0,y0,x1,y1] [--width 640] --out clip.mp4
  Reads the ORIGINAL video (the `_src.mp4` keeps the audio track; the proxy has none), crops to
  --box (normalized to the full frame, normally the context box) and scales to --width.
  Prints {"clip": path, "has_audio": bool, "start": s, "end": e, "bytes": n}.
  Exit code 2 = bad request (span outside the video, too long, end <= start).
"""
import argparse
import json
import os
import subprocess
import sys

MAX_SECONDS = 60.0


def has_audio(path: str) -> bool:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a",
                                   "-show_entries", "stream=index", "-of", "csv=p=0", path], text=True)
    return bool(out.strip())


def duration(path: str) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", path], text=True)
    return float(out.strip())


def cut(video: str, start: float, end: float, out: str, box=None, width: int = 640) -> dict:
    total = duration(video)
    end = min(end, total)
    if not 0 <= start < end:
        raise ValueError(f"clip span [{start}, {end}] is outside the video (0-{total:.1f} s)")
    if end - start > MAX_SECONDS:
        raise ValueError(f"clip is {end - start:.1f} s; at most {MAX_SECONDS:g} s")
    audio = has_audio(video)
    filters = []
    if box:
        x0, y0, x1, y1 = box
        filters.append(f"crop=iw*{x1 - x0:.4f}:ih*{y1 - y0:.4f}:iw*{x0:.4f}:ih*{y0:.4f}")
    filters.append(f"scale={width}:-2")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{end - start:.3f}",
           "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd += ["-c:a", "aac", "-b:a", "96k"] if audio else ["-an"]
    subprocess.run(cmd + [out], check=True)
    return {"clip": out, "has_audio": audio, "start": round(start, 2), "end": round(end, 2),
            "bytes": os.path.getsize(out)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--box")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[1:])
    box = [float(v) for v in a.box.split(",")] if a.box else None
    try:
        info = cut(a.video, a.start, a.end, a.out, box, a.width)
    except ValueError as exc:
        print(f"REJECTED clip: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
