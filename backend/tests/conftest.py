"""Shared test setup.

Two jobs, both done *before* any `app.*` module is imported:

1. Point the app's data dir at a throwaway temp directory. `app.core.config`
   builds `settings` (and mkdirs `projects/` + `credentials/`) at import time, so
   the override has to happen here, in conftest, which pytest loads first. This
   guarantees a test run never reads or writes the real `/data` volume.

2. Stub the heavy native dependency `ansible_runner` if it isn't installed, so
   `app.core.runner` imports in a plain venv. Tests that actually execute Ansible
   are out of scope for unit tests; we only exercise the pure helpers (e.g.
   `summarize`). In the Docker image the real package is present and the stub is
   skipped.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

# --- 1. Isolate data dir -----------------------------------------------------
_TMP_DATA = Path(tempfile.mkdtemp(prefix="ansible-gui-tests-"))
os.environ["ANSIBLE_GUI_DATA_DIR"] = str(_TMP_DATA)


# --- 2. Stub ansible_runner if absent ---------------------------------------
def _ensure_importable(name: str) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = types.ModuleType(name)


_ensure_importable("ansible_runner")


import pytest  # noqa: E402


@pytest.fixture
def data_dir() -> Path:
    return _TMP_DATA


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """A bare directory that looks like an Ansible project root (no git)."""
    (tmp_path / "playbooks").mkdir()
    (tmp_path / "inventories" / "production").mkdir(parents=True)
    return tmp_path
