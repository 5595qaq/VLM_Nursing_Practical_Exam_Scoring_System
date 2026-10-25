"""Tests for clip.py (needs ffmpeg/ffprobe)."""

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

from clip import cut, has_audio, main  # noqa: E402


def make_video(path, audio: bool):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=8"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=duration=8", "-c:a", "aac"]
    subprocess.check_call(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    return str(path)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    root = tmp_path_factory.mktemp("clip")
    return {"audio": make_video(root / "a.mp4", True), "silent": make_video(root / "s.mp4", False)}


def test_clip_keeps_audio_and_crops(videos, tmp_path):
    out = tmp_path / "c.mp4"
    info = cut(videos["audio"], 2.0, 5.0, str(out), box=[0.0, 0.0, 0.5, 0.5], width=160)
    assert info["has_audio"] is True and has_audio(str(out))
    assert (info["start"], info["end"]) == (2.0, 5.0) and info["bytes"] == out.stat().st_size
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width:format=duration",
         "-of", "json", str(out)]))
    assert probe["streams"][0]["width"] == 160
    assert abs(float(probe["format"]["duration"]) - 3.0) < 0.3


def test_clip_without_audio_track(videos, tmp_path):
    info = cut(videos["silent"], 0.0, 2.0, str(tmp_path / "c.mp4"))
    assert info["has_audio"] is False


def test_end_is_clamped_to_video(videos, tmp_path):
    assert cut(videos["audio"], 6.0, 20.0, str(tmp_path / "c.mp4"))["end"] == pytest.approx(8.0, abs=0.1)


@pytest.mark.parametrize("start, end, message", [
    (5.0, 5.0, "outside the video"), (9.0, 12.0, "outside the video")])
def test_bad_span_is_rejected(videos, tmp_path, start, end, message):
    with pytest.raises(ValueError, match=message):
        cut(videos["audio"], start, end, str(tmp_path / "c.mp4"))


def test_too_long_is_rejected(videos, tmp_path, monkeypatch):
    import clip
    monkeypatch.setattr(clip, "MAX_SECONDS", 2.0)
    with pytest.raises(ValueError, match="at most 2"):
        cut(videos["audio"], 0.0, 5.0, str(tmp_path / "c.mp4"))


def test_cli_exit_codes(videos, tmp_path, capsys):
    out = tmp_path / "c.mp4"
    assert main(["clip.py", videos["audio"], "--start", "1", "--end", "2", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["has_audio"] is True
    assert main(["clip.py", videos["audio"], "--start", "3", "--end", "1", "--out", str(out)]) == 2


def test_cli_region_is_relative_to_the_context_box(videos, tmp_path, monkeypatch):
    import clip
    seen = {}
    monkeypatch.setattr(clip, "cut", lambda video, start, end, out, box, width: seen.setdefault("box", box) and {})
    out = str(tmp_path / "c.mp4")
    main(["clip.py", videos["audio"], "--start", "1", "--end", "2", "--box", "0.5,0,1,0.5",
          "--rel-to-crop", "0.2,0.2,0.6,1.0", "--out", out])
    assert seen["box"] == pytest.approx([0.4, 0.2, 0.6, 0.6])
