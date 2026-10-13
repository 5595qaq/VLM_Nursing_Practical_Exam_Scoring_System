import sys
from pathlib import Path

# Let tests import scripts/*.py as plain modules (the scripts run as CLIs too).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
