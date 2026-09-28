# LLMSwarm

Supervisor that runs several llama.cpp `llama-server` instances together and
coordinates them as a swarm. Stdlib-only Python; llama.cpp itself barely
changes because the orchestration lives here, on top of the servers.

## Why this folder exists

It is the orchestration layer *above* llama.cpp: each member is a normal
`llama-server` process (local, or GPU-remote via `--rpc`), and this tool
handles roles, parallel fan-out, and a shared knowledge store.

## Quick start

```sh
python3 swarm.py up                  # boot all members defined in swarm.toml
python3 swarm.py status
python3 swarm.py ask --mode swarm --problem "your question"
python3 swarm.py board               # inspect blackboard entries
python3 swarm.py down
```

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

## Layout

- `swarm.py` - the whole supervisor
- `swarm.toml` - fleet definition
- `models/` - symlinks or copies of GGUFs used by the fleet
- `.swarm/` - runtime: `pids.json`, `blackboard.sqlite`, `<member>.log`

## Caveats

- `up` must run before `ask`; `ask` does not auto-boot members.
- Same-file models loaded as multiple members = duplicate VRAM.
- The `--rpc` flag offloads compute to RPC servers you start separately
  (`ggml-rpc-server` on the remote box); this tool only passes it through.
- Member health is polled, not event-driven: first `up` of a big model may
  print TIMEOUT until `boot_timeout` is tuned.
