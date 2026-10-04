# LLMSwarm

Supervisor that runs several Koboldcpp (and llama.cpp `llama-server`) instances together and
coordinates them as a swarm. Stdlib-only Python; llama.cpp itself barely
changes because the orchestration lives here, on top of the servers.

## Why this folder exists

It is the orchestration layer *above* llama.cpp: each member is a normal
`llama-server` process (local, or GPU-remote via `--rpc`), and this tool
handles roles, parallel fan-out, and a shared knowledge store.

## Quick start

```sh
cp example.llswarm.toml swarm.toml   # first run: swarm.toml is local-only (gitignored)
python3 swarm.py up                  # boot all members defined in swarm.toml
python3 swarm.py status
python3 swarm.py ask --mode swarm --problem "your question"
python3 swarm.py board               # inspect blackboard entries
python3 swarm.py serve               # OpenAI-compatible facade for chat UIs
python3 swarm.py horde               # AI-Horde worker: poll cluster, submit results
python3 swarm.py down
```

The web UI is at `http://127.0.0.1:5100/ui` and is reachable **only from the
local host** (loopback): the UI and all `/api/*` management routes return 403
for remote clients, while the OpenAI-compatible API (`/v1/chat/completions`,
`/v1/completions`, `/v1/models`, `/health`) stays open so you can share a link.
Raw config at `/api/config` (loopback); stats at `/api/stats` and
`/api/horde_stats` (loopback).

## Serving chat UIs (SillyTavern)

```sh
python3 swarm.py serve   # boots members, then serves OpenAI-compatible API
```

In SillyTavern: API Connection = **Text Completion** with **Server URL**
`http://192.168.1.101:5100` (or `/v1` base as your ST version expects),
Model = `swarm`. Configure `[serve]` in swarm.toml:

```toml
[serve]
host = "0.0.0.0"
port = 5100
mode = "ensemble"   # ensemble (default) or swarm
judge = "bravo"
```

- `ensemble`: every member answers the conversation in parallel; the judge
  merges into one reply. The judge sees the conversation + all candidates.
- `swarm`: full plan/decompose pipeline on the last user message (slow for
  chat, good for questions).
- Members answer with the full ST history (ST is still the memory); the
  blackboard records candidates as `chat` entries.

## Horde worker mode

Run the swarm as an [AI-Horde](https://stablehorde.net/) text worker: it
polls a cluster for jobs, runs each through the local members, and submits
the result back to earn kudos. `./set_and_start_horde.sh` does this
(`python3 swarm.py horde`). The local API stays live during polling, so
SillyTavern and the horde can run side by side.

```toml
[horde]
cluster = "https://stablehorde.net"   # master base; paths carry /api/v2
api_key = ""                          # your horde key, sent as the apikey header (keep it local)
name_prefix = "Swarm_Test"            # model identifier = prefix + "/" + enabled member names
worker_id = "MyWorker"                # worker identity sent on pop/submit
poll_interval = 3.0
max_length = 1024
max_context_length = 20480
concurrency = 0                       # jobs processed in parallel (0/1 = one at a time)
job_timeout = 120                     # hard wall-clock budget per job (s)
judge_reserve = 30                    # seconds of that budget reserved for the judge merge
alt_judge = ""                        # backup judge when the primary is busy/503
quiet = true                          # suppress per-job chatter
```

Judging is a ladder: primary `judge` -> `alt_judge` -> longest raw member
answer. koboldcpp per-IP rate limits (HTTP 503 "sending requests too
quickly") on the judge never fault a job: the worker honours the "try again
in N seconds" delay, falls back to the alternate judge, and finally submits
a raw candidate. A submitted single-model reply earns kudos; only genuine
failures count against the worker.

The model identifier shown on the master is the prefix joined with the
**enabled** member names (rename a member -> the identifier updates on next
start). Configure `api_key`/`worker_id` in `swarm.toml` (the web UI does not
edit horde settings).

The master starts a countdown (~150 s) on a job the moment we pop it, and
workers that let jobs expire are put into maintenance. The worker therefore
only pops when a processing slot is free, and submits (faulted, if need be)
before `job_timeout` elapses, so no job is ever counted as dropped.

Horde text jobs arrive in koboldcpp raw-generation form (`<|turn>` markers
plus koboldcpp sampling params). The worker sends the raw prompt to each
enabled member's `/v1/completions` endpoint (not chat, which reasoning
models return empty content for), then the judge merges the candidates into
one final reply. Each job logs elapsed time, member count, generation
length, and kudos earned.

## Modes

- `solo --member NAME` - one model answers, baseline.
- `ensemble` - every member answers the same problem in parallel; a judge
  (`--judge`, default last member) merges and flags disagreements.
- `swarm` - planner decomposes the problem (max 4 subtasks), workers solve
  subtasks in parallel, a critic reviews the draft, a synthesizer writes the
  final answer. Workers never see each other's raw context, only blackboard
  findings.

## Blackboard (shared knowledge storage)

SQLite + FTS5 at `.swarm/blackboard.sqlite`. Every plan, worker result,
critique, and final answer is recorded. Before each step, the supervisor
runs a full-text recall query and injects relevant prior findings into the
prompt, so later agents build on earlier ones. Swap or extend with a real
vector DB by replacing `Blackboard.recall()`.

## Members: local vs remote

Three kinds, decided by which keys you set:

- **local (managed)**: no `url` -> supervisor launches `llama-server` here,
  owns the process (up launches, down kills). `port`/`ctx`/`env`/`flags`
  all apply at launch.
- **url (connect-only external)**: `url = "http://ip:port"` -> swarm only
  health-checks and talks to it. The process itself is managed outside
  the swarm (systemd, tmux, another agent). down does not kill it.
- **host+remote_bin (experimental managed-remote)**: supervisor sshes into
  the host and launches `remote_bin` there with your flags; killing ssh
  kills the remote server via SIGHUP.
  Auth is **passwordless ssh keys only** (`BatchMode=yes`): ssh never
  prompts at runtime, so key-based auth is a hard prerequisite. Passphrases
  live in ssh-agent, never in the toml. `host` is the raw ssh destination
  (`user@ip` works; the supervisor strips the user for HTTP URLs).

Per-member `temperature` (float, omit = use request/UI default) is applied
when the supervisor calls that member - it overrides the UI's request
temperature for that member only. `ctx` stays a launch knob (local
members). Members with `host` set get `--host 0.0.0.0` automatically.

The test fleet: alpha/bravo/charlie on GPU1, delta on CPU, `echo` =
connect-only to the production Qwen3.8 server at 192.168.1.101:5001,
`foxtrot` = 0.8B launched on the Strix Halo box (192.168.1.16:8085).

## Config (swarm.toml)

`swarm.toml` is **local-only and gitignored** (it holds your horde API key).
Start from `example.llswarm.toml`, which ships with the full schema filled
in — copy it and set your own `api_key`, `worker_id`, and member URLs:

```sh
cp example.llswarm.toml swarm.toml
```

```toml
[llama]
server_bin = "~/Programming/llama.cpp-b10985/build-rpc-cuda/bin/llama-server"
boot_timeout = 600

[[member]]
name = "alpha"
model = "models/some.gguf"   # launched locally by the supervisor
port = 8081
ctx = 4096
roles = ["planner", "any"]
flags = []                   # extra llama-server args
env = { HIP_VISIBLE_DEVICES = "1" }

[[member]]
name = "remote"              # example: GPU on another machine via RPC
model = "/abs/path/model.gguf"
port = 8084
flags = ["--rpc", "192.168.1.16:50052", "-ngl", "99"]
```

If `url` is set instead of launching, the member is treated as external
(already-running server at that base URL).

Roles: `planner`, `worker`, `critic`, `synth`, or `any`. Override per run
with `--roles '{"planner":"alpha","critic":"charlie"}'`.

### Connect-only members (typical for horde)

Most deployments just connect to servers that already run elsewhere
(koboldcpp, llama-server, another swarm): give each member a `url` and the
six fields name/url/temperature/reasoning/reasoning_style/enabled. No local
process is launched. The web UI (`/ui`) lists these and has **Add Member**
and per-row **Remove** buttons, so the roster is edited live without touching
the file. Only enabled members generate; disabled ones are ignored by the
fan-out and by the horde model identifier.

```toml
[[member]]
name = "Hemmingway"
url = "http://192.168.1.101:5001"
temperature = 0.7
enabled = true
reasoning = "auto"
reasoning_style = "chat_template_kwargs"   # or enable_thinking / thinking_type / reasoning_effort / none
role = "judge"                             # worker | judge | alt_judge | planner
```

### Roles

Each member has one role (default `worker`), editable in the web UI dropdown
or via `role = "..."` in the toml:

| role        | generates?               | merges?                                        |
|-------------|--------------------------|------------------------------------------------|
| `worker`    | yes (fan-out, horde jobs)| -                                              |
| `judge`     | no                       | primary merger of candidate answers            |
| `alt_judge` | no                       | backup judge when the primary is busy/503      |
| `planner`   | ensemble fan-out only    | leads `ask --mode swarm` planning              |

A dedicated judge never joins the generation fan-out: it is free the moment
a job needs merging, which shortens time-to-submit and avoids the koboldcpp
per-IP 503 self-collision of a judge that just generated. The horde judge
ladder is: `serve.judge` (config override) -> role `judge` ->
role `alt_judge` -> `horde.alt_judge` -> longest raw worker answer.

## Layout

- `swarm.py` - entry-point shim; `python3 swarm.py ...` re-exports the package API
- `llmswarm/` - the supervisor, one file per concern:
  - `config.py` - load / validate / serialize `swarm.toml`
  - `fleet.py` - `Member` + `Fleet` (process management)
  - `blackboard.py` - SQLite + FTS5 knowledge store
  - `client.py` - per-member chat calls, retry, streaming, tools, health gate, reasoning, request logging
  - `swarm.py` - ensemble / swarm / solo modes + prompt constants
  - `agent.py` - tool-using agent mode
  - `ui.py` - web UI HTML
  - `server.py` - HTTP handler + OpenAI-compatible routes
  - `serve.py` - serve runner (graceful shutdown)
  - `horde.py` - AI-Horde worker runner
  - `cli.py` - `main()` argument handling
- `swarm.toml` - fleet definition (local-only, gitignored; create from `example.llswarm.toml`)
- `example.llswarm.toml` - full example config, no secrets
- `tests/` - test suite (`python3 tests/test_swarm.py`)
- `.swarm/` - runtime: `pids.json`, `blackboard.sqlite`, `<member>.log`

## Caveats

- `up` must run before `ask`; `ask` does not auto-boot members.
- Same-file models loaded as multiple members = duplicate VRAM.
- The `--rpc` flag offloads compute to RPC servers you start separately
  (`ggml-rpc-server` on the remote box); this tool only passes it through.
- Member health is polled, not event-driven: first `up` of a big model may
  print TIMEOUT until `boot_timeout` is tuned.
