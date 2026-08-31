"""Shared pytest fixtures.

Tests run against a real temporary SQLite file (not mocks) so they exercise
the actual schema and queries -- the bugs this suite guards against were all
SQL/data-shape bugs that a mocked connection would have hidden.
"""

import sqlite3
from pathlib import Path

import pytest

from applypilot import database


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> Path:
    """Point applypilot.database at a fresh temp SQLite file for one test.

    Clears the thread-local connection cache before and after so tests
    never see a connection left open by a previous test or a real run.
    """
    db_path = tmp_path / "test_applypilot.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database._local.__dict__.clear()

    database.init_db(db_path)

    yield db_path

    database._local.__dict__.clear()


def insert_job(conn: sqlite3.Connection, url: str, **overrides) -> None:
    """Insert a minimal job row for tests, with sane defaults.

    Any column can be overridden via kwargs, e.g. insert_job(conn, url,
    apply_status="in_progress", fit_score=9).
    """
    defaults = {
        "url": url,
        "title": "Test Job",
        "site": "Ashby: testco",
        "fit_score": 8,
        "full_description": "A real job description, long enough to count.",
        "tailored_resume_path": str(url) + ".txt",
        "application_url": None,
        "apply_status": None,
        "apply_attempts": 0,
    }
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", list(defaults.values()))
    conn.commit()
