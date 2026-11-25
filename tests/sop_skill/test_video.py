"""Tests for _video.grab near the end of a video (needs ffmpeg, OpenCV and numpy)."""

import shutil
import subprocess

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

import _video  # noqa: E402
from _video import grab, make_proxy, probe, proxy_complete, proxy_short_tolerance  # noqa: E402


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


def _cut(src, dst, seconds):
    subprocess.check_call(["ffmpeg", "-v", "error", "-y", "-i", src, "-t", str(seconds), "-an",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", dst])


def test_proxy_complete_allows_the_tolerance_but_not_a_truncated_file(tmp_path, video):
    short = str(tmp_path / "short.mp4")
    _cut(video, short, 1.0)
    assert proxy_complete(video, probe(video)["video_duration"])
    assert proxy_complete(short, 2.5, tolerance=2.0)
    assert not proxy_complete(short, 2.5, tolerance=1.0)
    assert not proxy_complete(str(tmp_path / "missing.mp4"), 2.5)


def test_make_proxy_rebuilds_a_truncated_proxy_with_a_matching_sidecar(tmp_path, video, monkeypatch):
    """Issue 216: ffmpeg cut the proxy short but exited 0; the sidecar still matched the source."""
    monkeypatch.setattr(_video, "proxy_short_tolerance", lambda _d: 0.8)
    dst = str(tmp_path / "proxy.mp4")
    make_proxy(video, dst)
    good = probe(dst)["duration"]
    _cut(video, dst, 1.0)  # truncated proxy, sidecar unchanged
    assert probe(dst)["duration"] < good - 0.8
    make_proxy(video, dst)
    assert probe(dst)["duration"] == pytest.approx(good, abs=0.2)


def test_make_proxy_rejects_a_truncated_transcode(tmp_path, video, monkeypatch):
    real_popen = subprocess.Popen

    def short_transcode(cmd, *a, **kw):  # the segment transcode writes only 1 s
        cmd = list(cmd)
        if "-t" in cmd:  # not ffprobe, which also goes through Popen
            cmd[cmd.index("-t") + 1] = "1.0"
        return real_popen(cmd, *a, **kw)

    monkeypatch.setattr(_video, "proxy_short_tolerance", lambda _d: 0.8)
    monkeypatch.setattr(_video.subprocess, "Popen", short_transcode)
    dst = tmp_path / "proxy.mp4"
    with pytest.raises(RuntimeError, match="truncated"):
        make_proxy(video, str(dst), jobs=1)
    assert not dst.exists() and not (tmp_path / "proxy.mp4.json").exists()


def test_probe_reports_the_video_stream_apart_from_the_audio_tail(video):
    info = probe(video)
    assert info["video_duration"] == pytest.approx(2.0, abs=0.15)
    assert info["duration"] > info["video_duration"] + 0.3  # 2.5 s of audio


def test_proxy_of_a_source_with_an_audio_tail_is_accepted(tmp_path, video, monkeypatch):
    """The proxy has no audio: compare it with the source's video stream, not the container."""
    monkeypatch.setattr(_video, "proxy_short_tolerance", lambda _d: 0.3)
    make_proxy(video, str(tmp_path / "proxy.mp4"))


@pytest.mark.parametrize("duration, expected", [(60, 2.0), (648, 6.48)])
def test_tolerance_grows_with_the_video(duration, expected):
    assert proxy_short_tolerance(duration) == pytest.approx(expected)
