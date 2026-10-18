import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Let tests import the grading scripts as plain modules (the scripts run as CLIs too), plus the
# segmentation validator to check that the input fixture is a valid sop-video-skill result.
sys.path.insert(0, str(ROOT / "sop-video-grading" / "scripts"))
sys.path.insert(0, str(ROOT / "sop-video-skill" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from grading_helpers import make_segmentation  # noqa: E402


@pytest.fixture
def segmentation() -> dict:
    return make_segmentation()
