# Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases
- Written in plain language, the same way you'd brief someone on your team

**Layer 2: Agents (The Decision-Maker)**
- This is your role. You're responsible for intelligent coordination.
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
- You connect intent to execution without trying to do everything yourself
- Example: If you need to pull data from a website, don't attempt it directly. Read `workflows/scrape_website.md`, figure out the required inputs, then execute `sleeve_notes/scrape_single_site.py`

**Layer 3: Tools (The Execution)**
- Python scripts in `sleeve_notes/` (the project's Python package) that do the actual work
- API calls, data transformations, file operations, database queries
- Credentials and API keys are stored in `.env`
- These scripts are consistent, testable, and fast

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you're down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

## How to Operate

**1. Look for existing tools first**
Before building anything new, check `sleeve_notes/` based on what your workflow requires. Only create new scripts when nothing exists for that task.

**2. Learn and adapt when things fail**
When you hit an error:
- Read the full error message and trace
- Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
- Document what you learned in the workflow (rate limits, timing quirks, unexpected behavior)
- Example: You get rate-limited on an API, so you dig into the docs, discover a batch endpoint, refactor the tool to use it, verify it works, then update the workflow so this never happens again

**3. Keep workflows current**
Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don't create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

## File Structure

**What goes where:**
- **Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
- **Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
```
.tmp/             # Temporary files (scraped data, intermediate exports). Regenerated as needed.
sleeve_notes/     # Python scripts for deterministic execution (Layer 3: Tools)
sleeve_notes_web/ # Localhost web UI (FastAPI + HTMX + Jinja) — see "Web UI conventions"
workflows/        # Markdown SOPs defining what to do and how
.env              # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json  # Google OAuth (gitignored)
```

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in `.tmp/` is disposable.

## Web UI conventions

The web UI lives in `sleeve_notes_web/` (FastAPI + HTMX + Jinja, launched via `sleeve-notes web`). It is a thin presentation layer on top of the same CLI engine. These conventions have been hard-won — please don't rediscover them.

**Action-oriented, not pipeline-oriented.** The CLI thinks in pipeline steps (fetch → bpm → render). The web UI does NOT expose that abstraction. Each long-running task is a discrete sidebar action with a tailored modal — "Discogs sync", "Import Discogs csv", "BPM lookup", "Generate stickers" — never a generic "step picker + freeform args" panel. If a new long-running CLI subcommand lands, add it as a named action, not as an option in a dropdown.

**Sidebar shape.** Three labelled sections in this order: **Collection** (Releases), **Actions** (the named jobs), **Custom settings** (Overrides). The Dashboard is the homepage (`/`) reached via the "Sleeve Notes" wordmark click; it is NOT a sidebar item. The wordmark is title-cased ("Sleeve Notes", not "sleeve-notes"), larger than nav items, with only the app version underneath — no host name.

**Background jobs go through `JobRunner`.** Web routes that trigger work shell out to a `sleeve-notes <subcommand>` subprocess via `services.jobs.runner` — they do NOT call engine code in-process. One job runs at a time; the start endpoint refuses a new job while one is in flight. `runner.cancel()` MUST escalate SIGTERM → SIGKILL via a background watchdog (~3s grace), because the BPM cascade has non-daemon worker threads that block subprocess exit when stuck in network I/O.

**Job status is a toast, not a console.** A fixed bottom-right toast with an indeterminate sliding progress bar — not a slide-out panel and not a console scrollback. The toast:
- Title is a friendly label ("BPM lookup", "Discogs sync") derived from `_job_title(job)` — never the raw `job.kind`.
- Body grows to fit; never `line-clamp`. Long messages just push the bottom down.
- Body reads like product copy. Filter command-echo lines (`$ /…/python -m sleeve_notes.cli …`), per-source progress tallies (`progress: songbpm=3 …`), and final source summaries (`new this run by source: …`). Rewrite technical sentences like "Cache populated: 421/445 BPMs (…)" to "Found BPM for 421 of 445 tracks (…)". The filter/rewrite logic lives in both `routes/run.py` (`_filter_line`, `_prettify_line`) and `static/app.js` (`prettifyLine`) — keep them in sync.

**After any terminal status, refresh in place.** On `done` / `cancelled` / `failed`, `app.js` issues `htmx.ajax(GET, current_url, {target:"main", select:"main > *", swap:"innerHTML"})`. Cancelled and failed jobs can still have written partial DB state (some releases imported, some BPMs cached) so they're treated the same as success. Never `window.location.reload()` — it kills toast, sidebar focus, and scroll position.

**Static-asset caching.** Static files are served via `NoCacheStaticFiles` (`Cache-Control: no-store`). Browsers heuristically cache JS aggressively, and stale `app.js` after a refactor produces baffling UI bugs (the new template renders correctly because Jinja re-reads every request, but the cached JS targets DOM nodes that no longer exist). The `app.js?v=N` query param in `base.html` is a belt-and-suspenders cache-buster — bump it whenever app.js changes substantively.

**Engine reuse, not duplication.** Web routes that need data the CLI also computes — sticker layout, BPM lookup, render — call the same modules in `sleeve_notes/`. Example: the sticker preview in the collection-detail drawer uses the same `render_release_stickers_svg` as `/preview`, ensuring printed PDF and on-screen preview stay in lockstep. Never fork engine logic into web-only helpers.

**Visual style.**
- No greyed-out text in actionable buttons — it reads as disabled. `.btn-quiet` uses `--ink` for text; reserve `--dim` for decorative glyphs inside buttons or genuinely inactive states.
- No italics on song or release titles.
- No abbreviated column headers, except `BPM` (universally abbreviated). `Position` over `Pos`, `Format` over `Fmt`, `Duration` over `Dur`.
- Form controls placed on the same row share an explicit height (`h-8` for `.field` inputs/selects/buttons). Native `<select>` adds invisible vertical chrome that doesn't match `<input>` even with identical Tailwind padding classes.
- Don't show redundant information in detail panels (e.g. an explicit ID immediately above a link that already encodes the same ID).

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.