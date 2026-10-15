"""Shared helpers: frame grabbing (ffmpeg seek), labels, tiling, JPEG size control.

All scripts are deterministic and do not call any model. They only produce images / JSON
that are then sent to the model (Claude on Vertex AI) together with SKILL.md as system prompt.
"""
import json
import os
import subprocess

import cv2
import numpy as np


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,codec_name:format=duration",
        "-of", "json", path])
    j = json.loads(out)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"width": int(s["width"]), "height": int(s["height"]), "codec": s.get("codec_name", ""),
            "fps": float(num) / float(den), "duration": float(j["format"]["duration"])}


PROXY_FPS, PROXY_WIDTH = 10, 960  # proxy spec: 960 wide, 10 fps, GOP = fps (1 s) H.264, no audio


def _src_sig(src, fps, width):
    st = os.stat(src)
    return {"src": os.path.abspath(src), "size": st.st_size, "mtime": int(st.st_mtime),
            "fps": fps, "width": width}


def make_proxy(src, dst, fps=PROXY_FPS, width=PROXY_WIDTH, jobs=None, hwaccel=False):
    """Low-res, short-GOP H.264 proxy: fast seeking for sheets/motion/grid. Zoom uses the original.

    Speed: decoding (HEVC 1080p+) dominates, so the source is split into `jobs` time segments that
    are transcoded in parallel and concatenated (GOP = fps keeps every segment start a keyframe).
    Reused when a sidecar <dst>.json records the same source size/mtime and proxy parameters.
    """
    sig = _src_sig(src, fps, width)
    side = dst + ".json"
    if os.path.exists(dst) and os.path.exists(side):
        try:
            if json.load(open(side, encoding="utf-8")) == sig:
                return dst
        except Exception:
            pass
    dur = probe(src)["duration"]
    jobs = max(1, min(jobs or os.cpu_count() or 1, int(dur // 30) or 1))  # segments >= 30 s
    vf = f"fps={fps},scale={width}:-2:flags=fast_bilinear"
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-g", str(fps),
           "-keyint_min", str(fps), "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-an"]
    hw = ["-hwaccel", "auto"] if hwaccel else []
    tmpdir = dst + ".parts"
    os.makedirs(tmpdir, exist_ok=True)
    seg = dur / jobs
    procs, parts = [], []
    for k in range(jobs):
        p = os.path.join(tmpdir, f"p{k:03d}.mp4")
        parts.append(p)
        t0 = k * seg
        cmd = ["ffmpeg", "-v", "error", "-y", *hw, "-ss", f"{t0:.3f}", "-i", src,
               "-t", f"{seg:.3f}", "-vf", vf, *enc, "-threads", "1" if jobs > 1 else "0", p]
        procs.append(subprocess.Popen(cmd))
    for pr in procs:
        if pr.wait() != 0:
            raise RuntimeError("proxy segment transcode failed")
    if jobs == 1:
        os.replace(parts[0], dst)
    else:
        lst = os.path.join(tmpdir, "list.txt")
        with open(lst, "w") as f:
            f.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
        subprocess.check_call(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                               "-i", lst, "-c", "copy", dst])
    for p in os.listdir(tmpdir):
        os.remove(os.path.join(tmpdir, p))
    os.rmdir(tmpdir)
    json.dump(sig, open(side, "w", encoding="utf-8"))
    return dst


# The container duration can extend past the last video frame (audio track, frame duration), so a
# seek near the end may yield no frame; step back until one is decoded.
GRAB_BACKOFF = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0)


def grab(path, t, width=None):
    """Grab one frame at time t (seconds) as BGR ndarray. Uses ffmpeg input seeking.

    If t is at or past the last frame, the closest earlier frame (up to 2 s back) is returned.
    """
    vf = [] if width is None else ["-vf", f"scale={width}:-2"]
    for back in GRAB_BACKOFF:
        if back and t - back < 0:
            break
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(t - back, 0):.3f}", "-i", path, "-frames:v", "1",
               *vf, "-f", "image2pipe", "-vcodec", "png", "-"]
        buf = subprocess.check_output(cmd)
        if buf:
            img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                return img
    raise RuntimeError(f"cannot grab frame at {t}s from {path}")


def crop_norm(img, box):
    """box = (x0, y0, x1, y1) normalized 0-1."""
    if box is None:
        return img
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def fmt_t(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def label(img, text, scale=None):
    img = img.copy()
    h, w = img.shape[:2]
    scale = scale or max(0.5, w / 600)
    th = int(2 * scale)
    (tw, tht), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, th)
    cv2.rectangle(img, (0, 0), (tw + 12, tht + 14), (0, 0, 0), -1)
    cv2.putText(img, text, (6, tht + 7), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 255, 255), th)
    return img


def tile(imgs, cols, cell_w):
    """Resize every image to cell width (keep aspect of the first) and tile in a grid."""
    h0, w0 = imgs[0].shape[:2]
    cell_h = int(round(cell_w * h0 / w0))
    rows = (len(imgs) + cols - 1) // cols
    canvas = np.full((rows * cell_h + (rows - 1) * 4, cols * cell_w + (cols - 1) * 4, 3), 255, np.uint8)
    for i, im in enumerate(imgs):
        r, c = divmod(i, cols)
        im = cv2.resize(im, (cell_w, cell_h), interpolation=cv2.INTER_AREA)
        canvas[r * (cell_h + 4):r * (cell_h + 4) + cell_h, c * (cell_w + 4):c * (cell_w + 4) + cell_w] = im
    return canvas


def save_jpg(img, path, quality=85, max_bytes=None):
    q = quality
    while True:
        cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if max_bytes is None or os.path.getsize(path) <= max_bytes or q <= 40:
            return path
        q -= 10


def parse_box(s):
    if s is None:
        return None
    v = [float(x) for x in s.split(",")]
    assert len(v) == 4 and 0 <= v[0] < v[2] <= 1 and 0 <= v[1] < v[3] <= 1, "box must be x0,y0,x1,y1 in 0-1"
    return tuple(v)


def parse_times(s):
    return [float(x) for x in s.split(",")]
