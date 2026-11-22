import subprocess

# One normalized object per upload: videos/{sha256}_src.mp4. Gemini, Gazelle and sop-video-skill
# all read it; Gazelle samples its own 5 FPS frames and the skill builds its own proxy.
NORMALIZED_SUFFIX = "_src.mp4"
GAZELLE_FPS = 5


def normalize_source(input_path: str, output_path: str) -> None:
    """Re-encode an upload without lowering resolution or frame rate.

    sop-video-skill zooms into the original resolution, and flick detection needs the
    original frame rate. H.264 decodes faster than HEVC, 1 s keyframes keep random seeks
    cheap, constant frame rate keeps timestamps exact, and re-encoding bakes in rotation.
    """
    command = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr",
        "-force_key_frames", "expr:gte(t,n_forced*1)",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(command, check=True)


def sample_segment(input_path: str, output_path: str, start_s: float, end_s: float,
                   fps: int = GAZELLE_FPS) -> None:
    """Cut [start_s, end_s) and resample it to `fps` frames per second (no audio)."""
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", str(start_s), "-to", str(end_s), "-i", input_path,
        "-vf", f"fps={fps}", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        output_path,
    ]
    subprocess.run(command, check=True)
