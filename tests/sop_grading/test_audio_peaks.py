"""Tests for audio_peaks.py (need ffmpeg/ffprobe)."""

import shutil
import subprocess
import wave

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

import audio_peaks  # noqa: E402

RATE = 16000
CLICKS = [1.0, 1.3, 1.6, 1.9, 2.2]  # a flick: five clicks in a row
SINGLE = 4.5  # a cap put down: one click


def _mux(tmp_path, name, audio_args):
    path = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=gray:size=160x120:rate=10:duration=6",
                    *audio_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)
    return str(path)


@pytest.fixture(scope="module")
def clicks(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("audio")
    rng = np.random.default_rng(0)
    signal = rng.normal(0, 0.002, 6 * RATE)  # quiet room noise
    for t in [*CLICKS, SINGLE]:
        i = int(t * RATE)
        signal[i:i + 160] += rng.normal(0, 0.05, 160) * np.hanning(160)  # 10 ms broadband click
    wav = tmp / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(signal, -1, 1) * 32767).astype(np.int16).tobytes())
    return _mux(tmp, "clicks.mp4", ["-i", str(wav), "-c:a", "aac"])


def test_clicks_become_peaks_and_one_run(clicks):
    result = audio_peaks.find_peaks(audio_peaks.read_levels(clicks, 0.0, 6.0, 2000), 0.0)
    assert not result["silent"] and result["floor_db"] < result["max_db"] - 6
    times = [p["t"] for p in result["peaks"]]
    assert all(any(abs(t - c) < 0.1 for t in times) for c in [*CLICKS, SINGLE])
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["n"] == len(CLICKS) and abs(run["start"] - CLICKS[0]) < 0.1 and abs(run["end"] - CLICKS[-1]) < 0.1


def test_window_offset(clicks):
    result = audio_peaks.find_peaks(audio_peaks.read_levels(clicks, 3.0, 6.0, 2000), 3.0)
    assert [round(p["t"]) for p in result["peaks"]] == [round(SINGLE)] and result["runs"] == []


def test_runs_need_three_close_peaks():
    peaks = [{"t": t, "db": -40} for t in (1.0, 1.5, 2.0, 5.0, 5.5, 9.0)]
    assert audio_peaks.find_runs(peaks) == [{"start": 1.0, "end": 2.0, "n": 3}]


def test_no_audio_track(tmp_path, capsys):
    video = _mux(tmp_path, "mute.mp4", ["-an"])
    assert audio_peaks.main(["x", video, "--start", "0", "--end", "5"]) == 0
    assert '"has_audio": false' in capsys.readouterr().out


def test_silent_track(tmp_path, capsys):
    video = _mux(tmp_path, "silent.mp4", ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-c:a", "aac"])
    assert audio_peaks.main(["x", video, "--start", "0", "--end", "5"]) == 0
    out = capsys.readouterr().out
    assert '"has_audio": true' in out and '"silent": true' in out and '"peaks": []' in out


def test_span_rejected(clicks):
    assert audio_peaks.main(["x", clicks, "--start", "5", "--end", "2"]) == 2
