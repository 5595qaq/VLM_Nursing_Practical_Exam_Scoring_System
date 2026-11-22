"""Crop preview (verify a crop box) and zoom (high-res detail at given times).

preview: python crop.py preview PROXY --box x0,y0,x1,y1 --n 9 --out preview.jpg
         -> the same box applied to n evenly spaced frames, tiled, for checking that both hands
            stay fully inside the box across the whole video.
zoom:    python crop.py zoom ORIGINAL_VIDEO --times 245.0,247.5 --box 0.35,0.5,0.65,0.85 \
             --rel-to-crop CONTEXT_BOX --out z.jpg
         -> region crops from the ORIGINAL resolution video (not the proxy), tiled 1-4 per image.
            --box is normalized to the FULL frame, or to CONTEXT_BOX when --rel-to-crop is given
            (SKILL.md: zoom.region is relative to the context box).
            Each cell gets a locator thumbnail (bottom-right): the whole context box at that time
            with the zoom region drawn in red, so hands cut off by the region are visible at once.
            Region limits (relative to the context box; checked only with --rel-to-crop):
              area <= 0.50, width <= 0.75, height <= 0.75  -> otherwise exit code 2, no image.
            --no-limits disables the check (debugging only).
"""
import argparse
import sys

import cv2

from _video import crop_norm, fmt_t, grab, label, parse_box, parse_times, probe, save_jpg, tile

ZOOM_MAX_AREA, ZOOM_MAX_SIDE = 0.50, 0.75


def compose(outer, inner):
    """inner box expressed inside outer box -> full-frame box."""
    ox0, oy0, ox1, oy1 = outer
    ix0, iy0, ix1, iy1 = inner
    w, h = ox1 - ox0, oy1 - oy0
    return (ox0 + ix0 * w, oy0 + iy0 * h, ox0 + ix1 * w, oy0 + iy1 * h)


def check_region(rel):
    w, h = rel[2] - rel[0], rel[3] - rel[1]
    errs = []
    if w * h > ZOOM_MAX_AREA:
        errs.append(f"region area {w*h:.2f} > {ZOOM_MAX_AREA} of the context box")
    if w > ZOOM_MAX_SIDE:
        errs.append(f"region width {w:.2f} > {ZOOM_MAX_SIDE}")
    if h > ZOOM_MAX_SIDE:
        errs.append(f"region height {h:.2f} > {ZOOM_MAX_SIDE}")
    return errs


def add_locator(cell, ctx_img, rel, frac=0.28):
    """Paste a small context thumbnail with the zoom region outlined into the cell's bottom-right."""
    cell = cell.copy()
    H, W = cell.shape[:2]
    ch, cw = ctx_img.shape[:2]
    tw = max(80, int(W * frac))
    th = int(round(tw * ch / cw))
    if th > H * 0.5:
        th = int(H * 0.5)
        tw = int(round(th * cw / ch))
    thumb = cv2.resize(ctx_img, (tw, th), interpolation=cv2.INTER_AREA)
    x0, y0, x1, y1 = rel
    cv2.rectangle(thumb, (int(x0 * tw), int(y0 * th)), (int(x1 * tw) - 1, int(y1 * th) - 1), (0, 0, 255), 2)
    cv2.rectangle(thumb, (0, 0), (tw - 1, th - 1), (255, 255, 255), 2)
    cell[H - th:H, W - tw:W] = thumb
    return cell


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["preview", "zoom"])
    ap.add_argument("video")
    ap.add_argument("--box", required=True)
    ap.add_argument("--rel-to-crop")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--times")
    ap.add_argument("--cell", type=int, default=640)
    ap.add_argument("--no-limits", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rel = parse_box(a.box)
    ctx = parse_box(a.rel_to_crop) if a.rel_to_crop else None
    if a.mode == "zoom" and ctx and not a.no_limits:
        errs = check_region(rel)
        if errs:
            print("REJECTED zoom region: " + "; ".join(errs), file=sys.stderr)
            sys.exit(2)
    box = compose(ctx, rel) if ctx else rel
    dur = probe(a.video)["duration"]
    times = parse_times(a.times) if a.times else [dur * (i + 0.5) / a.n for i in range(a.n)]
    imgs = []
    for t in times:
        full = grab(a.video, t)
        im = label(crop_norm(full, box), f"t={fmt_t(t)}")
        if a.mode == "zoom":
            loc_img = crop_norm(full, ctx) if ctx else full
            loc_rel = rel if ctx else box
            im = add_locator(im, loc_img, loc_rel)
        imgs.append(im)
    if a.mode == "zoom":
        cell = max(im.shape[1] for im in imgs)  # keep native resolution for detail
        cell = min(cell, 1000)
        cols = 1 if len(imgs) == 1 else 2
    else:
        cell, cols = a.cell, 3
    save_jpg(tile(imgs, cols, cell), a.out, max_bytes=3_000_000)
    print(a.out, "box_full_frame=", ",".join(f"{v:.3f}" for v in box), [round(t, 1) for t in times])


if __name__ == "__main__":
    main()
