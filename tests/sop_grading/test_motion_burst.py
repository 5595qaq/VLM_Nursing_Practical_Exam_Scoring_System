"""Tests for motion_peaks.py and burst.py (need ffmpeg/ffprobe; burst also needs OpenCV)."""

import json
import shutil
import subprocess

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

import burst  # noqa: E402
import motion_peaks  # noqa: E402

FPS = 30


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    """6 s of a still grey frame with a white square that moves only during 2.0-2.4 s and 4.0-4.3 s."""
    path = tmp_path_factory.mktemp("motion") / "v.mp4"
    frames = []
    x = 40
    for i in range(6 * FPS):
        t = i / FPS
        if 2.0 <= t < 2.4 or 4.0 <= t < 4.3:
            x += 12
        img = np.full((180, 320), 90, np.uint8)
        img[60:110, x % 260:x % 260 + 50] = 255
        frames.append(img)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "320x180",
                             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", str(path)],
                            stdin=subprocess.PIPE)
    proc.communicate(b"".join(f.tobytes() for f in frames))
    return str(path)


def test_peaks_land_on_the_movement(video):
    frames = motion_peaks.read_gray(video, 0.0, 6.0, [0, 0, 1, 1], 15)
    peaks = motion_peaks.find_peaks(frames, 15, 0.0, top=2)
    assert len(peaks) == 2
    assert 1.9 <= peaks[0]["t"] <= 2.6 and 3.9 <= peaks[1]["t"] <= 4.5


def test_peaks_respect_window_offset_and_gap(video):
    frames = motion_peaks.read_gray(video, 3.0, 6.0, [0, 0, 1, 1], 15)
    peaks = motion_peaks.find_peaks(frames, 15, 3.0, top=8, gap=0.5)
    assert peaks and all(3.0 <= p["t"] <= 6.0 for p in peaks)
    assert all(b["t"] - a["t"] > 0.5 for a, b in zip(peaks, peaks[1:]))
    assert any(3.9 <= p["t"] <= 4.5 for p in peaks)


def test_still_video_has_no_peaks():
    assert motion_peaks.find_peaks(np.zeros((30, 10, 10), np.float32), 15, 0.0) == []


def test_motion_cli(video, tmp_path, capsys):
    out = tmp_path / "p.json"
    assert motion_peaks.main(["x", video, "--start", "1", "--end", "5", "--box", "0,0,1,1", "--top", "3",
                              "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["peaks"]
    assert motion_peaks.main(["x", video, "--start", "5", "--end", "1", "--box", "0,0,1,1"]) == 2


@pytest.mark.parametrize("start, end, n, message", [
    (1.0, 1.1, 9, "span"), (1.0, 5.0, 9, "span"), (1.0, 2.0, 2, "n=2"), (1.0, 2.0, 13, "n=13")])
def test_burst_limits(start, end, n, message):
    with pytest.raises(ValueError, match=message):
        burst.burst_times(start, end, n)


def test_burst_times_are_even():
    assert burst.burst_times(2.0, 3.0, 5) == [2.0, 2.25, 2.5, 2.75, 3.0]


def test_burst_renders_tiled_image(video, tmp_path, capsys):
    pytest.importorskip("cv2")
    out = tmp_path / "b.jpg"
    assert burst.main(["x", video, "--start", "2.0", "--end", "2.4", "--n", "6", "--box", "0.1,0.2,0.6,0.8",
                       "--rel-to-crop", "0,0,1,1", "--out", str(out)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert len(info["times"]) == 6 and out.stat().st_size > 0
    import cv2
    h, w = cv2.imread(str(out)).shape[:2]
    assert w > h  # 6 cells in 3 columns, 2 rows
    assert burst.main(["x", video, "--start", "2", "--end", "9", "--box", "0,0,1,1", "--out", str(out)]) == 2
