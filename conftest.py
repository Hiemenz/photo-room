"""
Repo-wide pytest setup.

The pre-existing suite drives the library through hand-written exiftool
doubles that replace ``subprocess.run`` and expect a write to have reached
"the file" by the time the response arrives. The production defaults (a
persistent exiftool process, queued background sidecar writes, a SQLite
index) are switched to their synchronous equivalents for those tests.
Tests of the new machinery opt back in with the ``fast_library`` fixture
in ``tests/conftest.py``-independent test modules (see tests/test_libindex.py).
"""
import pytest


@pytest.fixture(autouse=True)
def _legacy_library_mode(request, monkeypatch):
    import exifpool
    import libindex
    if request.node.get_closest_marker("fastlib") or request.node.fspath.basename.startswith("test_libindex"):
        yield
        libindex.shutdown_all()
        return
    monkeypatch.setattr(exifpool, "PERSISTENT", False)
    monkeypatch.setattr(libindex, "DB_ENABLED", False)
    monkeypatch.setattr(libindex, "SYNC", True)
    monkeypatch.setattr(libindex, "RESCAN_MAX_AGE", 0)
    yield
    libindex.shutdown_all()
