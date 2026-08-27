"""Direct ATS job-board discovery: Ashby and Greenhouse public APIs.

Both platforms expose a public, unauthenticated JSON API for their job boards --
zero LLM, zero browser, same pattern as the Workday scraper. Applications on
these platforms are usually a single public form on the company's own site,
with no personal LinkedIn/SSO login required -- which makes them the most
reliable target for the autonomous apply stage.

Company registry is loaded from config/ats_boards.yaml instead of hardcoded.
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone

import httpx
import yaml

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db
from applypilot.discovery.workday import strip_html, _location_ok

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_TIMEOUT = 20


# -- Company registry from YAML ----------------------------------------------

def load_boards() -> dict:
    """Load Ashby/Greenhouse company slugs from config/ats_boards.yaml."""
    path = CONFIG_DIR / "ats_boards.yaml"
    if not path.exists():
        log.warning("ats_boards.yaml not found at %s", path)
        return {"ashby": [], "greenhouse": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"ashby": data.get("ashby", []), "greenhouse": data.get("greenhouse", [])}


# -- Ashby ---------------------------------------------------------------

def fetch_ashby_board(slug: str) -> list[dict]:
    """Fetch all listed jobs for one company's Ashby board."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": UA})
        if resp.status_code != 200:
            return []
        return resp.json().get("jobs", [])
    except Exception as e:
        log.warning("Ashby [%s]: fetch failed: %s", slug, e)
        return []


def _ashby_job_to_row(job: dict, slug: str) -> dict | None:
    url = job.get("jobUrl") or job.get("applyUrl")
    if not url:
        return None
    desc = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml", ""))
    location = job.get("location", "")
    if job.get("isRemote"):
        location = f"{location} (Remote)" if location else "Remote"
    return {
        "url": url,
        "title": job.get("title", ""),
        "location": location,
        "full_description": desc,
        "apply_url": job.get("applyUrl") or url,
        "site": f"Ashby: {slug}",
    }


# -- Greenhouse ------------------------------------------------------------

def fetch_greenhouse_board(slug: str) -> list[dict]:
    """Fetch all listed jobs for one company's Greenhouse board (with content)."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = httpx.get(url, params={"content": "true"}, timeout=_TIMEOUT, headers={"User-Agent": UA})
        if resp.status_code != 200:
            return []
        return resp.json().get("jobs", [])
    except Exception as e:
        log.warning("Greenhouse [%s]: fetch failed: %s", slug, e)
        return []


def _greenhouse_job_to_row(job: dict, slug: str) -> dict | None:
    url = job.get("absolute_url")
    if not url:
        return None
    desc = strip_html(job.get("content", ""))
    loc = job.get("location", {})
    location = loc.get("name", "") if isinstance(loc, dict) else str(loc or "")
    return {
        "url": url,
        "title": job.get("title", ""),
        "location": location,
        "full_description": desc,
        "apply_url": url,
        "site": job.get("company_name") or f"Greenhouse: {slug}",
    }


# -- Shared filtering + storage -----------------------------------------

def _matches_query(title: str, query: str) -> bool:
    """Loose match: any significant word from the query appears in the title."""
    if not query:
        return True
    words = [w.lower() for w in query.split() if len(w) > 2]
    title_lower = title.lower()
    return any(w in title_lower for w in words)


def _store_rows(conn: sqlite3.Connection, rows: list[dict], strategy: str,
                accept_locs: list[str], reject_locs: list[str]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    for row in rows:
        if not _location_ok(row["location"], accept_locs, reject_locs):
            continue
        full_desc = row["full_description"] or None
        short_desc = full_desc[:500] if full_desc else None
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["url"], row["title"], None, short_desc, row["location"], row["site"],
                 strategy, now, full_desc, row["apply_url"],
                 now if full_desc and len(full_desc) > 200 else None),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()
    return new, existing


# -- Public entry points --------------------------------------------------

def run_ats_board_discovery(workers: int = 1) -> dict:
    """Discover jobs directly from Ashby and Greenhouse company job boards.

    Runs before Workday and JobSpy in the discovery order -- these platforms
    host a single public application form with no personal login required,
    making them the most reliable target for autonomous apply.

    Returns:
        Dict with stats: new, existing, companies_checked.
    """
    boards = load_boards()
    search_cfg = config.load_search_config()
    queries = [q["query"] for q in search_cfg.get("queries", [])]
    accept_locs = search_cfg.get("location_accept", [])
    reject_locs = search_cfg.get("location_reject_non_remote", [])

    init_db()
    conn = get_connection()

    total_new = 0
    total_existing = 0
    checked = 0

    for slug in boards.get("ashby", []):
        if config.DISCOVERY_CAP:
            current = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            if current >= config.DISCOVERY_CAP:
                log.info("Discovery cap of %d reached — stopping Ashby scan early.", config.DISCOVERY_CAP)
                break
        jobs = fetch_ashby_board(slug)
        checked += 1
        rows = [
            r for j in jobs
            if (r := _ashby_job_to_row(j, slug)) and (not queries or _matches_query(r["title"], " ".join(queries)))
        ]
        # If query filtering leaves nothing but the board has jobs, fall back to per-query matching
        if not rows and jobs and queries:
            for q in queries:
                rows.extend(
                    r for j in jobs
                    if (r := _ashby_job_to_row(j, slug)) and _matches_query(r["title"], q)
                )
        new, existing = _store_rows(conn, rows, "ashby_api", accept_locs, reject_locs)
        total_new += new
        total_existing += existing
        if new or existing:
            log.info("Ashby [%s]: %d listed, %d matched -> %d new, %d dupes", slug, len(jobs), len(rows), new, existing)

    for slug in boards.get("greenhouse", []):
        if config.DISCOVERY_CAP:
            current = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            if current >= config.DISCOVERY_CAP:
                log.info("Discovery cap of %d reached — stopping Greenhouse scan early.", config.DISCOVERY_CAP)
                break
        jobs = fetch_greenhouse_board(slug)
        checked += 1
        rows = []
        for q in (queries or [""]):
            rows.extend(
                r for j in jobs
                if (r := _greenhouse_job_to_row(j, slug)) and _matches_query(r["title"], q)
            )
        # de-dupe rows within this board (a job can match multiple queries)
        seen = set()
        deduped = []
        for r in rows:
            if r["url"] not in seen:
                seen.add(r["url"])
                deduped.append(r)
        new, existing = _store_rows(conn, deduped, "greenhouse_api", accept_locs, reject_locs)
        total_new += new
        total_existing += existing
        if new or existing:
            log.info("Greenhouse [%s]: %d listed, %d matched -> %d new, %d dupes", slug, len(jobs), len(deduped), new, existing)

    log.info("ATS board discovery done: %d companies checked, %d new, %d dupes", checked, total_new, total_existing)
    return {"new": total_new, "existing": total_existing, "companies_checked": checked}
