"""Verify SQLite is opened in WAL with a busy_timeout (scheduler+UI concurrency)."""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")

from app.models.db import SessionLocal, init_db
from sqlalchemy import text


async def test_wal_and_busy_timeout_applied():
    await init_db()
    async with SessionLocal() as s:
        jm = (await s.execute(text("PRAGMA journal_mode"))).scalar()
        bt = (await s.execute(text("PRAGMA busy_timeout"))).scalar()
        sync = (await s.execute(text("PRAGMA synchronous"))).scalar()
    assert str(jm).lower() == "wal"
    assert int(bt) >= 5000
    assert int(sync) == 1  # NORMAL
