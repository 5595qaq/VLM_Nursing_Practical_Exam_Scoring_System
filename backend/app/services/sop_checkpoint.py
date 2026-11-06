"""Progress of a segmentation or grading run, saved before every model call.

Both loops rebuild each request from their state, so that state and the files it names are all a later
attempt needs to continue where the last one stopped (a 429, a dropped connection, a worker killed). The
fingerprint ties a checkpoint to the rules, code, model and inputs it was made with: when any differs the
checkpoint is ignored and the work starts over, so one result never mixes two versions.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Callable

from app.services import gcs_service

logger = logging.getLogger(__name__)
CHECKPOINT = "checkpoint.json"  # sop-video-skill: one per run
TASK_PREFIX = "checkpoint_task_"  # sop-video-grading: one per task
TEMPORARY = ".tmp"  # a checkpoint being written; never archived
UPLOAD_SECONDS = 60  # a queue worker keeps its checkpoint in GCS at most this stale


def fingerprint(rules: str, *inputs) -> str:
    """"" when the rules are unknown (tree_digest failed): such a checkpoint is never resumed."""
    if not rules:
        return ""
    text = json.dumps([rules, *inputs], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save(path: Path, data: dict) -> None:
    """Replace the file in one step: a run killed while writing leaves the previous checkpoint."""
    temporary = path.with_name(path.name + TEMPORARY)
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


def is_checkpoint(name: str) -> bool:
    return name == CHECKPOINT or (name.startswith(TASK_PREFIX) and name.endswith(".json"))


def needed_files(name: str, data: dict) -> set[str]:
    """What a checkpoint needs besides itself: the images and clips it shows the model, and its transcript."""
    if name == CHECKPOINT:
        return {"transcript.json", *(image for image, _ in data.get("pinned", []) + data.get("latest", []))}
    key = name[len(TASK_PREFIX):-len(".json")]
    return {f"transcript_task_{key}.json", *(material[0] for material in data.get("materials", []))}


def archive(zf: zipfile.ZipFile, workdir: Path, keep: Callable[[Path], bool]) -> None:
    """Add workdir's files that `keep` accepts to zf, plus every file a checkpoint needs (a grading task's
    clips, which `keep` leaves out). Tasks keep running next to a periodic upload, so the checkpoints are
    read first: the files they name already exist then and are in the listing that follows, whereas a
    checkpoint read after the listing could name an image made in between. A checkpoint being written is
    skipped, and a file gone mid-walk is left out instead of failing the upload."""
    needed: set[str] = set()
    for item in sorted(workdir.iterdir()):
        if not is_checkpoint(item.name):
            continue
        try:
            data = item.read_bytes()
            needed |= needed_files(item.name, json.loads(data))
        except (OSError, ValueError):
            continue
        zf.writestr(item.name, data)
    for item in sorted(workdir.iterdir()):
        if (is_checkpoint(item.name) or item.name.endswith(TEMPORARY) or not item.is_file()
                or not (keep(item) or item.name in needed)):
            continue
        try:
            zf.write(item, item.name)
        except FileNotFoundError:
            continue


class PeriodicUpload:
    """on_checkpoint hook starting `upload` in the background at most every `seconds`, so no model call
    waits for it. drain() before the final upload: an older upload finishing later would overwrite it.
    A failed upload only leaves an older checkpoint in GCS, so it never stops the run."""

    def __init__(self, upload: Callable[[], object], seconds: float = UPLOAD_SECONDS):
        self.upload, self.seconds = upload, seconds
        self.last = time.monotonic()
        self.running: asyncio.Task | None = None

    async def __call__(self) -> None:
        if (self.running and not self.running.done()) or time.monotonic() - self.last < self.seconds:
            return
        self.last = time.monotonic()
        self.running = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await asyncio.to_thread(self.upload)
        except Exception as exc:  # noqa: BLE001 - see above
            logger.warning("could not upload the checkpoint: %s", exc)

    async def drain(self) -> None:
        if self.running:
            await asyncio.shield(self.running)


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
    """Unpack the checkpoints in `prefix`/artifacts.zip, with only the files they need, into workdir; None
    when there is no archive. The attempt's results, rules copies and other transcripts stay out: they
    are not this run's. Blocking. Any failure means starting over, which costs tokens, not correctness."""
    object_name = f"{prefix}/artifacts.zip"
    downloaded = workdir.parent / "checkpoint_archive.zip"
    try:
        if not gcs_service.blob_exists(object_name):
            return None
        gcs_service.download_to_filename(gcs_service.gcs_uri_for(object_name), str(downloaded))
        with zipfile.ZipFile(downloaded) as zf:
            names = {name for name in zf.namelist() if Path(name).name == name}  # flat names only, as we upload them
            wanted = set()
            for name in filter(is_checkpoint, names):
                wanted |= {name, *needed_files(name, json.loads(zf.read(name)))}
            for name in wanted & names:
                (workdir / name).write_bytes(zf.read(name))
        if wanted:
            logger.warning("restored the checkpoint of an earlier attempt from %s", object_name)
        return bool(wanted)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.warning("could not restore a checkpoint from %s: %s", object_name, exc)
        return False
    finally:
        downloaded.unlink(missing_ok=True)
