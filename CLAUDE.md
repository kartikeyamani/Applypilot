# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ApplyPilot is a 6-stage autonomous job application pipeline: discover jobs across job boards and direct ATS platforms, enrich them with full descriptions, score them against the user's resume with an LLM, tailor a resume per job, generate a cover letter, then autonomously fill out and submit the application via a real browser. Distributed as a pip package (`applypilot` CLI, entry point `applypilot.cli:app`).

## Commands

### Setup (editable install is required, see gotcha below)
```bash
pip install -e ".[dev]"
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
playwright install chromium
```
The two-step jobspy install is required: `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver but works fine at runtime with any modern numpy. `--no-deps` bypasses the resolver; the second command installs jobspy's actual runtime deps.

### Running
```bash
applypilot init                    # interactive wizard: profile.json, resume, searches.yaml, .env
applypilot doctor                  # diagnose what's installed / which tier is unlocked
applypilot run [stages...]         # discover, enrich, score, tailor, cover, pdf (default: all)
applypilot run --stream            # run stages concurrently instead of sequentially
applypilot apply --limit N --url X # auto-apply, optionally to one specific job
applypilot status                  # DB stats
applypilot dashboard               # open the HTML results dashboard
```

### Tests
```bash
pytest tests/ -v
pytest tests/test_apply_acquire_job.py::test_acquire_job_finds_a_never_attempted_job_by_url -v  # single test
```
Tests use a real temporary SQLite file per test (see `tests/conftest.py`'s `temp_db` fixture), not mocks — most bugs in this codebase so far have been SQL/data-shape bugs a mocked connection would hide.

### Lint
```bash
ruff check src/
ruff check src/ --fix
ruff format src/
```

## Critical gotcha: editable install

`pip install -e .` is not optional for development. A regular `pip install .` copies the package into `site-packages`; source edits under `src/applypilot/` then silently do nothing until reinstalled, with no error or warning — the CLI just keeps running the stale copy. Always verify with:
```bash
python -c "import applypilot; print(applypilot.__file__)"
```
It should point into this repo's `src/applypilot/`, not a `site-packages` directory.

## Architecture

### The database is the pipeline

Every stage is a pure transformation over rows in one SQLite table (`~/.applypilot/applypilot.db`, or `$APPLYPILOT_DIR` if set), not a function call chain. A job's stage is entirely defined by which columns are `NULL` vs populated — e.g. "pending tailor" means `fit_score >= 7 AND full_description IS NOT NULL AND tailored_resume_path IS NULL`. `database.py`'s `_ALL_COLUMNS` dict is the single schema source of truth; `ensure_columns()` does additive-only migrations by diffing it against `PRAGMA table_info`. This is why stages can be run independently, killed and resumed, or (via `--stream`) run concurrently with threads polling the DB as a conveyor belt — read `pipeline.py`'s `_run_stage_streaming` before changing stage orchestration.

### Discover stage: four sub-scrapers in a deliberate order

`pipeline.py::_run_discover` runs, in order: **ats_boards** (Ashby/Greenhouse public JSON APIs, company slugs in `config/ats_boards.yaml`) → **workday** (undocumented CXS JSON API, employer registry in `config/employers.yaml`) → **jobspy** (Indeed/LinkedIn/Glassdoor/ZipRecruiter via the `python-jobspy` library) → **smartextract** (AI-picks-a-strategy scraper for arbitrary sites in `config/sites.yaml`). The order is intentional, not alphabetical: Ashby/Greenhouse and Workday applications are a single public form with no personal login required, while JobSpy's LinkedIn results need the user's own logged-in session during auto-apply, which is far less reliable. `config.DISCOVERY_CAP` (env: `APPLYPILOT_DISCOVERY_CAP`, default 50) short-circuits later scrapers once enough jobs exist in the DB — check via `database.count_jobs()`.

### Cost cascade: cheapest extraction wins

Two places use the same pattern — try free/deterministic first, fall back to an LLM call only as a last resort:
- **Enrichment** (`enrichment/detail.py`): JSON-LD → deterministic CSS selector patterns → LLM extraction.
- **Smart-extract discovery** (`discovery/smartextract.py`): the LLM only ever sees a compact *text briefing* (JSON-LD presence, intercepted API shapes, DOM stats) to pick a strategy, not raw HTML. Only the `css_selectors` strategy makes a second LLM call with actual (cleaned) HTML.

### LLM client (`llm.py`)

Single `LLMClient`, provider auto-detected from env vars via `_detect_provider()` in priority order **Anthropic > Gemini > OpenAI > local** (Anthropic checked first since it's always a deliberate paid opt-in). Gemini has a native-API fallback: a 403 on the OpenAI-compat endpoint switches to the native `generateContent` API for the rest of the process (needed for preview/experimental models). Anthropic has no OpenAI-compat endpoint at all — `_chat_anthropic()` talks to `/v1/messages` directly and converts the message format.

**Gemini model IDs get retired without warning**, and a stale `LLM_MODEL` produces an HTTP 404 that gets caught and recorded as `fit_score = 0` per-job (see `scorer.py`) rather than crashing the batch — so "everything scored 0" almost always means check `score_reasoning` for `LLM error:` text before assuming a logic bug. Relatedly, **Gemini free-tier quotas are tracked per model name, not per account/key** — a sibling model (e.g. `gemini-3.5-flash-lite` vs `gemini-3.6-flash`) often has untouched quota even when another is fully rate-limited.

Token budgets across scoring/tailoring/judging/cover-letter calls are sized generously (2048–8192) because some Gemini models emit a verbose internal reasoning trace before the real answer even on the plain chat endpoint — a low `max_tokens` can silently consume the whole budget on reasoning and never emit the actual output, which then fails downstream parsing with no obvious error pointing at the real cause.

### Tailoring's validation architecture (`scoring/tailor.py` + `scoring/validator.py`)

The LLM returns structured JSON only (title/summary/skills/experience/projects/education) — it never generates the final resume text. `assemble_resume_text()` (code, not the LLM) builds the plain-text document and always injects the profile's name/contact header, making header hallucination structurally impossible. Two independent validation layers run in sequence: `validate_json_fields()` (deterministic — required fields, `FABRICATION_WATCHLIST` scan, every `resume_facts.preserved_companies` entry must appear in an experience header, `LLM_LEAK_PHRASES` self-talk detection) then a second, separately-prompted LLM "judge" call (`judge_tailored_resume()`) that compares original vs tailored text. Each retry starts a **fresh** conversation rather than appending to the failed one, specifically to avoid the model spiraling into apologetic self-correction instead of just fixing the output.

`resume_facts.preserved_companies` must contain only genuine former employers, never the school (already captured separately by `preserved_school`) — this has been the single most common real validation failure, since the LLM correctly refuses to fabricate work experience at a university.

### Auto-apply (`apply/`): the one stage that delegates instead of calling an LLM directly

`apply/launcher.py::run_job` spawns a real `claude` CLI subprocess per job (`claude --model X -p --mcp-config ... --output-format stream-json`), piping a large profile-driven prompt (`apply/prompt.py::build_prompt`) over stdin. The agent drives a real Chrome instance that `apply/chrome.py` launches per worker with an isolated cloned profile, wired via Playwright MCP over a CDP port (`BASE_CDP_PORT + worker_id`). `acquire_job()` claims work atomically via `BEGIN IMMEDIATE` so parallel workers don't race on the same job — when touching its WHERE clauses, remember SQLite's three-valued logic: `apply_status != 'in_progress'` is `NULL` (not true) when `apply_status IS NULL`, which silently excludes every never-attempted job unless the clause is `(apply_status IS NULL OR apply_status != 'in_progress')`.

The agent's result is parsed out of free-form transcript text by searching for a fixed vocabulary of `RESULT:` markers (`APPLIED`, `EXPIRED`, `CAPTCHA`, `LOGIN_ISSUE`, `FAILED:<reason>`) — this is the entire contract between the agent's free-form reasoning and the deterministic DB update that follows. The pre-submit instruction in `prompt.py` is a mandatory numbered checklist (re-snapshot, re-verify every field against the profile, only then submit) rather than a soft suggestion, because agents will otherwise submit based on a stale mental model of what they filled in several tool calls earlier — form frameworks frequently reset field values on re-render.

`apply/prompt.py::_build_location_check` reads `searches.yaml`'s `location.accept_patterns` list, which is a **separate concept** from `defaults.location` (used for job *searching*, not apply-stage eligibility) — a location typed at `applypilot init` only affects both if the wizard's `_setup_searches()` derives `accept_patterns` from it, which it does by splitting on commas.

### Config layering — two different directories, don't confuse them

- **Package-shipped** (`src/applypilot/config/*.yaml`, versioned in this repo): `employers.yaml` (Workday registry), `sites.yaml` (direct scrape targets, blocklists, manual-ATS list, base URLs), `ats_boards.yaml` (Ashby/Greenhouse company slugs).
- **User-specific** (`~/.applypilot/`, never versioned, created by `applypilot init`): `profile.json`, `resume.txt`/`.pdf`, `searches.yaml`, `.env`, `applypilot.db`, `tailored_resumes/`, `cover_letters/`.

### Tier system (`config.py::get_tier`/`check_tier`)

CLI commands are gated by what's actually installed, detected live rather than a static config flag: Tier 1 (Python only) → Tier 2 (+ any LLM API key, unlocks score/tailor/cover) → Tier 3 (+ Claude Code CLI + Chrome + Node.js, unlocks `apply`). `applypilot doctor` surfaces the current tier and what's missing to unlock the next one.

## Note on CONTRIBUTING.md

Its "Project Structure" diagram and the `applypilot discover --employer`/`--site` example commands are stale relative to the current code — actual directory names are `discovery/`, `enrichment/`, `scoring/` (which holds scoring, tailoring, cover-letter, and PDF generation together, not separate top-level dirs), there is no `utils/` directory, and `config/` is packaged under `src/applypilot/config/`, not at the repo root. Verify against the actual `src/applypilot/` tree rather than that document when in doubt.
