import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from app.services import video_service


def test_normalize_source_keeps_resolution_and_frame_rate():
    with patch("app.services.video_service.subprocess.run") as mock_run:
        video_service.normalize_source("in.mp4", "out.mp4")

    mock_run.assert_called_once()
    command = mock_run.call_args[0][0]
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == "in.mp4" and command[-1] == "out.mp4"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-crf") + 1] == "18"
    assert command[command.index("-fps_mode") + 1] == "cfr"
    assert command[command.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*1)"
    # No resampling or scaling: the skill zooms into the original pixels and frames.
    assert not any(arg.startswith(("fps=", "scale=")) for arg in command)
    assert "-vf" not in command and "-r" not in command


ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,codec_name",
        "-of", "json", str(path)])
    return json.loads(out)["streams"][0]


def keyframe_times(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)], text=True)
    return [float(line.strip(",")) for line in out.split()]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("src") / "upload.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=duration=6", "-c:v", "mpeg4", "-c:a", "aac", str(path)], check=True)
    return path


@ffmpeg
def test_normalize_source_real_output(source, tmp_path):
    out = tmp_path / "out_src.mp4"
    video_service.normalize_source(str(source), str(out))
    stream = probe(out)
    assert stream["codec_name"] == "h264"
    assert (stream["width"], stream["height"]) == (640, 360)
    assert stream["avg_frame_rate"] == "30/1" and int(stream["nb_read_frames"]) == 180
    keys = keyframe_times(out)
    assert keys[:6] == pytest.approx([0, 1, 2, 3, 4, 5], abs=0.05)


@ffmpeg
def test_sample_segment_resamples_to_gazelle_fps(source, tmp_path):
    out = tmp_path / "seg.mp4"
    video_service.sample_segment(str(source), str(out), 1, 4)
    stream = probe(out)
    assert stream["avg_frame_rate"] == "5/1"
    assert int(stream["nb_read_frames"]) == 15  # 3 s at 5 FPS -> 200 ms per frame
