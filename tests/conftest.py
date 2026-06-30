"""pytest configuration — helpers are in helpers.py."""
import sys
from pathlib import Path

import pytest

# Ensure src/ and tests/ are on the path for all test modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def clear_card_cache():
    """Clear the card cache, investment data cache, and in-flight futures before every test."""
    from emoney_mcp.scrapers import _helpers
    _helpers._card_cache.clear()
    _helpers._card_futures.clear()
    _helpers._inv_cache = None
    yield
    _helpers._card_cache.clear()
    _helpers._card_futures.clear()
    _helpers._inv_cache = None
