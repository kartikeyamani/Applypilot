"""Regression tests for apply/launcher.py::acquire_job.

Bug this guards against (found during real use): SQLite's three-valued logic
means `apply_status != 'in_progress'` is NULL (not TRUE) when apply_status
IS NULL -- which is the case for every job that has never been attempted.
That silently excluded every untouched job from --url targeting, so
`applypilot apply --url <ready job>` reported "no matching job" and exited
having done nothing, for every job in the queue.
"""

from applypilot.apply.launcher import acquire_job
from applypilot.database import get_connection

from .conftest import insert_job


def test_acquire_job_finds_a_never_attempted_job_by_url(temp_db):
    """A job with apply_status = NULL (never attempted) must be acquirable via --url.

    This is the exact regression: before the fix, this returned None for
    every job that had never been attempted, i.e. almost every job that
    exists.
    """
    conn = get_connection()
    url = "https://jobs.ashbyhq.com/testco/abc123"
    insert_job(conn, url, apply_status=None)

    job = acquire_job(target_url=url, worker_id=0)

    assert job is not None
    assert job["url"] == url


def test_acquire_job_finds_a_previously_failed_job_by_url(temp_db):
    """A job that failed a prior attempt should also be retryable via --url."""
    conn = get_connection()
    url = "https://jobs.ashbyhq.com/testco/def456"
    insert_job(conn, url, apply_status="failed", apply_error="not_eligible_location")

    job = acquire_job(target_url=url, worker_id=0)

    assert job is not None
    assert job["url"] == url


def test_acquire_job_does_not_grab_a_job_another_worker_is_actively_on(temp_db):
    """apply_status = 'in_progress' must still be excluded -- this is the one
    status --url targeting should always skip, to avoid two workers racing
    on the same application.
    """
    conn = get_connection()
    url = "https://jobs.ashbyhq.com/testco/ghi789"
    insert_job(conn, url, apply_status="in_progress")

    job = acquire_job(target_url=url, worker_id=1)

    assert job is None


def test_acquire_job_returns_none_for_untailored_job(temp_db):
    """A job without a tailored resume yet is not ready to apply to, even
    if explicitly targeted by URL.
    """
    conn = get_connection()
    url = "https://jobs.ashbyhq.com/testco/jkl012"
    insert_job(conn, url, tailored_resume_path=None)

    job = acquire_job(target_url=url, worker_id=0)

    assert job is None
