import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest


@pytest.fixture(autouse=True)
def _no_personal_config(monkeypatch):
    """Tests never read the developer's own gutshaus.json (GUTSHAUS_CONFIG)."""
    monkeypatch.delenv("GUTSHAUS_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _fast_kowr(monkeypatch):
    """One KOWR branch, no politeness pauses: the fake serves the same schedule for every branch."""
    from gutshaus import config
    monkeypatch.setattr(config, "KOWR_BRANCHES", ["szczecin"])
    monkeypatch.setattr(config, "KOWR_PAUSE_S", 0)
