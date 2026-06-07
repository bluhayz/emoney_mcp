"""pytest configuration — helpers are in helpers.py."""
import sys
from pathlib import Path

# Ensure src/ and tests/ are on the path for all test modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
