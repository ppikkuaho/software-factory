"""Repo-root conftest.

Ensures the flat repo-root `harnessd/` package (IMPLEMENTATION-PLAN §3 module table)
is importable when the suite runs from the repo root without requiring an editable
install. The package layout is flat at the repo root, NOT a src/ layout.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

def pytest_configure(config):
    if sys.platform != "darwin" or config.option.basetemp is not None:
        return

    # macOS AF_UNIX sun_path is ~104 bytes; keep nested pytest socket paths under a short root.
    basetemp = Path(tempfile.mkdtemp(prefix="factory-pytest-", dir="/tmp"))
    config.option.basetemp = basetemp
    config._factory_created_basetemp = basetemp


def pytest_unconfigure(config):
    basetemp = getattr(config, "_factory_created_basetemp", None)
    if basetemp is not None:
        shutil.rmtree(basetemp)


@pytest.fixture(autouse=True)
def _restore_spawn_env_seam():
    """Save/restore the chokepoint's module-level SPAWN_ENV seam around every test.

    ``daemon.boot`` binds the runtime's spawn env into ``chokepoint.SPAWN_ENV`` (LT-1). A test
    that drives boot (or binds the seam directly) must not leak that binding into later tests,
    which pin the structural placeholder fallback (the dry-run shape).
    """
    from harnessd.spawn import chokepoint

    prior = chokepoint.SPAWN_ENV
    yield
    chokepoint.SPAWN_ENV = prior
