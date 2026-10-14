import sys
from pathlib import Path

# Let tests import the skill's scripts as plain modules (the scripts run as CLIs too).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sop-video-skill" / "scripts"))
