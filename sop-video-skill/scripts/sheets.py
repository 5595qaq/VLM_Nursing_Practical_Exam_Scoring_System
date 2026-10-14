"""3x3 contact sheets of the cropped operating area, each cell labelled #index and timestamp.

global:   python sheets.py PROXY --box BOX --start 0 --end 648 --step 8 --out-prefix out/global
          -> out/global_00.jpg, _01.jpg ... (9 frames per sheet, `step` seconds apart)
overview: python sheets.py PROXY --box BOX --start 120 --end 150 --n 9 --out-prefix out/ov_120
          -> one sheet of n frames evenly spread over [start, end] (denser look at one interval)
Prints a JSON index: [{"sheet": path, "cells": [{"i": k, "t": seconds}, ...]}]
"""
import argparse
import json

import numpy as np

from _video import crop_norm, fmt_t, grab, label, parse_box, save_jpg, tile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--box")
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--step", type=float)
    ap.add_argument("--n", type=int)
    ap.add_argument("--cell", type=int, default=520)  # 3*520 ~ 1568 px = model max edge
    ap.add_argument("--out-prefix", required=True)
    a = ap.parse_args()
    box = parse_box(a.box)
    if a.step:
        times = list(np.arange(a.start + a.step / 2, a.end, a.step))
    else:
        n = a.n or 9
        times = list(np.linspace(a.start, a.end, n))
    index, k = [], 0
    for s in range(0, len(times), 9):
        chunk = times[s:s + 9]
        imgs = []
        cells = []
        for t in chunk:
            imgs.append(label(crop_norm(grab(a.video, t), box), f"#{k} {fmt_t(t)}"))
            cells.append({"i": k, "t": round(float(t), 2)})
            k += 1
        path = f"{a.out_prefix}_{s // 9:02d}.jpg"
        save_jpg(tile(imgs, 3, a.cell), path, quality=80)
        index.append({"sheet": path, "cells": cells})
    print(json.dumps(index, ensure_ascii=False))


if __name__ == "__main__":
    main()
