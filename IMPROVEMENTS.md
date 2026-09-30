# LLMSwarm Improvement Proposals

Last updated: 2026-09-27

## Status Legend
- [x] Implemented
- [ ] Not yet implemented
- [~] Partially implemented

---

## High Priority

### 1. Real Streaming Support
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: Current SSE stream sends everything at once at the end. Clients with first-byte timeouts (like pi) may see connection errors on slow ensembles (~40-50s).
- **Fix**: Added `_send_sse_keepalive()` method that sends SSE headers immediately and starts a background thread to send keep-alive chunks every 15 seconds while waiting for members.
- **Details**: 
  - Initial empty chunk sent immediately to establish the stream
  - Keep-alive chunks sent every 15s with `finish_reason: None`
  - Final content chunk sent when ensemble completes
  - `[DONE]` marker sent at the end
  - Prevents client timeouts on slow ensembles

### 2. Swarm Mode Conversation Context
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: `run_swarm` only uses the last user message as the "problem" — it ignores conversation history.
- **Fix**: Added optional `history` parameter to `run_swarm()`. Now receives last 10 messages as context for the planner.
- **Details**:
  - `run_swarm(fleet, bb, problem, member_names, roles, history=None)` 
  - History text built from `messages[-10:]`
  - Passed to planner prompt: "Conversation history:\n{history_text}"
  - `do_POST` handler passes full `messages` list as history
  - CLI usage still works (history defaults to None)

### 3. Member Retry Logic
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: Transient network errors or slow model loading cause members to be silently excluded.
- **Fix**: Added `chat_with_retry()` with exponential backoff (1s, 2s), up to 3 total attempts.
- **Details**: Retries on URLError, socket.timeout, ConnectionError, HTTP 500/502/503/504. Does NOT retry on HTTP 400/401/403/404.

### 4. Error Surfacing
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: When a member fails, the client gets a partial answer without knowing which members participated or failed.
- **Fix**: All run functions now return `(text, member_status)` tuples. HTTP response includes `member_status` field.
- **Example**:
  ```json
  {
    "member_status": {
      "participated": ["rocm1", "rocm0", "salience"],
      "failed": []
    }
  }
  ```

---

## Medium Priority

### 5. Fast Path for Simple Queries
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: For short/simple queries, ensemble overhead (3× compute + judge merge) is wasteful.
- **Fix**: Added heuristic in `do_POST`: if query < 50 words AND > 1 active member, route to single member (active[0]) using `run_solo()`.
- **Details**:
  - `word_count = len(problem.split())`
  - `use_fast_path = word_count < 50 and len(active) > 1`
  - Fast path returns in ~1s vs ~40-70s for full ensemble
  - Member status shows only the single fast-path member

### 6. Blackboard Recall Quality
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: FTS5 word matching was crude — no relevance weighting, just recency (id DESC).
- **Fix**: Added BM25 relevance scoring + recency weighting to `Blackboard.recall()`.
- **Details**:
  - Uses `bm25(entries_fts)` for text relevance (inverted: higher = more relevant)
  - Recency bonus: last hour = 1.0, last day = 0.5, older = 0.1
  - Combined score: `(-relevance * 0.7 + recency * 0.3) DESC`
  - Results now ranked by relevance + freshness, not just newest-first

### 7. Request Timeout Configuration
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: Pi's client timeout may be shorter than ensemble latency.
- **Fix**: Added `request_timeout` config option (seconds). When set > 0, runs ensemble in a thread and returns HTTP 504 if timeout exceeded.
- **Details**:
  - `request_timeout = float(scfg.get("request_timeout", 0) or 0)`
  - When > 0: uses threading with `t.join(timeout=request_timeout)`
  - Returns 504: `{"error": "request timed out after Xs"}`
  - When 0: runs synchronously (no timeout)
  - Configurable via WebUI or swarm.toml under `[serve]`

### 8. Judge Fallback
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: If the configured judge is disabled, it still serves as judge.
- **Fix**: Added check: if judge not in active members, fall back to `active[-1]`.

---

## Low Priority

### 9. Code Modularization
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: Single 1000+ line file mixed HTTP server, fleet management, swarm logic, UI.
- **Fix**: Extracted to separate modules:
  - `blackboard.py` — Blackboard class (SQLite + FTS5)
  - `fleet.py` — Member, Fleet classes (process management)
  - `swarm.py` — Main entry point, HTTP server, swarm logic, UI
- **Note**: Full modularization (swarm_logic.py, server.py, ui.py) deferred — current split covers the most complex components

### 10. Structured Logging
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: Currently minimal logging. Hard to debug production issues.
- **Fix**: Added:
  - Request IDs (UUID[:8]) for each chat request
  - Per-member latency tracking in `chat()` function
  - Structured log lines: `[req:XXXX] member NAME: Y.Ys OK/FAIL`
  - Request start/end logging with timing

### 11. Metrics Endpoint
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: No visibility into per-member performance.
- **Fix**: Added `GET /api/metrics` endpoint returning:
  - Per-member: total_calls, successful, failed, avg_latency_s, success_rate
  - Total member count
  - Timestamp
- **Example**: `curl http://localhost:5100/api/metrics`

### 12. Graceful Shutdown
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: SIGTERM kills in-flight requests.
- **Fix**: Added signal handlers for SIGTERM and SIGINT:
  - Catches signal, logs "shutting down gracefully"
  - Calls `server.shutdown()` to wait for in-flight requests
  - Closes server socket in finally block

### 13. Test Suite
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: No automated tests.
- **Fix**: Created `tests/test_swarm.py` with 6 test functions:
  - `test_msg_text()` — content extraction (string, list, missing)
  - `test_blackboard()` — put, recall, tail, prune
  - `test_blackboard_prune()` — retention-based pruning
  - `test_validate_config()` — config schema validation
  - `test_parse_subtasks()` — JSON subtask parsing
  - `test_metrics_tracking()` — latency tracking
- **Run**: `python3 tests/test_swarm.py`

### 14. Configuration Validation
- [x] **Status**: Implemented (2026-09-27)
- **Issue**: Typos in `swarm.toml` (invalid roles, missing fields) cause runtime errors.
- **Fix**: Added `validate_config()` function that checks:
  - Duplicate member names
  - Port conflicts
  - Invalid roles (vs VALID_ROLES set)
  - Judge exists in members
  - Valid serve mode
  - Temperature range
  - Context window size
- **Output**: Warnings/errors printed to stderr at startup

---

## Bugs Fixed

### List Content Crash (pi integration)
- [x] **Status**: Fixed (2026-09-27)
- **Issue**: Pi sends message content as a list of blocks, which crashed in 4 places.
- **Fix**: Added `msg_text()` helper that extracts plain text from string, list-of-blocks, or missing content.
- **Crash sites fixed**:
  1. Usage calculation in `do_POST`
  2. Problem extraction in `do_POST`
  3. Conversation construction in `run_ensemble_chat`
  4. System message content concatenation in `run_ensemble_chat`

### Tool-Call Passthrough (pi integration)
- [x] **Status**: Fixed (2026-09-27)
- **Issue**: The swarm's `chat()` function stripped the `tools` parameter from requests. Members never saw tool definitions, so they responded with plain text instead of tool_calls. This caused pi to get stuck in tool-call loops.
- **Fix**: 
  - Added `tools` and `tool_choice` to `PASS_PARAMS`
  - Created `chat_with_tools()` function that preserves tool_calls in responses
  - Added dedicated tool-call path in `do_POST`: when `tools` present, routes to single member (fast path) and returns proper tool_call format
  - Sets `finish_reason: "tool_calls"` when tools are invoked
- **Result**: Tool-call requests now work end-to-end through the swarm in ~5s

---

## Swarm Sub-Agent Testing Notes

The `general-purpose-swarm` sub-agent (using `swarm/LLMSwarm (Ensemble)` model) was tested with:
- Retry logic implementation → Read file but didn't edit
- Error surfacing implementation → Read file but didn't edit
- Judge fallback fix → Read file but didn't edit

**Conclusion**: The ensemble models can read and understand code, but struggle with multi-step editing tasks. They are better suited for research, analysis, and single-file changes with very explicit instructions.

### SOLVED: the "read-only swarm sub-agent" curse

For days the `general-purpose-swarm` pi sub-agent appeared to read files but never execute edits ("0 tool uses", minutes of hanging). Root cause had nothing to do with the models:

- **pi's OpenAI-compatible streaming parser never finalizes an SSE stream on `data: [DONE]` — it waits for the connection to close.**
- The swarm's SSE responses sent a `Connection: keep-alive` header, so the socket stayed open after `[DONE]`.
- pi hung until the harness aborted the request (`terminated` / `Request was aborted`), discarding the fully-assembled assistant message — including valid tool_calls it had already parsed.
- Retries hit the same wall; the agent showed "0 tool uses" and empty output.

Fix (swarm.py): all SSE responses now send `Connection: close` and set `close_connection = True`, so the handler closes the socket right after `[DONE]`. The tool-call path additionally opens the SSE channel **before** the slow member call (TTFB 0s instead of 40s, with keep-alives in between), fails over to a second member on error, and logs member latency.

**Verified**: sub-agent read/bash/write tasks now complete in ~40-55s with correct results and real tool execution (`/tmp/swarm_subagent_proof.txt`). Also: all three member models emit proper tool_calls when tested directly and through the swarm; `max_completion_tokens` is now honored (mapped to `max_tokens`).

Debug tooling kept in-tree: touch `.swarm/dump_requests` to dump raw incoming request bodies to `.swarm/reqs/<rid>.json`.

---

## New: Agent Mode (`serve.mode = "agent"`)

A fourth mode where each member works as an **independent agent with real tool execution**, then a judge merges the findings:

1. Every active member receives the task plus the tool definitions in parallel.
2. When a member emits a `tool_calls` response, the swarm supervisor executes the tool locally and feeds the result back to that same member (up to `MAX_AGENT_ITERATIONS = 10` per member).
3. Each member finishes with a text answer; tool history is recorded per member.
4. The judge merges all member findings into one answer (errors and duplicates discarded).

### Built-in supervisor tools

| Tool | Aliases | Notes |
|------|---------|-------|
| `read` | `read_file` | Output capped at 10 KB |
| `bash` | `run_command`, `execute` | 30 s timeout, output capped at 10 KB |
| `ls` | `list_directory` | Output capped at 5 KB |
| `write` | `write_file` | Writes `content` to `path` |
| `grep` | `search` | `grep -rn`, output capped at 10 KB |

Unknown tool names return a helpful error string to the member (which can retry with a valid tool).

### Verified working

- **No tools**: each member produced its own name-marked sentence in ~6 s (4 members in parallel), judge merged.
- **Read tool**: all 4 members independently read `swarm.toml` and correctly reported 6 member sections; judge merged (~22 s).
- **Bash tool**: all 4 members independently ran `echo hello-from-swarm > /tmp/swarm_agent_test.txt` and verified the file on disk (~12 s). Members are no longer read-only — they can write files and execute commands.

### Security warning

Agent mode executes arbitrary bash from model output on the swarm host. The server has no sandbox. Only run agent mode on a trusted network / with trusted members, or restrict the tool set in `execute_tool()`.

### API notes

- `member_details` (per-member final answers) is now returned by all modes and stored for the WebUI (`GET /api/last_details`, "Show Member Outputs" button on `/ui`).
- `member_status` reports `participated` / `failed` as before.

---

## Text Completions Endpoint (`POST /v1/completions`)

The swarm now also serves the OpenAI legacy text-completions API alongside chat completions:

- **Request**: standard completion fields — `prompt` (string, list of strings, or token-id arrays), `system` (optional system prompt), `max_tokens`, `temperature`, `top_p`, `stop`, `seed`, `stream`.
- **Processing**: the prompt is converted to a chat message (`completion_messages()`) and routed through the current serve mode (ensemble / swarm / agent / fast path). The merged answer comes back in `choices[0].text` with `object: "text_completion"` and a `logprobs` field, as completion clients (e.g. SillyTavern) expect.
- **Streaming**: SSE chunks use `text_completion` object format with a `text` field, terminated by `data: [DONE]`.
- `member_status` / `member_details` are included in the response as with chat.

---

## True Token Streaming (SillyTavern-friendly)

Streaming is now genuinely incremental, not one blob at the end:

- The SSE channel opens **immediately** when the request arrives (empty init chunk + 15 s keep-alives), so clients see the response start during the fan-out phase.
- The final stage of every mode streams token-by-token from the member: `run_solo`, `run_ensemble_chat` (judge), `run_swarm` (synth), `run_agent_swarm` (judge). New `chat_stream()` consumes the member's SSE and pushes deltas out.
- Chat requests stream `chat.completion.chunk` deltas; `/v1/completions` streams `text_completion` chunks with `text` fields — both verified live (230+ chunks spread over 12 s, 299 chunks over 18 s).
- Fallback paths that never stream (e.g. synth failure -> raw draft) are backfilled as a single chunk before `[DONE]`.
- Errors during a started stream are sent as an SSE `error` payload + `[DONE]` instead of corrupting the stream with an HTTP 500.
- All SSE writes are mutex-protected (keep-alive thread vs. worker-thread deltas on the timeout path).

---

## Reasoning / Thinking Control

Thinking can be turned on/off globally, per member, and per request; each endpoint's wire dialect is configurable.

- **Serve default**: `serve.reasoning = "off" | "on"` (UI: "Thinking/reasoning default"; also per-request override `{"reasoning": "on"|"off"}` in the JSON body).
- **Per member**: `reasoning = "auto" | "on" | "off"` — `auto` follows the serve default, otherwise the member overrides it. UI: per-row `reasoning` dropdown.
- **Per member wire style** (`reasoning_style`, UI "thinking enabler"):
  | style | field sent |
  |-------|-----------|
  | `chat_template_kwargs` (default) | `chat_template_kwargs: {enable_thinking: <bool>}` (llama.cpp >= b4000, vLLM Qwen) |
  | `enable_thinking` | top-level `enable_thinking: <bool>` (vLLM forks) |
  | `thinking_type` | `thinking: {type: "enabled"|"disabled"}` (OpenRouter-style) |
  | `reasoning_effort` | `reasoning_effort: "high"|"none"` |
  | `none` | send nothing |

Precedence: request > member > serve default.

---

## TCP Health Gate (dead members skipped)

An enabled member whose endpoint silently swallows packets (dropped SYNs) used to hang the whole ensemble for the full timeout. Now, before every fan-out, members are TCP-connect-probed (2 s timeout, 15 s cache):

- connect refused / no answer -> member skipped for this request, logged `[health] member X unreachable ... - skipping`
- busy llama.cpp still accepts connections -> never mistaken for dead
- live traffic feeds the cache (any successful member call marks it alive)
- all members unreachable -> HTTP 503 `{"error": "all enabled members unreachable"}`
- judge pointing at a dead member falls back to the first reachable one

---

### Robustness: swarm mode no longer 500s on a single member failure

`run_swarm` previously used bare `chat()` for planner, critic, and synth — one offline member killed the whole request (observed as HTTP 500 from SillyTavern clients). Now:

- planner failure → retries, then falls back to a single direct-answer subtask
- worker failure → retried; if all workers fail the request errors (correctly) with a clear message
- critic failure → skipped, `(critic unavailable)` note
- synth failure → returns the raw subtask draft as the final answer
- every degraded component is listed in `member_status.failed`

---

## Summary

| Priority | Total | Done | Remaining |
|----------|-------|------|-----------|
| High | 4 | 4 | 0 |
| Medium | 4 | 4 | 0 |
| Low | 6 | 6 | 0 |
| **Total** | **14** | **14** | **0** |

✅ All improvements complete!

### Features Added
- Real streaming with keep-alive
- Swarm mode conversation context
- Member retry logic with backoff
- Error surfacing (member_status)
- Judge fallback when disabled
- Fast path for short queries
- Request timeout with HTTP 504
- BM25 + recency recall scoring
- Tool-call passthrough
- Code modularization (blackboard.py, fleet.py)
- Structured logging with request IDs
- Metrics endpoint (/api/metrics)
- Graceful shutdown (SIGTERM)
- Test suite (tests/test_swarm.py)
- Configuration validation

## Serve vs Horde split

Two runtime modes now:

- `serve` — the swarm IS a passive OpenAI-compatible API server. The endpoints
  ARE the interface; external callers (pi sub-agent, SillyTavern) drive it via
  HTTP POSTs. No polling loop.
- `horde` — additive worker mode: a worker loop polls the external cluster
  (URL from `[horde] cluster`, e.g. https://aihorde.net) for jobs, runs the
  local pipeline, and submits results back. Local endpoints stay live; horde
  requests are marked `HORDEREQ_<random>` genkey. Stats visible via
  `/api/stats` + `/api/horde_stats`; `quiet` suppresses generated text.

`[horde]` config section: `cluster`, `api_key`, `name`, `poll_interval`,
`max_length`, `max_context_length`, `quiet`. `genkey` added to PASS_PARAMS.
`set_and_start_horde.sh` launches `swarm.py horde`.
