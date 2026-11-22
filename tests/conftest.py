import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest


@pytest.fixture(autouse=True)
def _no_personal_config(monkeypatch):
    """Tests never read the developer's own gutshaus.json (GUTSHAUS_CONFIG)."""
    monkeypatch.delenv("GUTSHAUS_CONFIG", raising=False)
