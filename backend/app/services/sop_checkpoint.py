"""Progress of a segmentation or grading run, saved before every model call.

Both loops rebuild each request from their state, so that state and the files it names are all a later
attempt needs to continue where the last one stopped (a 429, a dropped connection, a worker killed). The
fingerprint ties a checkpoint to the rules and inputs it was made with: when they differ the checkpoint is
ignored and the work starts over, so one result never mixes two versions of the rules.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Awaitable, Callable

from app.services import gcs_service

logger = logging.getLogger(__name__)
CHECKPOINT = "checkpoint.json"  # sop-video-skill: one per run
TASK_PREFIX = "checkpoint_task_"  # sop-video-grading: one per task
UPLOAD_SECONDS = 60  # a queue worker keeps its checkpoint in GCS at most this stale


def fingerprint(rules: str, *inputs) -> str:
    """"" when the rules are unknown (tree_digest failed): such a checkpoint is never resumed."""
    if not rules:
        return ""
    text = json.dumps([rules, *inputs], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save(path: Path, data: dict) -> None:
    """Replace the file in one step: a run killed while writing leaves the previous checkpoint."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def load(path: Path, expected: str) -> dict | None:
    """The checkpoint, or None when there is none, it cannot be read, or it was made from something else."""
    if not expected:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("fingerprint") == expected else None


def files_exist(workdir: Path, names: list[str]) -> bool:
    return all((workdir / name).is_file() for name in names)


def mark_reported(path: Path) -> None:
    """The run reporting this task's tokens is over: a later attempt counts only the tokens it adds."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    save(path, {**data, "usage": {}})


def task_clips(workdir: Path) -> set[str]:
    """Clips that unfinished grading tasks still show the model; they go into the archive with their checkpoint."""
    names = set()
    for path in workdir.glob(f"{TASK_PREFIX}*.json"):
        try:
            materials = json.loads(path.read_text(encoding="utf-8")).get("materials") or []
        except (OSError, ValueError):
            continue
        names |= {name for name, _, kind, _ in materials if kind == "video"}
    return names


def throttled(upload: Callable[[], object], seconds: float = UPLOAD_SECONDS) -> Callable[[], Awaitable[None]]:
    """on_checkpoint hook running `upload` at most every `seconds`. A failed upload only leaves an older
    checkpoint in GCS, so it never stops the run."""
    last = time.monotonic()
    busy = False

    async def hook() -> None:
        nonlocal last, busy
        if busy or time.monotonic() - last < seconds:
            return
        busy, last = True, time.monotonic()
        try:
            await asyncio.to_thread(upload)
        except Exception as exc:  # noqa: BLE001 - see above
            logger.warning("could not upload the checkpoint: %s", exc)
        finally:
            busy = False

    return hook


def restore_latest(prefix_of: Callable[[int], str], generation: int, workdir: Path) -> bool:
    """Continue from the newest attempt that left an archive. "Resume from failure" starts a new generation
    with an empty folder, so a failed job's progress is found in the generations before it. An archive
    without a checkpoint ends the search: that attempt finished its work or dropped the checkpoint on purpose
    (a refusal, too many invalid actions), so anything older must not come back."""
    for number in range(generation, -1, -1):
        restored = _restore_from_gcs(prefix_of(number), workdir)
        if restored is not None:
            return restored
    return False


def _restore_from_gcs(prefix: str, workdir: Path) -> bool | None:
    """Unpack `prefix`/artifacts.zip into workdir when it holds a checkpoint; None when there is no archive.
    Blocking. Any failure means starting over, which costs tokens but gives the same result."""
    object_name = f"{prefix}/artifacts.zip"
    archive = workdir.parent / "earlier_artifacts.zip"
    try:
        if not gcs_service.blob_exists(object_name):
            return None
        gcs_service.download_to_filename(gcs_service.gcs_uri_for(object_name), str(archive))
        with zipfile.ZipFile(archive) as zf:
            names = [name for name in zf.namelist() if Path(name).name == name]  # flat names only, as we upload them
            if not any(name == CHECKPOINT or name.startswith(TASK_PREFIX) for name in names):
                return False
            for name in names:
                (workdir / name).write_bytes(zf.read(name))
        logger.warning("restored the checkpoint of an earlier attempt from %s", object_name)
        return True
    except Exception as exc:  # noqa: BLE001 - see above
        logger.warning("could not restore a checkpoint from %s: %s", object_name, exc)
        return False
    finally:
        archive.unlink(missing_ok=True)
