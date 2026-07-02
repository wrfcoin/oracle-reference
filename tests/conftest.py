import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sources import registry
from app.sources.fixture_source import fixture_sources

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fixture_registry():
    """Attach recorded FixtureSources to the singleton registry, restoring the
    previous state afterwards (test_health asserts the unattached default)."""
    saved_sources = registry._sources
    saved_statuses = copy.deepcopy(registry._statuses)
    registry.attach(fixture_sources(FIXTURES))
    yield registry
    registry._sources = saved_sources
    registry._statuses = saved_statuses


@pytest.fixture()
def client(fixture_registry):
    """TestClient WITHOUT lifespan (which would attach real adapters) —
    fixture sources are already attached by fixture_registry."""
    return TestClient(app)
