"""Tests for _video.grab near the end of a video (needs ffmpeg, OpenCV and numpy)."""

import shutil
import subprocess

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

from _video import grab, probe  # noqa: E402


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    """2 s of 10 fps video with 2.5 s of audio: the container is longer than the last frame."""
    path = tmp_path_factory.mktemp("video") / "clip.mp4"
    subprocess.check_call([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
        "-f", "lavfi", "-i", "sine=duration=2.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    return str(path)


def test_grab_inside_video(video):
    assert grab(video, 1.0).shape[:2] == (120, 160)


@pytest.mark.parametrize("offset", [0.0, -0.05, 0.3])
def test_grab_at_or_past_last_frame_returns_last_frame(video, offset):
    t = probe(video)["duration"] + offset
    assert grab(video, t).shape[:2] == (120, 160)


def test_grab_without_any_frame_raises(video):
    with pytest.raises(RuntimeError):
        grab(video, 60.0)
