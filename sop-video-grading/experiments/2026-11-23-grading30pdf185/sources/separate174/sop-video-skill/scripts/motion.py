"""Motion energy inside the crop box -> candidate change points and low-activity (still) spans.

usage: python motion.py PROXY --box HANDS_BOX --box2 CONTEXT_BOX --fps 5 --out motion.json [--plot motion.png]

Runs AFTER the crop is confirmed (needs hands_box). --box2 (context box) is optional but recommended:
hands_box energy alone cannot tell "hands still" from "hands left the work area".

Output JSON:
  energy:     [[t, e], ...] smoothed mean abs frame difference (0-1 scale), sampled at --fps
  peaks:      [{"t", "e"}] local maxima (burst of movement, e.g. tearing package, flicking syringe)
  valleys:    [{"t", "e"}] local minima between activity (hands still -> likely step boundary)
  still_spans:[[t0, t1]] spans where energy < still_thr for >= min_still seconds
  candidates: boundary candidates = motion PEAKS (min gap --order-sec). Round-1 finding on the first test video:
              human-labelled boundaries sit near motion bursts (reaching / picking up / putting down),
              not near still valleys (peaks: median 1.8 s to a GT boundary vs 4.5 s for random points;
              valleys: 3.2 s vs 4.0 s random).
  still_spans are reported as context only (looking at scale, pausing), not as boundaries.
  With --box2 (context box):
  energy2:    [[t, e], ...] motion energy in the context box OUTSIDE hands_box (hands_box masked out)
  left_spans: [[t0, t1]] outside/inside energy ratio above its --left-pct percentile and outside
              energy above its median, for >= --min-left s -> hands left the work area (IV pole,
              sharps bin). Overlapping still_spans are dropped (low hands energy there is not "still").
  Note: plain context-box energy is useless for this - the body stays inside hands_box while the
  hands are away, so it just tracks hands_box energy.
"""
import argparse
import json
import subprocess

import cv2
import numpy as np

from _video import parse_box, probe


def read_frames(path, fps, box, w=320):
    info = probe(path)
    W, H = info["width"], info["height"]
    x0, y0, x1, y1 = box or (0, 0, 1, 1)
    cw, ch = int((x1 - x0) * W) // 2 * 2, int((y1 - y0) * H) // 2 * 2
    if cw < 2 or ch < 2:
        raise SystemExit(f"box {box} is too small for a {W}x{H} video")
    # Give ffmpeg the exact height: with scale=w:-2 it rounds .5 up while Python's round() rounds to
    # even, so the two could differ by 2 px and every raw frame below would be sliced at the wrong offset.
    h = max(2, int(w * ch / cw / 2 + 0.5) * 2)
    vf = f"fps={fps},crop={cw}:{ch}:{int(x0*W)}:{int(y0*H)},scale={w}:{h},format=gray"
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE)
    size = w * h
    frames = []
    while True:
        b = p.stdout.read(size)
        if len(b) < size:
            break
        frames.append(np.frombuffer(b, np.uint8).reshape(h, w))
    if p.wait() != 0 or len(frames) < 2:
        raise SystemExit(f"ffmpeg could not read frames from {path} (exit {p.returncode}, {len(frames)} frames)")
    return frames


def smooth(x, k):
    if k <= 1:
        return x
    ker = np.ones(k) / k
    return np.convolve(np.pad(x, (k // 2, k - 1 - k // 2), mode="edge"), ker, mode="valid")


def local_ext(x, order, mode):
    idx = []
    for i in range(len(x)):
        lo, hi = max(0, i - order), min(len(x), i + order + 1)
        seg = x[lo:hi]
        if (mode == "max" and x[i] == seg.max() and x[i] > np.median(x)) or \
           (mode == "min" and x[i] == seg.min() and x[i] < np.median(x)):
            if not idx or i - idx[-1] > order:
                idx.append(i)
    return idx


def energy(frames, fps, smooth_sec, mask=None):
    f = [cv2.GaussianBlur(x, (5, 5), 0).astype(np.float32) for x in frames]
    if mask is None:
        e = [np.mean(np.abs(f[i] - f[i - 1])) for i in range(1, len(f))]
    else:
        e = [np.mean(np.abs(f[i] - f[i - 1])[mask]) for i in range(1, len(f))]
    e = np.array([0.0] + e) / 255
    return smooth(e, max(1, int(smooth_sec * fps)))


def outside_mask(shape, ctx, hb):
    """True where a context-box pixel is NOT inside hands_box."""
    h, w = shape
    cx0, cy0, cx1, cy1 = ctx
    hx0, hy0, hx1, hy1 = hb
    cw, ch = cx1 - cx0, cy1 - cy0
    m = np.ones((h, w), bool)
    x0, x1 = int((hx0 - cx0) / cw * w), int(np.ceil((hx1 - cx0) / cw * w))
    y0, y1 = int((hy0 - cy0) / ch * h), int(np.ceil((hy1 - cy0) / ch * h))
    m[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = False
    return m


def spans_where(mask, fps, min_len):
    out, s0 = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and s0 is None:
            s0 = i
        elif not v and s0 is not None:
            if (i - s0) / fps >= min_len:
                out.append([round(s0 / fps, 1), round(i / fps, 1)])
            s0 = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--box", help="hands_box (full-frame normalized)")
    ap.add_argument("--box2", help="context box; enables energy2 / left_spans")
    ap.add_argument("--left-pct", type=float, default=75,
                    help="percentile of outside/inside energy ratio treated as 'hands outside hands_box'")
    ap.add_argument("--min-left", type=float, default=4.0)
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--smooth-sec", type=float, default=1.0)
    ap.add_argument("--order-sec", type=float, default=5.0, help="min distance between extrema")
    ap.add_argument("--still-pct", type=float, default=25, help="percentile of energy treated as still")
    ap.add_argument("--min-still", type=float, default=1.5)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plot")
    a = ap.parse_args()
    frames = read_frames(a.video, a.fps, parse_box(a.box))
    es = energy(frames, a.fps, a.smooth_sec)
    t = np.arange(len(es)) / a.fps
    order = max(1, int(a.order_sec * a.fps))
    pk = local_ext(es, order, "max")
    vl = local_ext(es, order, "min")
    thr = np.percentile(es, a.still_pct)
    low = es < thr
    es2, left = None, []
    if a.box2:
        ctx_frames = read_frames(a.video, a.fps, parse_box(a.box2))
        om = outside_mask(ctx_frames[0].shape, parse_box(a.box2), parse_box(a.box))
        es2 = energy(ctx_frames, a.fps, a.smooth_sec, om)
        n = min(len(es), len(es2))
        es2 = es2[:n]
        ratio = smooth(es2 / (es[:n] + 1e-4), max(1, int(2 * a.fps)))
        left_mask = np.zeros(len(es), bool)
        left_mask[:n] = (ratio > np.percentile(ratio, a.left_pct)) & (es2 > np.median(es2))
        left = spans_where(left_mask, a.fps, a.min_left)
    spans = spans_where(low, a.fps, a.min_still)
    if left:
        spans = [sp for sp in spans if not any(sp[0] < l1 and sp[1] > l0 for l0, l1 in left)]
    merged = [round(float(t[i]), 1) for i in pk]
    out = {"fps": a.fps, "still_thr": round(float(thr), 5),
           "energy": [[round(float(tt), 1), round(float(v), 5)] for tt, v in zip(t, es)][::max(1, int(a.fps))],
           "peaks": [{"t": round(float(t[i]), 1), "e": round(float(es[i]), 5)} for i in pk],
           "valleys": [{"t": round(float(t[i]), 1), "e": round(float(es[i]), 5)} for i in vl],
           "still_spans": spans, "candidates": merged}
    if es2 is not None:
        out["energy2"] = [[round(float(tt), 1), round(float(v), 5)] for tt, v in zip(t, es2)][::max(1, int(a.fps))]
        out["left_spans"] = left
    json.dump(out, open(a.out, "w"), ensure_ascii=False)
    print(f"{len(frames)} frames, {len(pk)} peaks, {len(vl)} valleys, {len(spans)} still spans, "
          f"{len(merged)} candidates, {len(left)} left spans -> {a.out}")
    if a.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(18, 3.2))
        ax.plot(t, es, lw=0.8, label="hands_box")
        if es2 is not None:
            ax.plot(t[:len(es2)], es2, lw=0.6, color="tab:orange", alpha=0.7, label="context outside hands_box")
            for sp in left:
                ax.axvspan(sp[0], sp[1], color="tab:orange", alpha=0.15)
            ax.legend(loc="upper right", fontsize=7)
        for sp in spans:
            ax.axvspan(sp[0], sp[1], color="0.85")
        ax.plot([t[i] for i in pk], [es[i] for i in pk], "r^", ms=4)
        ax.set_xticks(np.arange(0, t[-1] + 1, 30))
        ax.set_xticklabels([f"{int(x//60)}:{int(x%60):02d}" for x in np.arange(0, t[-1] + 1, 30)], fontsize=7)
        ax.set_xlim(0, t[-1])
        ax.grid(axis="x", lw=0.3)
        fig.tight_layout()
        fig.savefig(a.plot, dpi=110)


if __name__ == "__main__":
    main()
