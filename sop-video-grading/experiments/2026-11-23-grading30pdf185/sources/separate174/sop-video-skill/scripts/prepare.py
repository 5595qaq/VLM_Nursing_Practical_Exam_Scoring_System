"""Opening step (run once per video, before the model sees anything).

usage: python prepare.py ORIGINAL_VIDEO --out run/ [--jobs N] [--hwaccel]

Produces in --out:
  probe.json   original resolution / fps / duration / codec, plus proxy path
  proxy.mp4    960-wide, 10 fps, GOP 10 H.264 (sheets / motion / grid / crop_preview use this)
  grid.jpg     6 full frames with a normalized 0-1 grid (sent to the model with the first turn)
Zoom always reads the ORIGINAL video, never the proxy.
Motion energy is NOT computed here: it needs the crop (see SKILL.md, runs after crop is confirmed).
"""
import argparse
import json
import os
import subprocess
import sys
import time

from _video import PROXY_FPS, PROXY_WIDTH, make_proxy, probe

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, help="parallel segments (default: CPU count)")
    ap.add_argument("--hwaccel", action="store_true", help="ffmpeg -hwaccel auto for decoding")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    info = probe(a.video)
    proxy = os.path.join(a.out, "proxy.mp4")
    t0 = time.time()
    make_proxy(a.video, proxy, jobs=a.jobs, hwaccel=a.hwaccel)
    info.update({"original": os.path.abspath(a.video), "proxy": os.path.abspath(proxy),
                 "proxy_spec": {"width": PROXY_WIDTH, "fps": PROXY_FPS, "gop": PROXY_FPS},
                 "proxy_seconds": round(time.time() - t0, 1)})
    json.dump(info, open(os.path.join(a.out, "probe.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    grid = os.path.join(a.out, "grid.jpg")
    subprocess.check_call([sys.executable, os.path.join(HERE, "grid_overlay.py"), proxy,
                           "--n", "6", "--out", grid])
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
