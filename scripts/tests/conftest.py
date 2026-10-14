import sys
from pathlib import Path

# Let tests import scripts/*.py as plain modules (the scripts run as CLIs too).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Same for the skill's own scripts (validate_action.py ships inside the skill).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sop-video-skill" / "scripts"))
