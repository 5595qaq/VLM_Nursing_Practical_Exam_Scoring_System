"""Full-frame images with a normalized coordinate grid (every 10%, labelled) for crop estimation.

usage: python grid_overlay.py VIDEO --n 6 --out grid.jpg            # n evenly spaced times
       python grid_overlay.py VIDEO --times 30,120,240,360 --out grid.jpg
       python grid_overlay.py VIDEO --n 6 --box 0.2,0.3,0.8,1.0 --out grid.jpg  # also draw a candidate box
"""
import argparse

import cv2

from _video import fmt_t, grab, label, parse_box, parse_times, probe, save_jpg, tile


def draw_grid(img, box=None):
    img = img.copy()
    h, w = img.shape[:2]
    fs = max(0.5, w / 1600)
    for i in range(1, 10):
        x, y = int(w * i / 10), int(h * i / 10)
        cv2.line(img, (x, 0), (x, h), (0, 255, 0), 1)
        cv2.line(img, (0, y), (w, y), (0, 255, 0), 1)
        cv2.putText(img, f"{i/10:.1f}", (x + 3, int(h * 0.97)), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 255, 0), 2)
        cv2.putText(img, f"{i/10:.1f}", (w - int(60 * fs), y - 4), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 255, 0), 2)
    if box:
        x0, y0, x1, y1 = box
        cv2.rectangle(img, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)), (0, 0, 255), 3)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--times")
    ap.add_argument("--box")
    ap.add_argument("--cell", type=int, default=800)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    dur = probe(a.video)["duration"]
    times = parse_times(a.times) if a.times else [dur * (i + 0.5) / a.n for i in range(a.n)]
    box = parse_box(a.box)
    imgs = [label(draw_grid(grab(a.video, t), box), f"t={fmt_t(t)}") for t in times]
    save_jpg(tile(imgs, 2 if len(imgs) <= 4 else 3, a.cell), a.out)
    print(a.out, [round(t, 1) for t in times])


if __name__ == "__main__":
    main()
