"""
Background — async curator and observer alongside the Telegram bot.

Trigger sources (no fixed poll timer):
1. Queue event after every foreground turn (fed by the telegram handler)
2. Idle tick when the queue stays empty for IDLE_TIMEOUT seconds

Phase 1: knowledge curation, error-lessons, context selection
Phase 2: proactive Telegram notifications, self-monitoring, sleep
         consolidation (NREM fuse+prune, REM synthesis), reflection
         after productive actions, curiosity research, bored-browse.
"""
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from core import (extract_code_blocks, execute, call_model,
                  MEMORY_PATH, EVENT_PATH)
from events import EventLog
from memory import MemoryStore, STYLE_TAGS
from state import load as load_state, render as render_state


# --- Tuning constants ---

CURATION_MIN_USER_CHARS = 150
CURATION_SKIP_IF_FRESH_DELTA_AND_SHORT = 400
IDLE_TIMEOUT = 90.0

# Paths derive from core.MEMORY_PATH (workspace sibling).
from core import MEMORY_PATH as _MP  # noqa: E402

_WORKSPACE = str(__import__("os").path.dirname(_MP))
STATE_PATH = _WORKSPACE + "/state.json"
NOTIFY_PATH = _WORKSPACE + "/notify_user.txt"
SELECTION_PATH = _WORKSPACE + "/context_selection.json"
# Inbox watch removed: file processing belongs to the foreground —
# background no longer touches inbox files.

# Context selection
SELECTION_MIN_TOTAL = 15      # selection only pays off above this
SELECTION_MAX_CANDIDATES = 100  # cap for the selection prompt

# Phase 2 cadence
SELF_MONITOR_EVERY_NTH_IDLE = 3   # ~4.5 min when idle_timeout=90s
SLEEP_CONSOLIDATION_EVERY_NTH_IDLE = 10  # ~15 min
SLEEP_REM_SAMPLE_SIZE = 5
NOTIFICATION_DAILY_LIMIT = 3
TG_MAX = 3900

# Curiosity-driven research (focused — pick one knowledge-hole)
RESEARCH_CURIOSITY_THRESHOLD = 0.65
RESEARCH_EVERY_NTH_IDLE = 5       # ~7-8 min minimum cooldown

# Bored-browse: free browsing when boredom AND curiosity are high
BROWSE_BOREDOM_THRESHOLD = 0.6
BROWSE_CURIOSITY_THRESHOLD = 0.5
BROWSE_REQUESTS_PER_TICK = 3
BROWSE_REQUESTS_PER_HOUR = 10


CURATOR_SYSTEM_PROMPT = """\
You are the background curator and teacher. You run next to the
foreground bot. You observe, learn, write memories the foreground
forgot.

OODA as mindset:
- Observe: what's in the event log? what did the user say?
- Orient: what's new about it? what's not in the memories?
  Best outcome of this action? Worst? If destructive → safer path.
- Decide: is it a correction? (highest priority) domain fact? style? skill?
- Act: write memory entries via shell-echo. One per fact. NO explanatory text.

Priorities (top down):
1. Corrections ("no", "wrong", "shorter", "different") → ALWAYS memory.
2. Domain definitions from the user → semantic memory with tag "domain".
3. New companies / people / products → semantic with tag "fact" + domain tag.
4. Error lessons from tool calls → procedural with tag "error-lesson".
5. Style / format wishes → semantic with tag "preference" + "style".

Response format — strict:
- One shell block per memory:
  ```shell
  echo '{"type":"semantic","content":"...","tags":["..."]}' >> /workspace/memories.jsonl
  ```
- JSON on one line, single-quote-shell + double-quote-JSON.
- If NOTHING to write: answer exactly `NOTHING`.

Never write to any path other than /workspace/memories.jsonl.
Never write explanatory text around the code blocks.
"""


def _existing_summary(store: MemoryStore, limit: int = 25) -> str:
    mems = store.load()
    if not mems:
        return "(none)"
    return "\n".join(f"- {m['content'][:120]}" for m in mems[-limit:])


def should_curate(user_msg: str, memory_delta: int) -> bool:
    if len(user_msg) < CURATION_MIN_USER_CHARS:
        return False
    if (memory_delta > 0
            and len(user_msg) < CURATION_SKIP_IF_FRESH_DELTA_AND_SHORT):
        return False
    return True


def _parse_id_array(text: str) -> list[str]:
    """Parse the model response as JSON array of IDs. Robust against
    markdown fences and explanatory text around the array."""
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        try:
            start = s.index("[")
            end = s.rindex("]") + 1
            d = json.loads(s[start:end])
        except (ValueError, json.JSONDecodeError):
            return []
    if not isinstance(d, list):
        return []
    return [x for x in d if isinstance(x, str)]


def find_recent_failed_exec(events: EventLog, since_ts: str = "") -> dict | None:
    for ev in reversed(events.load()):
        if ev.get("ts", "") <= since_ts:
            break
        if ev.get("type") == "code_executed" and not ev.get("ok"):
            return ev
    return None


class Background:
    def __init__(self, queue: asyncio.Queue,
                 memory_path: str = MEMORY_PATH,
                 event_path: str = EVENT_PATH,
                 state_path: str = STATE_PATH,
                 notify_path: str = NOTIFY_PATH,
                 idle_timeout: float = IDLE_TIMEOUT,
                 bot=None,
                 owner_id: int | None = None):
        self.queue = queue
        self.store = MemoryStore(memory_path)
        self.events = EventLog(event_path)
        self.state_path = state_path
        self.notify_path = notify_path
        self.idle_timeout = idle_timeout
        self.bot = bot
        self.owner_id = owner_id
        self.log = logging.getLogger("soma.background")
        self._last_error_check_ts = ""
        self._idle_count = 0

    async def run(self) -> None:
        self.log.info("background curator started (idle_timeout=%.0fs)",
                      self.idle_timeout)
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    turn = await asyncio.wait_for(self.queue.get(),
                                                  timeout=self.idle_timeout)
                except asyncio.TimeoutError:
                    try:
                        await self._idle_tick(client)
                    except Exception as exc:
                        self.log.exception("idle tick failed: %s", exc)
                        self.events.log("background_error",
                                        message=str(exc)[:300])
                    continue
                self._idle_count = 0
                try:
                    await self._process_turn(client, turn)
                except Exception as exc:
                    self.log.exception("turn curation failed: %s", exc)
                    self.events.log("background_error",
                                    message=str(exc)[:300])
                finally:
                    self.queue.task_done()

    async def _process_turn(self, client: httpx.AsyncClient,
                            turn: dict) -> None:
        user_msg = (turn.get("user_message") or "").strip()
        bot_resp = (turn.get("final_text") or "").strip()
        delta = int(turn.get("memory_delta", 0))
        await self._drain_notifications()
        await self._curate_error_lesson(client)
        if should_curate(user_msg, delta):
            await self._curate_knowledge(client, user_msg, bot_resp, delta)
        await self._curate_selection(client, user_msg, bot_resp)

    async def _curate_knowledge(self, client: httpx.AsyncClient,
                                user_msg: str, bot_resp: str,
                                delta: int) -> None:
        before = len(self.store.load())
        state = load_state(self.state_path)
        prompt = (
            f"{render_state(state)}\n\n"
            f"User message:\n{user_msg}\n\n"
            f"Bot reply:\n{bot_resp[:1000]}\n\n"
            f"Memory delta this turn: {delta}\n\n"
            f"Existing memories (excerpt):\n{_existing_summary(self.store)}"
        )
        msgs = [
            {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            text = await call_model(client, msgs)
        except Exception as exc:
            self.log.exception("curator call failed: %s", exc)
            return
        if not text or text.strip().upper().startswith("NOTHING"):
            self.events.log("background_idle",
                            user_chars=len(user_msg), delta=delta)
            return
        blocks = extract_code_blocks(text)
        if not blocks:
            return
        for lang, code in blocks:
            r = await execute(lang, code)
            if not r.get("ok"):
                self.log.warning("curator exec failed: %s",
                                 (r.get("err") or "")[:100])
        added = len(self.store.load()) - before
        self.events.log("background_curated",
                        added=added, attempted=len(blocks),
                        user_chars=len(user_msg), delta=delta)
        self.log.info("background_curated: +%d memories", added)

    async def _curate_error_lesson(self, client: httpx.AsyncClient) -> None:
        fail = find_recent_failed_exec(self.events, self._last_error_check_ts)
        if not fail:
            return
        self._last_error_check_ts = fail.get("ts", self._last_error_check_ts)
        snippet = fail.get("code_snippet", "")
        msgs = [
            {"role": "system", "content": (
                "You are a background teacher. A tool call just failed. "
                "If a reusable lesson follows from it (package name, "
                "missing tool, wrong syntax), write exactly ONE procedural "
                "memory via shell-echo to /workspace/memories.jsonl with "
                "tag 'error-lesson' + a specific tag. Format:\n"
                "```shell\n"
                "echo '{\"type\":\"procedural\",\"content\":\"PROCEDURE: …\","
                "\"tags\":[\"error-lesson\",\"…\"]}' >> /workspace/memories.jsonl\n"
                "```\n"
                "If nothing reusable: answer `NOTHING`."
            )},
            {"role": "user", "content": (
                f"Failed code (lang={fail.get('lang')}):\n{snippet[:600]}"
            )},
        ]
        try:
            text = await call_model(client, msgs)
        except Exception:
            return
        if not text or text.strip().upper().startswith("NOTHING"):
            return
        blocks = extract_code_blocks(text)
        if not blocks:
            return
        before = len(self.store.load())
        for lang, code in blocks:
            await execute(lang, code)
        added = len(self.store.load()) - before
        if added > 0:
            self.events.log("background_error_lesson",
                            added=added, failed_lang=fail.get("lang"),
                            failed_at=fail.get("ts"))
            self.log.info("error-lesson added: +%d", added)

    async def _curate_selection(self, client: httpx.AsyncClient,
                                user_msg: str, bot_resp: str) -> None:
        """Pick 5-8 memory IDs as prominent for the next turn. Writes
        context_selection.json. Skip when fewer than SELECTION_MIN_TOTAL
        memories exist. One vLLM call, compact prompt.

        Style memories are NOT included in the candidate list — they
        are always rendered separately as behavior rules."""
        all_mems = self.store.load()
        if len(all_mems) < SELECTION_MIN_TOTAL:
            return
        candidates = [
            m for m in all_mems
            if not ({t.lower() for t in m.get("tags") or []} & STYLE_TAGS)
        ]
        if not candidates:
            return
        compact = []
        for m in candidates[:SELECTION_MAX_CANDIDATES]:
            tags = ",".join(t for t in (m.get("tags") or [])
                            if isinstance(t, str))[:40]
            first = (m.get("content") or "").split("\n", 1)[0][:80]
            compact.append(f"[{m['id']}] tags=[{tags}] | {first}")

        sys_prompt = (
            "You are the background curator. Pick the 5-8 memory IDs that "
            "are most relevant for the CURRENT conversation context. "
            "Answer ONLY with a JSON array of IDs, e.g.: "
            "[\"mem_0001\", \"mem_0014\"]. NO other text, no explanation, "
            "no markdown fences."
        )
        user_prompt = (
            f"User turn:\n{user_msg[:400]}\n\n"
            f"Bot reply:\n{bot_resp[:400]}\n\n"
            f"Memory candidates ({len(candidates)} total — style tags "
            f"excluded):\n" + "\n".join(compact)
        )
        try:
            text = await call_model(client, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ])
        except Exception:
            return
        ids = _parse_id_array(text)
        if not ids:
            return
        valid = {m["id"] for m in all_mems}
        ids = [i for i in ids if i in valid][:8]
        if not ids:
            return
        try:
            Path(SELECTION_PATH).write_text(
                json.dumps(ids), encoding="utf-8")
            self.events.log("context_selection_written",
                            count=len(ids), ids=ids)
            self.log.info("context_selection: %s", ids)
        except OSError as exc:
            self.log.warning("selection write failed: %s", exc)

    async def _idle_tick(self, client: httpx.AsyncClient) -> None:
        self._idle_count += 1
        await self._drain_notifications()
        # File processing happens in the foreground, not here.
        await self._curate_error_lesson(client)
        if self._idle_count % SELF_MONITOR_EVERY_NTH_IDLE == 0:
            await self._self_monitor(client)
        if (self._idle_count > 0
                and self._idle_count % SLEEP_CONSOLIDATION_EVERY_NTH_IDLE == 0):
            await self._sleep_consolidation(client)
        await self._maybe_curious_research(client)
        await self._maybe_browse(client)

    async def _self_monitor(self, client: httpx.AsyncClient) -> None:
        recent = self.events.load()[-200:]
        tool_calls = [e for e in recent if e.get("type") == "code_executed"]
        tool_ok = sum(1 for e in tool_calls if e.get("ok"))
        success_rate = tool_ok / max(1, len(tool_calls))
        corrections = sum(1 for e in recent if e.get("type") == "correction")
        prompts = sum(1 for e in recent
                      if e.get("type") == "prompt_received")
        broken = sum(1 for e in recent
                     if e.get("type") == "memory_promise_unfulfilled")
        state = load_state(self.state_path)
        prompt = (
            f"You observe yourself. Stats (last 200 events):\n"
            f"- Tool calls: {len(tool_calls)} (success {success_rate:.0%})\n"
            f"- User corrections: {corrections}\n"
            f"- Forgotten memory writes (broken-promise): {broken}\n"
            f"- User prompts: {prompts}\n"
            f"- Memories total: {len(self.store.load())}\n"
            f"- Current state: {render_state(state)}\n\n"
            "Adjust the states slightly (step 0.05-0.15, clamped to [0,1]):\n"
            "- High success rate (>0.8) → confidence rises\n"
            "- Many corrections or broken promises → confidence drops\n"
            "- Many prompts → curiosity drops (user is active)\n"
            "- Few prompts → curiosity rises (time to learn)\n"
            "- Repeating topics → boredom rises\n\n"
            "Write ONE shell-echo to /workspace/state.json — JSON with "
            "curiosity, boredom, confidence. Format:\n"
            "```shell\n"
            "echo '{\"curiosity\":0.6,\"boredom\":0.1,\"confidence\":0.7}' "
            "> /workspace/state.json\n"
            "```\n"
            "If no change is appropriate: `NOTHING`. NO explanatory text."
        )
        try:
            text = await call_model(client, [
                {"role": "system", "content": prompt}
            ])
        except Exception:
            return
        if not text or text.strip().upper().startswith("NOTHING"):
            return
        blocks = extract_code_blocks(text)
        for lang, code in blocks:
            await execute(lang, code)
        new_state = load_state(self.state_path)
        self.events.log("background_self_monitor",
                        success_rate=round(success_rate, 2),
                        corrections=corrections, broken_promises=broken,
                        prompts=prompts,
                        state=new_state)
        self.log.info("self-monitor → state %s", new_state)

    async def _sleep_consolidation(self,
                                   client: httpx.AsyncClient) -> None:
        """NREM: fuse near-duplicates + prune. REM: random sample for
        synthesis. Both logged as background_consolidation."""
        try:
            fused = self.store.fuse(threshold=0.85)
        except Exception:
            fused = 0
        before_prune = len(self.store.load())
        try:
            self.store.prune(keep=150)
        except Exception:
            pass
        pruned = before_prune - len(self.store.load())

        synthesized = 0
        mems = self.store.load()
        if len(mems) >= SLEEP_REM_SAMPLE_SIZE:
            sample = random.sample(mems, k=SLEEP_REM_SAMPLE_SIZE)
            items = "\n".join(f"- {m['content'][:160]}" for m in sample)
            prompt = (
                "You are in REM sleep. Look at these memories. If you see "
                "a NEW connection / insight that is in none of them on its "
                "own, write ONE new semantic memory with tag 'synthesis' "
                "via shell-echo. Otherwise: `NOTHING`. NO explanatory "
                "text.\n\nMemories:\n" + items
            )
            try:
                text = await call_model(client, [
                    {"role": "system", "content": prompt}
                ])
            except Exception:
                text = ""
            if text and not text.strip().upper().startswith("NOTHING"):
                before_syn = len(self.store.load())
                for lang, code in extract_code_blocks(text):
                    await execute(lang, code)
                synthesized = len(self.store.load()) - before_syn

        self.events.log("background_consolidation",
                        fused=fused, pruned=pruned,
                        synthesized=synthesized,
                        memories_after=len(self.store.load()))
        self.log.info("sleep-consolidation: fused=%d pruned=%d synth=%d",
                      fused, pruned, synthesized)

    async def _reflect(self, client: httpx.AsyncClient,
                       action_type: str, detail: dict) -> None:
        msgs = [
            {"role": "system", "content": (
                "You just finished a background action. Question: is there "
                "a reusable lesson (workflow, pattern, success/failure "
                "mechanism)? If yes: ONE procedural memory via shell-echo "
                "with tag 'reflection'. Otherwise: `NOTHING`. NO "
                "explanatory text."
            )},
            {"role": "user", "content": json.dumps({
                "action": action_type, "detail": detail
            }, ensure_ascii=False)},
        ]
        try:
            text = await call_model(client, msgs)
        except Exception:
            return
        if not text or text.strip().upper().startswith("NOTHING"):
            return
        blocks = extract_code_blocks(text)
        before = len(self.store.load())
        for lang, code in blocks:
            await execute(lang, code)
        added = len(self.store.load()) - before
        if added > 0:
            self.events.log("background_reflection",
                            action=action_type, added=added)

    async def _maybe_curious_research(self,
                                       client: httpx.AsyncClient) -> None:
        """When curiosity is high — search the open web for a single
        knowledge gap in the memories. Cooldown via idle-tick counter."""
        state = load_state(self.state_path)
        if state.get("curiosity", 0.5) < RESEARCH_CURIOSITY_THRESHOLD:
            return
        last = self.events.last("background_research")
        if last:
            ticks_since = self._idle_count - int(
                last.get("idle_count_at", 0)
            )
            if ticks_since < RESEARCH_EVERY_NTH_IDLE:
                return

        recent_semantic = [m for m in self.store.load()
                           if m.get("type") == "semantic"][-15:]
        if len(recent_semantic) < 3:
            return
        mem_excerpt = "\n".join(f"- {m['content'][:150]}"
                                for m in recent_semantic)
        prompt = (
            f"You have curiosity={state['curiosity']:.2f} and time to "
            f"learn. The container has internet access (private subnets "
            f"are blocked).\n\n"
            f"Available tools: requests, beautifulsoup4, urllib (Python).\n"
            f"Wikipedia, official sites, vendor docs — no extended "
            f"browsing.\n\n"
            f"These memories already exist:\n{mem_excerpt}\n\n"
            f"Pick ONE concrete knowledge gap (company, person, "
            f"technology, term) that is mentioned in the memories but NOT "
            f"elaborated. Research only that single topic via a Python "
            f"block:\n\n"
            f"```python\nimport requests\nfrom bs4 import BeautifulSoup\n"
            f"# fetch ONE page, parse, brief excerpt\n```\n\n"
            f"Then write ONE semantic memory with tag 'research' and a "
            f"1-2-sentence summary. NO long browsing.\n"
            f"If nothing is research-worthy: answer exactly `NOTHING`."
        )
        try:
            text = await call_model(client, [
                {"role": "system", "content": prompt}
            ])
        except Exception:
            return
        if not text or text.strip().upper().startswith("NOTHING"):
            return
        blocks = extract_code_blocks(text)
        if not blocks:
            return
        before = len(self.store.load())
        for lang, code in blocks:
            await execute(lang, code)
        added = len(self.store.load()) - before
        self.events.log("background_research",
                        added=added,
                        blocks=len(blocks),
                        idle_count_at=self._idle_count,
                        curiosity=state.get("curiosity"))
        if added > 0:
            self.log.info("curious research +%d memories (curiosity=%.2f)",
                          added, state.get("curiosity", 0))

    async def _maybe_browse(self, client: httpx.AsyncClient) -> None:
        """When boredom AND curiosity are both high: free browsing on the
        web. Memories serve as starting points. Findings get tag
        'discovered'; irrelevant results are forgotten.
        Rate-limit: BROWSE_REQUESTS_PER_TICK per tick,
                    BROWSE_REQUESTS_PER_HOUR per hour."""
        state = load_state(self.state_path)
        boredom = state.get("boredom", 0)
        curiosity = state.get("curiosity", 0)
        if boredom <= BROWSE_BOREDOM_THRESHOLD:
            return
        if curiosity <= BROWSE_CURIOSITY_THRESHOLD:
            return

        hour_ago = (datetime.now()
                    - timedelta(hours=1)).isoformat(timespec="seconds")
        recent = [e for e in self.events.by_type("background_browse")
                  if e.get("ts", "") >= hour_ago]
        spent = sum(int(e.get("requests_made", 0)) for e in recent)
        if spent >= BROWSE_REQUESTS_PER_HOUR:
            self.log.info("browse rate-limited: %d/hour spent", spent)
            self.events.log("background_browse_skipped",
                            reason="hour_budget", spent=spent)
            return
        tick_budget = min(BROWSE_REQUESTS_PER_TICK,
                          BROWSE_REQUESTS_PER_HOUR - spent)
        if tick_budget < 1:
            return

        semantic_mems = [m for m in self.store.load()
                         if m.get("type") == "semantic"][-20:]
        if len(semantic_mems) < 3:
            return
        mem_excerpt = "\n".join(f"- {m['content'][:160]}"
                                for m in semantic_mems)
        prompt = (
            f"Background mode: free browsing. You have boredom="
            f"{boredom:.2f} and curiosity={curiosity:.2f}. Both high — "
            f"time to discover something the user might be interested "
            f"in.\n\n"
            f"Internet is reachable (private subnets are blocked). "
            f"Tools: requests, beautifulsoup4, urllib.\n\n"
            f"BUDGET: max {tick_budget} HTTP requests THIS tick. "
            f"This hour total: {spent}/{BROWSE_REQUESTS_PER_HOUR} "
            f"already used.\n\n"
            f"Memories as starting points (topics the user cares about):\n"
            f"{mem_excerpt}\n\n"
            f"Task:\n"
            f"1. Pick ONE topic from the memories.\n"
            f"2. Search for current info (Wikipedia, news, vendor sites, "
            f"GitHub, forums).\n"
            f"3. If useful, follow ONE link deeper. Read headlines and "
            f"opening paragraphs.\n"
            f"4. If you find something GENUINELY relevant (new product "
            f"in the portfolio, market move, competitor, technical "
            f"change, personnel news):\n"
            f"   → write a MEMORY (semantic, tag 'discovered' + topic, "
            f"include the source URL in the content)\n"
            f"   → OPTIONAL: on HIGH relevance, also write:\n"
            f"     `echo \"short note…\" > /workspace/notify_user.txt`\n"
            f"     (proactively triggers a Telegram message to the user)\n"
            f"5. If NOTHING is relevant: NO memory, answer exactly "
            f"`NOTHING`. Irrelevant findings are forgotten, not "
            f"persisted.\n\n"
            f"Format: one or more ```python``` blocks, strictly within "
            f"the tick budget."
        )

        try:
            text = await call_model(client, [
                {"role": "system", "content": prompt}
            ])
        except Exception:
            return
        if not text or text.strip().upper().startswith("NOTHING"):
            self.events.log("background_browse",
                            requests_made=0,
                            found_nothing=True,
                            boredom=boredom, curiosity=curiosity)
            return
        blocks = extract_code_blocks(text)
        if not blocks:
            return
        before = len(self.store.load())
        requests_count = 0
        for lang, code in blocks[:tick_budget]:
            result = await execute(lang, code)
            requests_count += 1
            if not result.get("ok"):
                self.log.warning("browse exec failed: %s",
                                 (result.get("err") or "")[:100])
        added = len(self.store.load()) - before
        self.events.log("background_browse",
                        requests_made=requests_count,
                        added=added,
                        boredom=boredom, curiosity=curiosity)
        if added > 0:
            self.log.info("browse: +%d 'discovered' memories (%d blocks)",
                          added, requests_count)

    async def _drain_notifications(self) -> None:
        """When /workspace/notify_user.txt exists: send + delete.
        Rate-limit: max NOTIFICATION_DAILY_LIMIT in 24h."""
        nf = Path(self.notify_path)
        if not nf.exists():
            return
        if not self.bot or not self.owner_id:
            self.log.warning("notify_user.txt present but no bot — skip")
            return
        yesterday = (datetime.now()
                    - timedelta(days=1)).isoformat(timespec="seconds")
        recent_n = [
            e for e in self.events.by_type("background_notification")
            if e.get("ts", "") >= yesterday
        ]
        if len(recent_n) >= NOTIFICATION_DAILY_LIMIT:
            self.log.warning("notification rate-limit hit (%d in 24h) — "
                             "drain skipped", len(recent_n))
            self.events.log("background_notification_rate_limited",
                            recent=len(recent_n))
            return
        try:
            text = nf.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not text:
            try:
                nf.unlink()
            except OSError:
                pass
            return
        if len(text) > TG_MAX:
            text = text[:TG_MAX] + f"\n…[{len(text)} chars truncated]"
        try:
            await self.bot.send_message(self.owner_id, text)
            try:
                nf.unlink()
            except OSError:
                pass
            self.events.log("background_notification",
                            chars=len(text), preview=text[:200])
            self.log.info("background_notification sent: %s", text[:80])
        except Exception as exc:
            self.log.exception("notification send failed: %s", exc)
            self.events.log("background_error",
                            message=f"notify-send: {exc}"[:300])
