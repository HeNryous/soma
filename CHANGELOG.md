# Changelog

All notable changes to Soma are documented here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/). Versions follow SemVer
under a leading `0.` until the architecture is stable enough for `1.0`.

## Versioning policy

- **MAJOR** (`X` in `0.X.0`) — architecture break (different tool-call
  mechanism, multi-user, container model change). Pushed + tagged.
- **MINOR** (`Y` in `0.X.Y`) — new module / new behavior / new memory
  category / new background phase. Pushed + tagged.
- **PATCH** (third number) — bug fix, refactor without behavior change,
  test additions, doc tweaks, small prompt adjustments. Accumulated
  locally; pushed in batches alongside the next MINOR or when 3-5 PATCH
  commits make a meaningful "maintenance release".

## [Unreleased]

_Nothing yet._

## [0.2.0] — 2026-05-12

One-shot installer pipeline. A fresh clone can now reach a runnable bot
with `./install.sh && $EDITOR .env && ./start_soma.sh`.

### Added

- **`install.sh`** — idempotent bootstrap script that:
  1. Verifies `python3 >= 3.10`, `docker` daemon, `pip`
  2. Creates `.venv/` next to the repo and installs the three host deps
     (works around PEP 668 / externally-managed-environment cleanly)
  3. Creates the `data/` tree (`sandbox-home/`, `sandbox/workspace/`,
     `sandbox/inbox/`)
  4. Pulls `python:3.12-slim` and creates the `soma-sandbox` container
     with proper bind-mounts
  5. Copies `.env.example` → `.env` on first run
  6. Runs all eight test suites and reports pass/fail
- **`requirements.txt`** — pinned host deps (`aiogram >= 3.0`,
  `httpx >= 0.27`, `pyyaml >= 6.0`).
- **`.venv/` integration** — `start_soma.sh` and `soma.service` now
  prefer `.venv/bin/python3` when present.

### Changed

- `.env.example`, `start_soma.sh`, `soma.service` comments translated
  to English (leftover German from `0.1.0`).
- `README.md` Quick start rewritten around the installer; manual install
  kept as a fallback section.
- `.gitignore` now ignores `.venv/`.

## [0.1.0] — 2026-05-12

First public release. Soma is a lightweight, model-agnostic agent harness
that talks via Telegram, executes code in a Docker sandbox, and learns
through interaction. ~4100 LOC across nine modules, three external
dependencies (`aiogram`, `httpx`, `pyyaml`).

### Added — core loop

- **Programmatic Tool Calling (PTC)**: model writes Markdown
  ` ```shell ` / ` ```python ` blocks instead of OpenAI tool-call
  JSON. Harness extracts via regex, executes in container, returns
  stdout/stderr as `role="user"` message. Model-agnostic, no
  modell-specific parser.
- **Iteration budget** with auto-summary on exhaustion
  (`MAX_ITERATIONS=100`, `BUDGET_SUMMARY_PROMPT`).
- **Head-Middle-Tail context compression** (`compress_middle`) triggers
  at >40 messages or >200K chars; cooldown 10 iterations.

### Added — memory

- **Three-type memory** (`semantic` / `procedural` / `episodic`)
  persisted as JSONL. Model writes via shell-echo, code reads.
- **Style routing**: memories tagged `preference` / `style` / `tone` /
  `behavior` render as a top-priority `Behavior Rules`-block;
  behavior-reminder also re-injected directly before the user message.
- **Immortal vs. mortal tags**: `preference, style, tone, behavior,
  domain, role, identity` survive every prune (`score = +inf`).
  Everything else uses `recency × frequency × type_weight` decay.
- **Forgetting**: auto-prune at >200 memories down to 100 + all
  immortals.
- **Fusion**: near-duplicate memories merge (token-overlap ≥ 0.85,
  same type, matching style-status).
- **Conflict detection**: tag-overlap ≥ 2 + same type logs
  `memory_conflict_detected`, system-reminder injected next turn.
- **Curator-driven context selection**: when ≥15 memories exist,
  background curator picks 5-8 IDs per turn → written to
  `context_selection.json`. Foreground renders three blocks:
  `Behavior Rules`, `Relevant context`, `Further memories`.

### Added — background

- **Async curator task** next to Telegram polling. Event-driven
  (queue-fed after each foreground turn), idle-tick fallback after 90s.
- **Knowledge curation**: writes memories the foreground forgot.
- **Error-lesson extraction** from failed `code_executed` events.
- **Self-monitoring** (every 3rd idle-tick): tool success rate,
  correction count, prompt volume → updates inner states.
- **Inner states** (`curiosity` / `boredom` / `confidence`) in
  `state.json`. Model updates them; harness reads them.
- **Sleep consolidation** (every 10th idle-tick): NREM fuse+prune,
  REM synthesis from 5 random memories.
- **Curiosity-driven research** when `curiosity > 0.65`: model picks
  one knowledge-hole and fetches one targeted page via `requests` +
  `bs4`.
- **Bored-browse** when `boredom > 0.6` AND `curiosity > 0.5`: free
  browsing using recent memories as starting points. Rate-limited to
  3 requests per idle-tick, 10 per hour. Relevant findings get tag
  `discovered`; irrelevant results are forgotten.
- **Proactive Telegram notifications** via `notify_user.txt`
  (rate-limited 3 per 24h).
- **Reflection** after productive idle-tick actions.

### Added — closed-loop correction

- `detect_correction` keyword heuristic on user message.
- `build_broken_promise_note` — when last bot response said
  "gemerkt"/"notiert"/"gespeichert" but `blocks_executed == 0`, next
  turn deterministically auto-captures the prior user message.
- `memory_promise_unfulfilled` event tracks the lie pattern.

### Added — skill crystallization

- `crystallize.py` reads `events.jsonl` and extracts recurring
  `lang:firsttoken` patterns. After 3+ uses, writes a procedural
  memory with concrete example. Idempotent via tag-set.
- Auto-triggered every 20 successful `code_executed` events.

### Added — Telegram interface

- aiogram 3.x bot, single-user (`OWNER_CHAT_ID`).
- **Message debouncing**: 3-second buffer collapses fast follow-ups
  into one `core.run`.
- **Serialization lock**: only one `core.run` in flight at a time
  (FIFO via `asyncio.Lock`).
- **Direct address**: prompt rule forbids 3rd-person speech about the
  user even when memories phrase them in 3rd person.
- **File handling**: every uploaded file is processed by the
  foreground immediately. Batch mode: multiple files in the 3s window
  run sequentially under `core_lock`; ONE summary message at the
  end ("N files processed: …"), substantial facts persist as
  memory with tag `from-file`.
- **Binary-format hint** in the prompt: extract in-container with
  `pypdf` / `extract_msg` / `openpyxl`, never load entire file into
  the LLM context.

### Added — sandbox

- Docker container `soma-sandbox` (python:3.12-slim, uid 1000:1000,
  1G RAM, 2 CPU, `--cap-drop=ALL`, `--security-opt=no-new-privileges`).
- Bind-mounts: `data/workspace` → `/workspace`,
  `data/sandbox-home` → `/home/sandbox` (pip `--user` installs persist).
- Network: `--network=bridge` for public-internet access (pip + web
  research) but iptables `DOCKER-USER` rule blocks the configurable
  private subnet (`<YOUR_PRIVATE_CIDR>` by default) so inference cluster and
  internal services are unreachable from the sandbox.

### Added — operational

- `soma.service` systemd unit; logs to journald
  (`journalctl -u soma -f`).
- `.env` symlink convention (canonical token file outside the repo).
- `--version` flag on `core.py`.
- `status.py` CLI dashboard.

### Tests

- ~55 unit tests across eight `test_*.py` files. All green at release.
