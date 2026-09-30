#!/usr/bin/env python3
"""LLMSwarm: run several llama.cpp models together, coordinated by a supervisor.

Modes:
  solo     one member answers (baseline)
  ensemble every member answers the same problem in parallel, a judge merges
  swarm    planner decomposes, workers solve subtasks in parallel, critics
           review, synthesizer writes the final answer

All agent output is stored on a shared blackboard (SQLite + FTS5) that every
agent can query for relevant prior findings.
"""
import argparse
import json
import os
import random
import signal
import socket
import sqlite3
import shlex
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Import modularized components
from blackboard import Blackboard
from fleet import Member, Fleet, HERE, RUNTIME, RUNTIME_DIR

VALID_ROLES = {"planner", "worker", "critic", "synth", "any", "judge"}


def validate_config(cfg):
    """Validate swarm.toml structure. Returns list of warnings/errors."""
    issues = []
    
    # Validate members
    members = cfg.get("member", [])
    if not members:
        issues.append("ERROR: No members defined")
    
    seen_names = set()
    seen_ports = {}
    for m in members:
        name = m.get("name", "")
        if not name:
            issues.append("ERROR: Member without name")
            continue
        if name in seen_names:
            issues.append(f"ERROR: Duplicate member name: {name}")
        seen_names.add(name)
        
        port = m.get("port", 8080)
        if port in seen_ports:
            issues.append(f"WARNING: Port {port} used by both {seen_ports[port]} and {name}")
        seen_ports[port] = name
        
        # Validate roles
        roles = m.get("roles", ["any"])
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",")]
        for r in roles:
            if r not in VALID_ROLES:
                issues.append(f"WARNING: Member {name} has invalid role: {r} (valid: {VALID_ROLES})")
        
        # Validate ctx
        ctx = m.get("ctx", 8192)
        if ctx < 512:
            issues.append(f"WARNING: Member {name} has very small ctx ({ctx})")
        
        # Validate temperature
        temp = m.get("temperature")
        if temp is not None and not (0 <= temp <= 2.0):
            issues.append(f"WARNING: Member {name} has temperature {temp} outside typical range 0-2")
    
    # Validate serve config
    serve = cfg.get("serve", {})
    judge = serve.get("judge")
    if judge and judge not in seen_names:
        issues.append(f"ERROR: Judge '{judge}' not found in members")
    
    mode = serve.get("mode", "ensemble")
    if mode not in ("ensemble", "swarm", "solo", "agent"):
        issues.append(f"ERROR: Invalid serve mode: {mode}")
    
    # Validate blackboard
    bb = cfg.get("blackboard", {})
    retention = bb.get("retention_days", 0)
    if retention < 0:
        issues.append(f"WARNING: Negative retention_days ({retention})")
    
    return issues


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    
    # Validate config
    issues = validate_config(cfg)
    for issue in issues:
        if issue.startswith("ERROR"):
            print(f"[config] {issue}", file=sys.stderr)
        else:
            print(f"[config] {issue}", file=sys.stderr)
    
    server_bin = cfg.get("llama", {}).get(
        "server_bin", "~/llama.cpp/build/bin/llama-server"
    )
    members = {}
    order = []
    for m in cfg.get("member", []):
        members[m["name"]] = Member(m, server_bin)
        order.append(m["name"])
    return cfg, members, order


# Blackboard, Member, and Fleet are now imported from blackboard.py and fleet.py

class _BlackboardDeprecated:
    """Placeholder - actual class in blackboard.py"""
    def __init__(self, path, bb_cfg=None):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = __import__("threading").Lock()
        bb_cfg = bb_cfg or {}
        self.retention_days = float(bb_cfg.get("retention_days", 0) or 0)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries(
              id INTEGER PRIMARY KEY, kind TEXT, member TEXT,
              problem TEXT, content TEXT, ts REAL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
              USING fts5(content, content=entries, content_rowid=id);
            CREATE TRIGGER IF NOT EXISTS bb_ai AFTER INSERT ON entries BEGIN
              INSERT INTO entries_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )
        self.db.commit()

    def put(self, kind, member, problem, content):
        with self.lock:
            self.db.execute(
                "INSERT INTO entries(kind, member, problem, content, ts) VALUES (?,?,?,?,?)",
                (kind, member, problem, content, time.time()),
            )
            self.db.commit()

    def recall(self, query, limit=6):
        """Full-text search with BM25 relevance + recency weighting."""
        try:
            words = " ".join(f'"{w}"' for w in query.split() if len(w) > 3)[:200] or query
            now = time.time()
            rows = self.db.execute(
                """SELECT e.kind, e.member, e.content, e.ts,
                          bm25(entries_fts) as relevance,
                          CASE 
                            WHEN e.ts > ? - 3600 THEN 1.0
                            WHEN e.ts > ? - 86400 THEN 0.5
                            ELSE 0.1
                          END as recency
                   FROM entries e 
                   JOIN entries_fts f ON f.rowid = e.id 
                   WHERE entries_fts MATCH ? 
                   ORDER BY (-relevance * 0.7 + recency * 0.3) DESC 
                   LIMIT ?""",
                (now, now, words, limit),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        return [
            f"[{k} by {m}] {c[:1500]}" for k, m, c, ts, rel, rec in rows
        ]

    def prune(self):
        """Delete entries older than retention_days. No-op when retention is 0."""
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        with self.lock:
            n = self.db.execute(
                "SELECT count(*) FROM entries WHERE ts < ?", (cutoff,)
            ).fetchone()[0]
            self.db.execute("DELETE FROM entries WHERE ts < ?", (cutoff,))
            # external-content FTS5 does not track deletes; rebuild once
            self.db.execute(
                "INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
            self.db.commit()
        return n

    def tail(self, n=20):
        rows = self.db.execute(
            "SELECT kind, member, substr(content,1,200), datetime(ts,'unixepoch','localtime') "
            "FROM entries ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return rows


# ---------------------------------------------------------------------------
# server fleet


class Member:
    def __init__(self, conf, server_bin):
        self.name = conf["name"]
        model = conf.get("model")
        if model and not os.path.isabs(model):
            model = str(HERE / model)
        self.model = os.path.expanduser(model) if model else model
        # url set -> connect-only external (never launched/killed)
        # host set, no url -> ssh-managed remote: launched via ssh,
        #            killed when the supervisor's ssh session dies
        self.url = conf.get("url", "")
        self.host = conf.get("host", "")
        self.port = conf.get("port", 8080)
        self.ctx = conf.get("ctx", 8192)
        self.temperature = conf.get("temperature")
        self.flags = conf.get("flags", [])
        # launch-time device controls (boot, not runtime):
        # device: comma list as llama.cpp names, e.g. "CPU" or "RPC0,ROCM1"
        # rpc:    comma list of ggml-rpc-server endpoints, e.g. "192.168.1.16:50052"
        self.device = conf.get("device", "")
        self.rpc = conf.get("rpc", "")
        self.roles = set(conf.get("roles", ["any"])) or {"any"}
        self.env = conf.get("env", {})
        self.server_bin = os.path.expanduser(server_bin)
        # engine: "llama.cpp" (default) or "koboldcpp"; changes the server
        # command line. Boot-time param: set via swarm.toml / onboarding.
        self.engine = conf.get("engine", "llama.cpp")
        # ssh member: supervisor launches it over ssh (managed remote).
        # host set + ssh false = connect-only, you manage the process.
        self.ssh = bool(conf.get("ssh", True))
        self.enabled = bool(conf.get("enabled", True))
        self.remote_bin = os.path.expanduser(conf.get("remote_bin", server_bin))
        self.proc = None

    def is_external(self):
        # connect-only: the supervisor never owns this process
        return bool(self.url)

    @property
    def base(self):
        if self.url:
            return self.url.rstrip("/")
        if self.host:
            # host may be "user@1.2.3.4" for ssh; URLs need the bare ip
            host = self.host.rsplit("@", 1)[-1] if "@" in self.host else self.host
            return f"http://{host}:{self.port}"
        return f"http://127.0.0.1:{self.port}"

    def _server_cmd(self, server_bin=None, extra_host=False):
        # single command builder for local and ssh-managed members.
        # extra_host: ssh remotes must bind 0.0.0.0 (and kcpp parses ctx there).
        bin_ = server_bin or self.server_bin
        if self.engine == "koboldcpp":
            cmd = [
                bin_, "--model", self.model,
                "--port", str(self.port),
                "--ctxsize", str(self.ctx),
            ]
            if extra_host:
                cmd += ["--host", "0.0.0.0"]
            return cmd + self.flags
        cmd = [
            bin_, "-m", self.model,
            "--port", str(self.port), "--ctx-size", str(self.ctx),
            "--no-webui", "--alias", self.name,
        ]
        if self.rpc:
            cmd += ["--rpc", self.rpc]
        if self.device:
            cmd += ["--device", self.device]
        if extra_host:
            cmd += ["--host", "0.0.0.0"]
        return cmd + self.flags

    def launch(self, logf):
        if self.ssh and self.host:
            # managed remote: ssh holds the session; killing ssh kills the
            # remote server. remote env is set inside the ssh command string.
            remote_parts = self._server_cmd(
                server_bin=self.remote_bin, extra_host=True)
            remote = " ".join(shlex.quote(x) for x in remote_parts)
            envs = " ".join(f"{k}={v}" for k, v in self.env.items())
            remote_cmd = f"{envs} {remote}" if envs else remote
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
                   self.host, remote_cmd]
            return subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        cmd = self._server_cmd()
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.env.items()})
        return subprocess.Popen(
            cmd, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env=env,
        )

    def healthy(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=3):
                    return True
            except Exception:
                time.sleep(1.0)
        return False


class Fleet:
    def __init__(self, cfg, members, order):
        self.cfg = cfg
        self.members = members
        self.order = order
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.pidfile = RUNTIME_DIR / "pids.json"

    def running(self):
        if not self.pidfile.exists():
            return {}
        try:
            return json.loads(self.pidfile.read_text())
        except Exception:
            return {}

    def save(self, procs):
        self.pidfile.write_text(json.dumps(procs, indent=1))

    def up(self):
        pids = self.running()
        for name in self.order:
            m = self.members[name]
            if not m.enabled:
                print(f"[{name}] disabled -> skip")
                continue
            if m.is_external():  # external: assume already running
                print(f"[{name}] external -> {m.base}")
                continue
            if name in pids and _alive(pids[name]):
                print(f"[{name}] already up (pid {pids[name]})")
                continue
            logf = open(RUNTIME_DIR / f"{name}.log", "a")
            m.proc = m.launch(logf)
            pids[name] = m.proc.pid
            print(f"[{name}] launching pid {m.proc.pid} port {m.port} ...")
        self.save(pids)
        # wait for health of everything we manage
        timeout = self.cfg.get("llama", {}).get("boot_timeout", 300)
        for name in self.order:
            m = self.members[name]
            if not m.enabled:
                continue
            if m.is_external():
                ok = m.healthy(timeout=10)
                if not ok:
                    print(f"[{name}] external: not reachable at {m.base} (launch a server there)")
            else:
                if not _alive(self.running().get(name, -1)):
                    pass  # launched just now, pid recorded; wait on port anyway
                ok = m.healthy(timeout=timeout)
            print(f"[{name}] {'healthy' if ok else 'TIMEOUT loading model, check log'}")
            if not ok:
                print(f"    see {RUNTIME_DIR / (name + '.log')}")

    def down(self):
        for name in self.order:
            m = self.members[name]
            if m.is_external():
                continue
            pid = self.running().get(name)
            if pid and _alive(pid):
                print(f"[{name}] stopping pid {pid}")
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.pidfile.unlink(missing_ok=True)

    def status(self):
        pids = self.running()
        for name in self.order:
            m = self.members[name]
            state = "external" if m.is_external() else ("up" if _alive(pids.get(name, -1)) else "down")
            print(f"[{name}] {state} {m.base} roles={sorted(m.roles)}")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# chat API


PASS_PARAMS = ("temperature", "max_tokens", "top_p", "presence_penalty",
               "frequency_penalty", "stop", "seed", "tools", "tool_choice",
               "genkey")


def completion_messages(req):
    """Convert an OpenAI text-completion prompt into chat messages."""
    prompt = req.get("prompt", "")
    if isinstance(prompt, list):
        # list of strings or token-id arrays -> flatten to text
        parts = []
        for p in prompt:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, list):
                parts.append(" ".join(str(t) for t in p))
        prompt = "\n".join(parts)
    elif isinstance(prompt, (int, float)):
        prompt = str(prompt)
    messages = []
    sp = req.get("system")
    if sp:
        messages.append({"role": "system", "content": sp})
    messages.append({"role": "user", "content": str(prompt)})
    return messages


# Request tracking for structured logging
_request_id = None
_member_latencies = {}  # member -> list of (latency, success)

def set_request_id(rid):
    global _request_id
    _request_id = rid

def get_request_id():
    return _request_id

def log_request_start(problem_preview):
    rid = get_request_id()
    print(f"[req:{rid}] START: {problem_preview[:50]}...", file=sys.stderr)

def log_request_end(final_preview, elapsed):
    rid = get_request_id()
    print(f"[req:{rid}] END ({elapsed:.1f}s): {final_preview[:50]}...", file=sys.stderr)

def log_member_call(name, latency, success):
    rid = get_request_id()
    status = "OK" if success else "FAIL"
    print(f"[req:{rid}] member {name}: {latency:.1f}s {status}", file=sys.stderr)
    _member_latencies.setdefault(name, []).append((latency, success))
    # feed the health gate: live traffic marks members up/down
    _health_cache[name] = (time.time(), True) if success else (time.time(), False)


# TCP health gate: members whose TCP port never answers (dropped SYNs, dead
# host) are skipped for fan-out; a busy-but-listening llama.cpp still accepts
# connections, so busy members are never mistaken for dead ones.
_health_cache = {}   # name -> (timestamp, alive)
HEALTH_TTL = 15.0
HEALTH_CONNECT_TIMEOUT = 2.0


def _tcp_probe(member):
    base = member.base
    rest = base.split("//", 1)[-1].rstrip("/")
    host, _, port = rest.partition(":")
    try:
        s = socket.create_connection((host, int(port) if port else 80),
                                     timeout=HEALTH_CONNECT_TIMEOUT)
        s.close()
        return True
    except OSError:
        return False


def healthy_members(fleet, active, use_cache=True):
    """Split active members into (alive, unreachable) using cached TCP probes."""
    now = time.time()
    alive, stale = [], []
    for n in active:
        hit = _health_cache.get(n) if use_cache else None
        if hit and now - hit[0] < HEALTH_TTL:
            if hit[1]:
                alive.append(n)
        else:
            stale.append(n)
    if stale:
        with ThreadPoolExecutor(max_workers=min(8, len(stale))) as ex:
            for n, ok in zip(stale, ex.map(lambda x: _tcp_probe(fleet.members[x]), stale)):
                _health_cache[n] = (now, ok)
                if ok:
                    alive.append(n)
                else:
                    print(f"[health] member {n} unreachable at {fleet.members[n].base} - skipping", file=sys.stderr)
    # preserve configured order
    alive = [n for n in active if n in alive]
    unreachable = [n for n in active if n not in alive]
    return alive, unreachable

# Reasoning/thinking control. serve.reasoning sets the default for members
# configured as "auto"; a request may override with {"reasoning": "on"|"off"}.
_reasoning_ctx = {"serve_default": "off", "request": None}


def set_serve_reasoning(value):
    _reasoning_ctx["serve_default"] = value if value in ("on", "off") else "off"


def set_request_reasoning(value):
    # pi sends reasoning_effort (minimal/low/medium/high) or reasoning on/off;
    # any effort level means thinking is wanted -> force on, never drop to None.
    if value in ("off", "none"):
        _reasoning_ctx["request"] = "off"
    elif value in ("on", "minimal", "low", "medium", "high"):
        _reasoning_ctx["request"] = "on"
    else:
        _reasoning_ctx["request"] = None


def reasoning_fields(member):
    """Wire fields that enable/disable thinking for one member's endpoint."""
    setting = _reasoning_ctx["request"]
    if setting is None:
        setting = member.reasoning if getattr(member, "reasoning", "auto") in ("on", "off") \
            else _reasoning_ctx["serve_default"]
    on = setting == "on"
    style = getattr(member, "reasoning_style", "chat_template_kwargs")
    if style == "chat_template_kwargs":      # llama.cpp >= b4000, vLLM (Qwen et al.)
        return {"chat_template_kwargs": {"enable_thinking": on}}
    if style == "enable_thinking":           # some vLLM forks: top-level flag
        return {"enable_thinking": on}
    if style == "thinking_type":             # OpenRouter-style servers
        return {"thinking": {"type": "enabled" if on else "disabled"}}
    if style == "reasoning_effort":          # llama.cpp --reasoning-effort, OpenAI o-series
        return {"reasoning_effort": "high" if on else "none"}
    return {}                                # style "none": do not send anything


def chat(fleet, name, messages, temperature=0.7, max_tokens=2048, timeout=600, params=None):
    m = fleet.members[name]
    member = fleet.members[name]
    body = {
        "messages": messages,
        "temperature": member.temperature if member.temperature is not None else temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body.update(reasoning_fields(member))
    if params:
        for k in PASS_PARAMS:
            if k == "temperature" and member.temperature is not None:
                continue  # member's own temp wins over the UI's request
            if params.get(k) is not None:
                body[k] = params[k]
    req = urllib.request.Request(
        m.base + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        latency = time.time() - t0
        log_member_call(name, latency, True)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        latency = time.time() - t0
        log_member_call(name, latency, False)
        raise RuntimeError(f"member {name} HTTP {e.code}: {detail}") from e
    msg = out["choices"][0]["message"]
    text = msg.get("content") or ""
    if not text.strip():
        text = msg.get("reasoning_content") or ""
    return text


def chat_stream(fleet, name, messages, on_delta=None, temperature=0.7,
                max_tokens=2048, timeout=600, params=None):
    """Streaming chat with one member. Calls on_delta(text_piece) for each
    content delta and returns the full text."""
    m = fleet.members[name]
    body = {
        "messages": messages,
        "temperature": m.temperature if m.temperature is not None else temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    body.update(reasoning_fields(m))
    if params:
        for k in PASS_PARAMS:
            if k == "temperature" and m.temperature is not None:
                continue
            if params.get(k) is not None and k not in ("tools", "tool_choice"):
                body[k] = params[k]
    req = urllib.request.Request(
        m.base + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            parts, reason_parts = [], []
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except json.JSONDecodeError:
                    continue
                ch = (j.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    parts.append(piece)
                    if on_delta:
                        on_delta(piece)
                rc = delta.get("reasoning_content") or ""
                if rc:
                    reason_parts.append(rc)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log_member_call(name, time.time() - t0, False)
        raise RuntimeError(f"member {name} HTTP {e.code}: {detail}") from e
    latency = time.time() - t0
    log_member_call(name, latency, True)
    text = "".join(parts)
    if not text.strip() and reason_parts:
        # thinking model that filled reasoning_content only: use it as answer
        text = "".join(reason_parts)
        if on_delta:
            on_delta(text)
    return text


def chat_with_tools(fleet, name, messages, timeout=600, params=None):
    """Like chat() but preserves tool_calls in the response.
    Returns (text, tool_calls) tuple."""
    m = fleet.members[name]
    body = {
        "messages": messages,
        "temperature": m.temperature if m.temperature is not None else 0.7,
        "max_tokens": 2048,
        "stream": False,
    }
    body.update(reasoning_fields(m))
    if params:
        for k in PASS_PARAMS:
            if params.get(k) is not None:
                body[k] = params[k]
    req = urllib.request.Request(
        m.base + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log_member_call(name, time.time() - t0, False)
        raise RuntimeError(f"member {name} HTTP {e.code}: {detail}") from e
    log_member_call(name, time.time() - t0, True)
    msg = out["choices"][0]["message"]
    text = msg.get("content") or ""
    if not text.strip():
        text = msg.get("reasoning_content") or ""
    tool_calls = msg.get("tool_calls") or []
    return text, tool_calls


def chat_with_retry(fleet, name, messages, max_retries=2, base_delay=1.0, **kwargs):
    """Wrap chat() with exponential-backoff retry for transient errors.

    Retries on: URLError, socket.timeout, ConnectionError, HTTP 500/502/503/504.
    Does NOT retry on: HTTP 400/401/403/404 (client errors).
    Logs each retry to stderr.
    """
    import socket as _socket
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return chat(fleet, name, messages, **kwargs)
        except urllib.error.HTTPError as e:
            # Client errors (4xx) are not retryable
            if 400 <= e.code < 500:
                raise
            last_err = e
        except (urllib.error.URLError, _socket.timeout, ConnectionError, RuntimeError) as e:
            # RuntimeError is raised by chat() for HTTP errors with detail
            # Check if it's a retryable server error
            msg = str(e)
            if any(code in msg for code in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404")):
                raise
            last_err = e
        if attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            sys.stderr.write(f"[retry] member {name} attempt {attempt+1} failed ({last_err}), retrying in {delay:.1f}s\n")
            time.sleep(delay)
    raise last_err


# ---------------------------------------------------------------------------
# swarm logic

JUDGE_SYSTEM = (
    "You are the judge of a multi-model swarm. You receive the same problem and "
    "independent answers from several models. Produce one final answer: keep the "
    "best correct reasoning, discard hallucinations, note disagreements."
)
PLANNER_SYSTEM = (
    "You are the planner of a model swarm. Decompose the user problem into at most "
    "4 independent subtasks that together solve it. Respond ONLY with JSON: "
    '{"subtasks":[{"title":"...","task":"..."}]}. '
    "If no subtasks are needed, return one trivial subtask."
)
WORKER_SYSTEM = (
    "You are a worker in a model swarm. Solve the assigned subtask completely and "
    "concisely. Shared blackboard findings may be included; use what helps."
)
CRITIC_SYSTEM = (
    "You are a critic in a model swarm. Review the draft answer for errors, gaps, "
    "and hallucinations. List concrete corrections, or say 'looks sound'."
)
SYNTH_SYSTEM = (
    "You are the synthesizer of a model swarm. Merge the subtask results and critic "
    "notes into one final, complete answer to the original problem."
)


def parse_subtasks(text):
    try:
        s = text[text.index("{"): text.rindex("}") + 1]
        data = json.loads(s)
        return [t for t in data.get("subtasks", []) if t.get("task")]
    except Exception:
        return [{"title": "solve", "task": "answer the question directly"}] if text.strip() else []


def run_swarm(fleet, bb, problem, member_names, roles, history=None, stream_cb=None):
    def pick(role):
        for n in member_names:
            if role in fleet.members[n].roles:
                return n
        for n in member_names:
            if "any" in fleet.members[n].roles:
                return n
        return member_names[0]

    planner = roles.get("planner") or pick("planner")
    critic = roles.get("critic") or pick("critic")
    synth = roles.get("synth") or pick("synth")

    context = bb.recall(problem)
    ctx_blob = "\n".join(context) if context else "(empty)"
    if history:
        history_text = "\n".join(f"{m.get('role','?')}: {msg_text(m)}" for m in history[-10:])
    else:
        history_text = "(no history)"
    failed = []
    print(f"== plan ({planner}) ==")
    try:
        plan_text = chat_with_retry(fleet, planner, [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": f"Conversation history:\n{history_text}\n\nBlackboard context:\n{ctx_blob}\n\nProblem: {problem}"},
        ], temperature=0.2)
        subtasks = parse_subtasks(plan_text)
        bb.put("plan", planner, problem, plan_text)
    except Exception as e:
        # Planner failure must not kill the request: fall back to a single direct task
        print(f"-- planner {planner} failed ({e}); falling back to direct answer")
        failed.append(planner)
        subtasks = [{"title": "direct", "task": problem}]
    print(json.dumps(subtasks, indent=1)[:800])

    workers = [n for n in member_names if n not in (planner,)] or member_names
    results = [None] * len(subtasks)
    print(f"== work ({len(subtasks)} subtasks, parallel on {len(workers)}+ members) ==")

    def do(i, st):
        w = workers[i % len(workers)] if len(workers) > 1 else pick("worker")
        ctx = bb.recall(st.get("task", problem))
        msgs = [
            {"role": "system", "content": WORKER_SYSTEM},
            {"role": "user", "content": "Problem: " + problem +
             "\nYour subtask: " + st.get("task", "") +
             "\nBlackboard:\n" + ("\n".join(ctx) if ctx else "(none)")},
        ]
        ans = chat_with_retry(fleet, w, msgs)
        bb.put("result", w, problem, ans)
        return w, ans

    with ThreadPoolExecutor(max_workers=min(8, len(subtasks) + 1)) as ex:
        futs = {ex.submit(do, i, st): i for i, st in enumerate(subtasks)}
        for f in futs:
            i = futs[f]
            try:
                w, ans = f.result()
            except Exception as e:
                print(f"-- subtask {i} failed: {e}")
                failed.append(subtasks[i].get("title", f"subtask-{i}"))
                continue
            results[i] = (w, ans)
            print(f"-- [{w}] {subtasks[i].get('title','subtask')}: {ans[:200]}...")
    if not any(results):
        raise RuntimeError("all subtask workers failed")

    draft = "\n\n".join(
        f"### {subtasks[i].get('title','subtask')}\n({w})\n{ans}"
        for i, (w, ans) in enumerate(results) if w
    )
    print(f"== critique ({critic}) ==")
    try:
        critique = chat_with_retry(fleet, critic, [
            {"role": "system", "content": CRITIC_SYSTEM},
            {"role": "user", "content": f"Problem: {problem}\nDraft:\n{draft[:6000]}"},
        ], temperature=0.2)
        bb.put("critique", critic, problem, critique)
    except Exception as e:
        print(f"-- critic {critic} failed ({e}); continuing without critique")
        failed.append(critic)
        critique = "(critic unavailable)"

    print(f"== synthesize ({synth}) ==")
    synth_msgs = [
        {"role": "system", "content": SYNTH_SYSTEM},
        {"role": "user", "content": f"Problem: {problem}\nSubtask results:\n{draft[:6000]}\n"
                                    f"Critic notes:\n{critique[:2000]}"},
    ]
    try:
        if stream_cb:
            final = chat_stream(fleet, synth, synth_msgs, stream_cb, temperature=0.3)
        else:
            final = chat_with_retry(fleet, synth, synth_msgs, temperature=0.3)
    except Exception as e:
        # Synthesis failure: return the raw draft instead of erroring out
        print(f"-- synth {synth} failed ({e}); returning raw draft")
        failed.append(synth)
        final = draft
    bb.put("final", synth, problem, final)
    member_status = {
        "participated": [planner, critic, synth] + [w for w, _ in results if w],
        "failed": failed,
    }
    member_details = {}
    for i, (w, ans) in enumerate(results):
        if w:
            member_details[w] = ans
    return final, member_status, member_details


def run_ensemble(fleet, bb, problem, member_names, judge, stream_cb=None):
    context = bb.recall(problem)
    ctx_blob = "\n".join(context) if context else "(empty)"
    print(f"== fan-out to {len(member_names)} members in parallel ==")
    answers = {}

    def ask_one(name):
        msgs = [
            {"role": "system", "content": f"You are '{name}' in a model swarm. Answer directly."},
            {"role": "user", "content": f"Blackboard:\n{ctx_blob}\n\nProblem: {problem}"},
        ]
        try:
            ans = chat_with_retry(fleet, name, msgs)
        except Exception:
            return name, None
        bb.put("answer", name, problem, ans)
        return name, ans

    offline = []
    with ThreadPoolExecutor(max_workers=len(member_names)) as ex:
        for name, ans in ex.map(ask_one, member_names):
            if ans is None:
                offline.append(name)
                continue
            answers[name] = ans
            print(f"-- [{name}] {ans[:200]}...")
    if offline:
        print(f"[fan-out] offline: {', '.join(offline)}")

    member_status = {
        "participated": list(answers.keys()),
        "failed": list(offline),
    }
    member_details = {name: ans for name, ans in answers.items()}
    if judge:
        print(f"== judge ({judge}) merges answers ==")
        judge_msgs = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": "Problem: " + problem + "\n\n" +
             "\n\n---\n\n".join(f"[{k}]\n{v[:3000]}" for k, v in answers.items())},
        ]
        if stream_cb:
            merged = chat_stream(fleet, judge, judge_msgs, stream_cb, temperature=0.2)
        else:
            merged = chat(fleet, judge, judge_msgs, temperature=0.2)
        bb.put("final", judge, problem, merged)
        return merged, member_status, member_details
    return "\n\n---\n\n".join(f"[{k}]\n{v}" for k, v in answers.items()), member_status, member_details


JUDGE_CHAT_SYSTEM = (
    "You are the single voice of a model swarm. You receive a conversation and "
    "independent candidate replies from other swarm models. Produce the one final "
    "assistant reply: keep the strongest content and voice, drop weak candidates. "
    "Reply with only the final message text."
)


def run_ensemble_chat(fleet, bb, messages, member_names, judge, params=None,
                      judge_system=None, member_timeout=None, stream_cb=None):
    """Ensemble over a chat conversation: every member answers in parallel,
    judge produces the single reply SillyTavern shows."""
    candidates = {}

    def ask_one(name):
        msgs = list(messages)
        if msgs and msgs[0].get("role") == "system":
            sys_text = msg_text(msgs[0])
            msgs = [dict(msgs[0], content=sys_text +
                          "\nYou are one voice in a model swarm; answer as best you can."),
                    *msgs[1:]]
        try:
            if member_timeout:
                ans = chat_with_retry(fleet, name, msgs, params=params,
                                      timeout=member_timeout)
            else:
                ans = chat_with_retry(fleet, name, msgs, params=params)
        except Exception:
            return name, None
        bb.put("answer", name, "chat", ans)
        return name, ans

    offline = []
    with ThreadPoolExecutor(max_workers=len(member_names)) as ex:
        for name, ans in ex.map(ask_one, member_names):
            if ans is None:
                offline.append(name)
            else:
                candidates[name] = ans
    if offline:
        print(f"[chat] members offline: {', '.join(offline)}")

    convo = "\n".join(f"{m.get('role','?')}: {msg_text(m)}" for m in messages)
    # cap candidates: judge ctx is shared with the full conversation
    capped = "\n\n".join(f"[{k}] {v[:1200]}" for k, v in candidates.items())
    judge_msgs = [
        {"role": "system", "content": judge_system or JUDGE_CHAT_SYSTEM},
        {"role": "user", "content": "Conversation:\n" + convo +
         "\n\nCandidate replies:\n" + capped},
    ]
    if stream_cb:
        final = chat_stream(fleet, judge, judge_msgs, stream_cb, temperature=0.4)
    else:
        final = chat(fleet, judge, judge_msgs, temperature=0.4)
    bb.put("final", judge, "chat", final)
    member_status = {
        "participated": list(candidates.keys()),
        "failed": list(offline),
    }
    member_details = {name: ans for name, ans in candidates.items()}
    return final, member_status, member_details


# ---------------------------------------------------------------------------
# agent mode: parallel agents with tool execution

AGENT_SYSTEM = (
    "You are an independent agent in a parallel swarm. You have access to tools. "
    "Use them to investigate the task thoroughly. When you have enough information, "
    "provide your final answer as text (not a tool call). Be specific and cite findings."
)

MAX_AGENT_ITERATIONS = 10


def execute_tool(tool_name, arguments, cwd=None):
    """Execute a tool call. Returns result string."""
    try:
        if tool_name == "read" or tool_name == "read_file":
            path = arguments.get("path", "")
            with open(path, "r") as f:
                content = f.read()
            return content[:10000]  # Cap output
        
        elif tool_name == "bash" or tool_name == "run_command" or tool_name == "execute":
            import subprocess
            cmd = arguments.get("command", "")
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=cwd or "/"
            )
            output = result.stdout + result.stderr
            return output[:10000]
        
        elif tool_name == "ls" or tool_name == "list_directory":
            import subprocess
            path = arguments.get("path", ".")
            result = subprocess.run(
                ["ls", "-la", path], capture_output=True, text=True, timeout=10
            )
            return result.stdout[:5000]
        
        elif tool_name == "write" or tool_name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            with open(path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {path}"
        
        elif tool_name == "grep" or tool_name == "search":
            import subprocess
            pattern = arguments.get("pattern", arguments.get("query", ""))
            path = arguments.get("path", ".")
            result = subprocess.run(
                ["grep", "-r", "-n", pattern, path],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout[:10000] or "(no matches)"
        
        else:
            return f"Unknown tool: {tool_name}. Available: read, bash, ls, write, grep"
            
    except Exception as e:
        return f"Tool error: {e}"


def run_agent_member(fleet, name, messages, tools, timeout=300):
    """Run a single member as an agent loop. Returns (final_text, tool_history)."""
    tool_history = []
    current_messages = list(messages)
    
    for iteration in range(MAX_AGENT_ITERATIONS):
        try:
            if timeout:
                final, tool_calls = chat_with_tools(fleet, name, current_messages,
                                                    timeout=timeout, params={"tools": tools})
            else:
                final, tool_calls = chat_with_tools(fleet, name, current_messages,
                                                    params={"tools": tools})
        except Exception as e:
            return f"[ERROR] {name} failed: {e}", tool_history
        
        if not tool_calls:
            # Member is done - returned text
            return final, tool_history
        
        # Member wants to use tools - execute them
        # Add assistant message with tool_calls
        assistant_msg = {"role": "assistant", "content": final or ""}
        assistant_msg["tool_calls"] = tool_calls
        current_messages.append(assistant_msg)
        
        # Execute each tool call
        for tc in tool_calls:
            tool_name = tc.get("function", {}).get("name", "")
            try:
                arguments = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}
            
            tool_call_id = tc.get("id", "")
            tool_history.append({"tool": tool_name, "args": arguments, "iteration": iteration})
            
            # Execute tool
            result = execute_tool(tool_name, arguments)
            
            # Add tool result message
            current_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })
    
    # Max iterations reached
    return "[MAX ITERATIONS REACHED]", tool_history


def run_agent_swarm(fleet, bb, problem, member_names, judge, messages=None, tools=None,
                    member_timeout=None, history=None, stream_cb=None):
    """Agent mode: each member works independently with tools, judge merges results."""
    
    if messages is None:
        # Build messages from problem + history
        msgs = [{"role": "system", "content": AGENT_SYSTEM}]
        if history:
            history_text = "\n".join(f"{m.get('role','?')}: {msg_text(m)}" for m in history[-5:])
            msgs.append({"role": "system", "content": f"Previous context:\n{history_text}"})
        msgs.append({"role": "user", "content": problem})
        messages = msgs
    
    print(f"== agent swarm: {len(member_names)} members working independently ==")
    
    results = {}
    tool_histories = {}
    
    def run_one(name):
        text, history = run_agent_member(fleet, name, messages, tools,
                                         timeout=member_timeout or 300)
        return name, text, history
    
    with ThreadPoolExecutor(max_workers=len(member_names)) as ex:
        futures = {ex.submit(run_one, n): n for n in member_names}
        for f in futures:
            name, text, history = f.result()
            results[name] = text
            tool_histories[name] = history
            bb.put("agent_result", name, problem, text)
            print(f"-- [{name}] done ({len(history)} tool calls): {text[:150]}...")
    
    # Judge merges all results
    if judge:
        print(f"== judge ({judge}) merges {len(results)} agent results ==")
        combined_parts = []
        for name in results:
            text = results[name]
            hist = tool_histories.get(name, [])
            tools_used = ", ".join(set(h['tool'] for h in hist)) or "none"
            combined_parts.append(f"[{name}] (tools: {tools_used})\n{text[:3000]}")
        combined = "\n\n---\n\n".join(combined_parts) if results else "(no results)"
        
        agent_judge_msgs = [
            {"role": "system", "content": (
                "You are the judge of a parallel agent swarm. Each agent worked "
                "independently with tools. Merge their findings into one coherent "
                "answer. Discard errors and duplicates. Keep the best information.")},
            {"role": "user", "content": f"Problem: {problem}\n\nAgent results:\n{combined}"},
        ]
        if stream_cb:
            final = chat_stream(fleet, judge, agent_judge_msgs, stream_cb, temperature=0.2)
        else:
            final = chat(fleet, judge, agent_judge_msgs, temperature=0.2)
        bb.put("final", judge, problem, final)
        
        member_status = {
            "participated": list(results.keys()),
            "failed": [],
        }
        member_details = results
        return final, member_status, member_details
    
    # No judge - return combined
    final = "\n\n---\n\n".join(f"[{k}]\n{v}" for k, v in results.items())
    member_status = {"participated": list(results.keys()), "failed": []}
    return final, member_status, results


def run_solo(fleet, bb, problem, name, stream_cb=None):
    solo_msgs = [
        {"role": "system", "content": "Answer thoroughly."},
        {"role": "user", "content": problem},
    ]
    if stream_cb:
        ans = chat_stream(fleet, name, solo_msgs, stream_cb)
    else:
        ans = chat(fleet, name, solo_msgs)
    bb.put("answer", name, problem, ans)
    member_status = {"participated": [name], "failed": []}
    member_details = {name: ans}
    return ans, member_status, member_details


# ---------------------------------------------------------------------------
# serve: OpenAI-compatible facade for chat UIs (SillyTavern etc.) + webui


def toml_str(s):
    return json.dumps(s)


def msg_text(m):
    """Extract plain text from a chat message's content.
    Content may be a string, a list of blocks (pi/OpenAI multimodal format),
    or missing. Returns a best-effort string."""
    c = m.get("content", "") if isinstance(m, dict) else ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict):
                parts.append(p.get("text", "") or "")
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(x for x in parts if x)
    return ""


def serialize_toml(cfg, members_rows):
    lines = ["[llama]"]
    llama = cfg.get("llama", {})
    lines.append("server_bin = " + toml_str(llama.get("server_bin", "")))
    lines.append("boot_timeout = %d" % llama.get("boot_timeout", 600))
    lines.append("")
    lines.append("[blackboard]")
    bbcfg = cfg.get("blackboard", {})
    lines.append('path = "%s"' % bbcfg.get("path", ".swarm/blackboard.sqlite"))
    lines.append('retention_days = %s' % float(bbcfg.get("retention_days", 0) or 0))
    lines.append("")
    scfg = cfg.get("serve", {})
    hcfg = cfg.get("horde", {})
    lines.append("[serve]")
    lines.append("host = " + toml_str(scfg.get("host", "0.0.0.0")))
    lines.append("port = %d" % scfg.get("port", 5100))
    lines.append("mode = " + toml_str(scfg.get("mode", "ensemble")))
    lines.append("reasoning = " + toml_str(scfg.get("reasoning", "off")))
    if scfg.get("member_timeout") is not None:
        lines.append("member_timeout = %s" % float(scfg["member_timeout"]))
    if scfg.get("judge"):
        lines.append("judge = " + toml_str(scfg["judge"]))
    if scfg.get("judge_prompt"):
        lines.append("judge_prompt = " + toml_str(scfg["judge_prompt"]))
    if hcfg:
        lines.append("")
        lines.append("[horde]")
        for k in ("cluster", "api_key", "name", "worker_id", "poll_interval",
                  "max_length", "max_context_length"):
            if hcfg.get(k) is not None:
                v = hcfg[k]
                lines.append(f"{k} = " + (toml_str(v) if isinstance(v, str) else str(v)))
        if hcfg.get("quiet") is not None:
            lines.append("quiet = " + ("true" if hcfg.get("quiet") else "false"))
    for m in members_rows:
        lines.append("")
        lines.append("[[member]]")
        lines.append("name = " + toml_str(m["name"]))
        lines.append("url = " + toml_str(m.get("url", "")))
        if m.get("temperature") is not None:
            lines.append("temperature = " + str(float(m["temperature"])))
        lines.append("enabled = " + ("true" if m.get("enabled", True) else "false"))
        lines.append("reasoning = " + toml_str(m.get("reasoning", "auto")))
        lines.append("reasoning_style = " + toml_str(m.get("reasoning_style", "chat_template_kwargs")))
    return "\n".join(lines) + "\n"


def norm_members(rows):
    out = []
    for m in rows:
        env = m.get("env", {})
        if isinstance(env, list):
            env = dict(p.split("=", 1) for p in env if "=" in p)
        elif isinstance(env, str):
            env = dict(p.split("=", 1) for p in env.split(",") if "=" in p)
        if env is None:
            env = {}
        roles = m.get("roles", ["any"])
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",") if r.strip()]
        temp = m.get("temperature", "")
        temperature = None
        if temp not in ("", None):
            try:
                temperature = float(temp)
            except (TypeError, ValueError):
                temperature = None
        en = m.get("enabled", True)
        if isinstance(en, str):
            en = en.strip().lower() not in ("0", "false", "no", "off")
        out.append({
            "name": m["name"], "url": m.get("url", ""),
            "temperature": temperature,
            "enabled": bool(en),
            "reasoning": m.get("reasoning", "auto")
                if m.get("reasoning") in ("auto", "on", "off") else "auto",
            "reasoning_style": m.get("reasoning_style", "chat_template_kwargs")
                if m.get("reasoning_style") in ("chat_template_kwargs", "enable_thinking",
                                                "thinking_type", "reasoning_effort", "none")
                else "chat_template_kwargs",
        })
    return out


def members_public(fleet):
    rows = []
    for name in fleet.order:
        m = fleet.members[name]
        rows.append({
            "name": m.name, "url": m.url or "",
            "temperature": m.temperature if m.temperature is not None else "",
            "reasoning": getattr(m, "reasoning", "auto"),
            "reasoning_style": getattr(m, "reasoning_style", "chat_template_kwargs"),
            "enabled": m.enabled,
        })
    return rows


UI_HTML = """<!doctype html><html><head><meta charset=utf-8><title>LLMSwarm</title>
<style>
body{font-family:system-ui;background:#151515;color:#ddd;max-width:1280px;margin:1.5rem auto;font-size:16px}
h2{color:#9cf} input,textarea,select{background:#222;color:#ddd;border:1px solid #555;padding:8px 9px;border-radius:3px;font-size:15px}
.mrow{display:grid;grid-template-columns:180px minmax(260px,1.8fr) 80px 110px 150px 96px;gap:8px;margin:4px 0}
.mhead{display:grid;grid-template-columns:180px minmax(260px,1.8fr) 80px 110px 150px 96px;gap:8px;color:#8aa;font-size:13px}
.tog{padding:5px 8px;font-size:12px;border:0;border-radius:3px;cursor:pointer;color:#fff}
.field{margin:10px 0}.field label{display:block;margin-bottom:3px;color:#aaa}
textarea{width:100%;box-sizing:border-box}
button{background:#2a6;color:#fff;border:0;padding:8px 18px;border-radius:4px;cursor:pointer}
#status{color:#8c8;margin:0 10px}small{color:#777}
</style></head><body>
<h2>LLMSwarm</h2>
<p><small>member changes restart the llama-server processes; serve settings apply live.
Port/host changes need a full supervisor restart.</small></p>
<h3>Members</h3>
<div class=mhead><span>name</span><span>endpoint URL</span><span>temp</span><span>thinking</span><span>thinking style</span><span>enabled</span></div>
<div id=members></div>
<div class=field><label>Serve mode</label>
<select id=mode><option>ensemble</option><option>swarm</option><option>agent</option></select>
<div id=modehelp style="color:#777;font-size:12px;margin-top:4px"></div></div>
<div class=field><label>Thinking/reasoning default (for members set to "auto")</label>
<select id=reasoning><option value=off>off</option><option value=on>on</option></select>
<div style="color:#777;font-size:12px;margin-top:4px">Requests may override per-call with {"reasoning": "on"/"off"}.</div></div>
<div class=field><label>Judge / synthesizer member (ensemble only)</label><select id=judge></select></div>
<div class=field><label>Judge system prompt (live)</label>
<textarea id=jprompt rows=3></textarea></div>
<div class=field><label>Serve port (full restart)</label><input id=port size=6></div>
<div class=field><label>Member timeout s (0=off, ensemble)</label><input id=member_timeout size=6></div>
<p><button id=save>Save &amp; Apply</button><span id=status></span></p>
<h3>Last Request Details</h3>
<button onclick=showDetails()>Show Member Outputs</button>
<div id=details style="display:none;margin-top:10px">
  <div id=details_content></div>
</div>
<pre id=out></pre>
<script>
let cfg;
async function loadCfg(){
  cfg = await (await fetch('/api/config')).json();
  const mrow = document.getElementById('members'); mrow.innerHTML='';
  cfg.members.forEach(m=>{
    const d=document.createElement('div'); d.className='mrow';
    const inp=(ph,v)=>{const i=document.createElement('input');i.value=v??'';i.placeholder=ph||'';d.appendChild(i);return i;};
    const sel=(opts,v)=>{const s=document.createElement('select');
      opts.forEach(o=>{const x=document.createElement('option');x.value=o;x.textContent=o;s.appendChild(x);});
      s.value=v;d.appendChild(s);return s;};
    const name=inp('name',m.name), url=inp('http://host:port',m.url||''),
          temp=inp('temp',m.temperature),
          rsn=sel(['auto','on','off'],m.reasoning||'auto'),
          rstyle=sel(['chat_template_kwargs','enable_thinking','thinking_type','reasoning_effort','none'],
                     m.reasoning_style||'chat_template_kwargs'),
          enb=sel(['true','false'],m.enabled?'true':'false');
    enb.dataset.role='toggle';
    d._fields={name,url,temp,rsn,rstyle,enb};
    mrow.appendChild(d);
  });
  const jSel=document.getElementById('judge');
  jSel.innerHTML='';
  cfg.members.forEach(m=>{
    const o=document.createElement('option');
    o.value=m.name; o.textContent=m.name;
    jSel.appendChild(o);
  });
  jSel.value=(cfg.serve.judge && cfg.members.some(m=>m.name===cfg.serve.judge))
    ? cfg.serve.judge : cfg.members[cfg.members.length-1].name;
  document.getElementById('mode').value=cfg.serve.mode;
  updateModeHelp();
  document.getElementById('jprompt').value=cfg.serve.judge_prompt||'';
  document.getElementById('reasoning').value=cfg.serve.reasoning||'off';
  document.getElementById('port').value=cfg.serve.port;
  document.getElementById('member_timeout').value=cfg.serve.member_timeout??0;
}
const MODE_HELP={
  ensemble:"Ensemble: every member answers in parallel; the judge merges all replies into one final answer.",
  swarm:"Swarm: a planner decomposes the problem, workers solve subtasks in parallel, results merge at the end.",
  agent:"Agent: each member works independently with tools (read, bash, grep, etc.); the judge merges all findings."
};
function updateModeHelp(){
  const v=document.getElementById('mode').value;
  document.getElementById('modehelp').textContent=MODE_HELP[v]||"";
}

function collect(){
  const members=[...document.querySelectorAll('#members .mrow')].map(d=>{
    const f=d._fields;
    return {name:f.name.value.trim(),url:f.url.value.trim(),temperature:f.temp.value,
            reasoning:f.rsn.value,reasoning_style:f.rstyle.value,
            enabled:f.enb.value==='false'};
  });
  return {serve:{mode:document.getElementById('mode').value,
                 reasoning:document.getElementById('reasoning').value,
                 judge:document.getElementById('judge').value,
                 judge_prompt:document.getElementById('jprompt').value,
                 port:+document.getElementById('port').value,
                 member_timeout:+document.getElementById('member_timeout').value||0},
          members};
}
async function save(){
  const st=document.getElementById('status'); st.textContent='saving...';
  const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(collect())});
  const j=await r.json();
  st.textContent=(j.restarted?'saved, members restarted':'saved (live)')+
    (j.error?' -- '+j.error:'');
  const out=document.getElementById('out');
  out.textContent=JSON.stringify(j,null,1).slice(0,600);
  loadCfg();
}
document.getElementById('mode').addEventListener('change',updateModeHelp);
document.getElementById('save').onclick=save;

function showDetails(){
  const div=document.getElementById('details');
  const content=document.getElementById('details_content');
  if(div.style.display==='none'){
    div.style.display='block';
    fetch('/api/last_details').then(r=>r.json()).then(d=>{
      if(!d.members){content.innerHTML='<em>No details available</em>';return;}
      let html='<h4>Member Outputs (request: '+d.request_id+')</h4>';
      for(const [name, text] of Object.entries(d.members)){
        html+='<div style="margin:10px 0;padding:10px;background:#222;border-radius:4px">';
        html+='<strong>'+name+':</strong> '+text.slice(0,500)+'...';
        html+='</div>';
      }
      html+='<h4>Judge Output:</h4><div style="padding:10px;background:#333;border-radius:4px">'+d.final+'</div>';
      content.innerHTML=html;
    }).catch(e=>{content.innerHTML='<em>Error: '+e+'</em>';});
  } else {
    div.style.display='none';
  }
}

loadCfg();
</script></body></html>"""


def make_handler(state):
    """HTTP handler class factory; the server itself is run by run_serve/run_horde."""
    import http.server

    scfg = state["cfg"].get("serve", {})

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # bound in run_serve/run_horde; serve is passive here

        def log_message(self, fmt, *a):
            sys.stderr.write("[serve] %s %s\n" % (self.address_string(), fmt % a))

        def _send(self, code, payload, sse=False):
            if sse:
                chunk = {
                    "id": "swarm-%d" % int(time.time()),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "swarm",
                    "choices": [{"index": 0,
                                 "delta": {"content": payload},
                                 "finish_reason": "stop"}],
                }
                body = ("data: " + json.dumps(chunk) + "\n\n"
                        "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(code)
                self.send_header("Content-Type",
                                 "text/html" if body.lstrip().startswith(b"<!doctype")
                                 else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def _send_sse_keepalive(self, completion=False, lock=None):
            """Send SSE headers immediately and start a keep-alive thread.
            Returns a Stopper object to signal the thread to stop.
            completion=True uses text_completion chunk format (SillyTavern)."""
            import threading
            if lock is None:
                lock = threading.Lock()
            
            def mk_chunk():
                if completion:
                    return {
                        "id": "swarm-%d" % int(time.time()),
                        "object": "text_completion",
                        "created": int(time.time()),
                        "model": "swarm",
                        "choices": [{"index": 0, "text": "",
                                     "finish_reason": None}],
                    }
                return {
                    "id": "swarm-%d" % int(time.time()),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "swarm",
                    "choices": [{"index": 0,
                                 "delta": {"content": ""},
                                 "finish_reason": None}],
                }
            
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # pi-style clients only finalize an SSE stream on connection close,
            # not on [DONE] -> force close after the handler returns.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            
            # Send initial empty chunk to establish the stream
            with lock:
                self.wfile.write(("data: " + json.dumps(mk_chunk()) + "\n\n").encode())
                self.wfile.flush()
            
            # Start keep-alive thread
            stop_event = threading.Event()
            
            def keepalive():
                while not stop_event.is_set():
                    if stop_event.wait(timeout=15):  # Wait 15s between keep-alives
                        break
                    try:
                        with lock:
                            self.wfile.write(("data: " + json.dumps(mk_chunk()) + "\n\n").encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            
            t = threading.Thread(target=keepalive, daemon=True)
            t.start()
            
            # Return a stopper that signals the thread to stop
            class Stopper:
                def stop(self):
                    stop_event.set()
                    t.join(timeout=1)
            return Stopper()

        def _read_body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        _completion_messages = staticmethod(completion_messages)

        def _completion_response(self, resp, stream):
            """Reformat a chat-completion response into text_completion form."""
            if stream:
                chunk = {
                    "id": resp["id"], "object": "text_completion",
                    "created": resp["created"], "model": resp["model"],
                    "choices": [{
                        "index": 0,
                        "text": resp["choices"][0]["message"]["content"],
                        "finish_reason": resp["choices"][0].get("finish_reason", "stop"),
                    }],
                    "usage": resp.get("usage", {}),
                }
                body = ("data: " + json.dumps(chunk) + "\n\n"
                        + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                return
            out = dict(resp)
            out["object"] = "text_completion"
            ch = dict(resp["choices"][0])
            out["choices"] = [{
                "index": 0,
                "text": ch["message"]["content"],
                "finish_reason": ch.get("finish_reason", "stop"),
                "logprobs": None,
            }]
            self._send(200, out)

        # Store last request details for UI display
        state.setdefault("last_details", None)
        
        def do_GET(self):
            if self.path in ("/v1/models", "/v1/models/", "/models"):
                self._send(200, {"object": "list", "data": [
                    {"id": "swarm", "object": "model", "owned_by": "llmswarm"}]})
            elif self.path == "/api/last_details":
                self._send(200, json.dumps(state.get("last_details", {})).encode())
            elif self.path in ("/api/stats", "/api/horde", "/api/horde_stats"):
                stats = state.get("horde_stats", {})
                if stats:
                    self._send(200, json.dumps(stats).encode())
                else:
                    self._send(200, {"error": "serve mode: no horde stats"})
            elif self.path == "/api/config":
                self._send(200, json.dumps({
                    "serve": state["cfg"]["serve"],
                    "members": members_public(state["fleet"]),
                }).encode())
            elif self.path == "/health":
                self._send(200, {"status": "ok"})
            elif self.path == "/api/metrics":
                # Per-member latency and success stats
                metrics = {}
                for name, calls in _member_latencies.items():
                    total = len(calls)
                    success = sum(1 for _, s in calls if s)
                    avg_latency = sum(l for l, _ in calls) / total if total else 0
                    metrics[name] = {
                        "total_calls": total,
                        "successful": success,
                        "failed": total - success,
                        "avg_latency_s": round(avg_latency, 2),
                        "success_rate": round(success / total * 100, 1) if total else 100.0,
                    }
                self._send(200, {
                    "members": metrics,
                    "total_members": len(metrics),
                    "timestamp": time.time(),
                })
            elif self.path in ("/ui", "/"):
                self._send(200, UI_HTML.encode())
            else:
                self._send(404, {"error": "no route; try /v1/chat/completions or /ui"})

        def do_POST(self):
            if self.path == "/api/save":
                self._save(self._read_body())
                return
            if self.path == "/api/toggle":
                self._toggle(self._read_body())
                return
            is_completion = self.path in ("/v1/completions", "/completions")
            if self.path not in ("/v1/chat/completions", "/v1/completions",
                                 "/completions"):
                self._send(404, {"error": "unknown endpoint " + self.path})
                return
            try:
                req = self._read_body()
            except Exception as e:
                self._send(400, {"error": "bad json: %s" % e})
                return
            if is_completion:
                # text-completion request: adapt to the chat pipeline
                req["messages"] = self._completion_messages(req)
            # Set request ID for structured logging
            set_request_id(str(uuid.uuid4())[:8])
            
            # Optional raw-request debug dump: touch .swarm/dump_requests
            try:
                if os.path.exists(HERE / ".swarm" / "dump_requests"):
                    ddir = HERE / ".swarm" / "reqs"
                    ddir.mkdir(exist_ok=True)
                    with open(ddir / (get_request_id() + ".json"), "w") as f:
                        json.dump(req, f)
            except Exception:
                pass
            
            scfg = state["cfg"]["serve"]
            # Reasoning control: serve default, overridable per request
            set_serve_reasoning(scfg.get("reasoning", "off"))
            set_request_reasoning(req.get("reasoning"))
            messages = req.get("messages", [])
            user_msgs = [m for m in messages if m.get("role") == "user"]
            problem = msg_text(user_msgs[-1]) if user_msgs else (
                msg_text(messages[-1]) if messages else "")
            t0_start = time.time()
            log_request_start(problem[:50])
            stream = bool(req.get("stream", False))
            params = {k: req.get(k) for k in PASS_PARAMS}
            # OpenAI's newer clients send max_completion_tokens instead of max_tokens
            if params.get("max_tokens") is None and req.get("max_completion_tokens") is not None:
                try:
                    params["max_tokens"] = int(req["max_completion_tokens"])
                except (TypeError, ValueError):
                    pass
            active = [n for n in state["fleet"].order
                      if state["fleet"].members[n].enabled]
            if not active:
                self._send(503, {"error": "no enabled members; enable one in /ui"})
                return
            # skip members whose endpoint is dead (hangs on dropped packets)
            active, unreachable = healthy_members(state["fleet"], active)
            if not active:
                self._send(503, {"error": "all enabled members unreachable",
                                 "unreachable": unreachable})
                return
            judge = scfg.get("judge") or active[-1]
            if judge not in active:
                judge = active[-1]
            judge_system = scfg.get("judge_prompt") or JUDGE_CHAT_SYSTEM
            member_status = None
            has_tools = bool(req.get("tools"))
            
            # Tool-call requests: route to single member (tools need pass-through).
            # Exception: agent mode runs each member as an independent tool-using agent.
            if has_tools and scfg.get("mode", "ensemble") != "agent":
                # Open the SSE channel BEFORE the slow member call so clients
                # with TTFB timeouts (pi) do not abort and retry forever.
                tool_stream_stopper = None
                if stream:
                    tool_stream_stopper = self._send_sse_keepalive()
                try:
                    fast_member = active[0]
                    try:
                        final, tool_calls = chat_with_tools(
                            state["fleet"], fast_member, messages,
                            timeout=600, params=params)
                    except Exception:
                        # one failover to the next healthy member
                        _health_cache[fast_member] = (time.time(), False)
                        if len(active) < 2:
                            raise
                        fast_member = active[1]
                        final, tool_calls = chat_with_tools(
                            state["fleet"], fast_member, messages,
                            timeout=600, params=params)
                    member_status = {"participated": [fast_member], "failed": []}
                    resp_msg = {"role": "assistant", "content": final}
                    if tool_calls:
                        resp_msg["tool_calls"] = tool_calls
                    resp = {
                        "id": "swarm-%d" % int(time.time()),
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "swarm",
                        "choices": [{
                            "index": 0,
                            "message": resp_msg,
                            "finish_reason": "tool_calls" if tool_calls else "stop",
                        }],
                        "usage": {
                            "prompt_tokens": sum(len(msg_text(m).split()) for m in messages),
                            "completion_tokens": len(final.split()),
                            "total_tokens": 0,
                        },
                        "member_status": member_status,
                    }
                    if stream:
                        delta = {"content": final}
                        if tool_calls:
                            delta["tool_calls"] = tool_calls
                        chunk = {
                            "id": "swarm-%d" % int(time.time()),
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": "swarm",
                            "choices": [{"index": 0, "delta": delta,
                                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
                        }
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        if tool_stream_stopper:
                            tool_stream_stopper.stop()
                    else:
                        self._send(200, resp)
                except Exception as e:
                    if stream and tool_stream_stopper:
                        try:
                            self.wfile.write(("data: " + json.dumps({"error": str(e)}) + "\n\n"
                                              + "data: [DONE]\n\n").encode())
                            self.wfile.flush()
                        except Exception:
                            pass
                        tool_stream_stopper.stop()
                    else:
                        self._send(500, {"error": str(e)})
                return
            
            # Fast path: short queries (< 50 words) skip ensemble, use single member
            word_count = len(problem.split())
            use_fast_path = word_count < 50 and len(active) > 1
            
            # Request timeout: return 504 if ensemble takes too long
            request_timeout = float(scfg.get("request_timeout", 0) or 0)
            
            # Streaming: open the SSE channel up-front (keep-alives flow during
            # the fan-out phase) and stream the final judge/synth answer
            # token-by-token via stream_cb.
            import threading
            sse_lock = threading.Lock()
            sse_id = "swarm-%d" % int(time.time())
            streamed = {"any": False}
            stopper = None
            stream_cb = None
            
            def sse_write(obj):
                with sse_lock:
                    try:
                        self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            
            if stream:
                stopper = self._send_sse_keepalive(completion=is_completion,
                                                   lock=sse_lock)
                
                def stream_cb(piece):
                    if not piece:
                        return
                    streamed["any"] = True
                    if is_completion:
                        obj = {"id": sse_id, "object": "text_completion",
                               "created": int(time.time()), "model": "swarm",
                               "choices": [{"index": 0, "text": piece,
                                            "finish_reason": None}]}
                    else:
                        obj = {"id": sse_id, "object": "chat.completion.chunk",
                               "created": int(time.time()), "model": "swarm",
                               "choices": [{"index": 0,
                                            "delta": {"content": piece},
                                            "finish_reason": None}]}
                    sse_write(obj)
            
            try:
                if request_timeout > 0:
                    result_container = {}
                    
                    def run_with_result():
                        if scfg.get("mode", "ensemble") == "agent":
                            mt = float(scfg.get("member_timeout", 0) or 0) or None
                            result_container['final'], result_container['status'], result_container['details'] = run_agent_swarm(
                                state["fleet"], state["bb"], problem, active, judge,
                                messages=messages, tools=req.get("tools"),
                                member_timeout=mt, history=messages,
                                stream_cb=stream_cb)
                        elif scfg.get("mode", "ensemble") == "swarm":
                            result_container['final'], result_container['status'], result_container['details'] = run_swarm(
                                state["fleet"], state["bb"], problem, active, {}, history=messages,
                                stream_cb=stream_cb)
                        elif use_fast_path:
                            result_container['final'], result_container['status'], result_container['details'] = run_solo(
                                state["fleet"], state["bb"], problem, active[0],
                                stream_cb=stream_cb)
                        else:
                            mt = float(scfg.get("member_timeout", 0) or 0) or None
                            result_container['final'], result_container['status'], result_container['details'] = run_ensemble_chat(
                                state["fleet"], state["bb"], messages, active, judge,
                                params, judge_system, member_timeout=mt,
                                stream_cb=stream_cb)
                    
                    t = threading.Thread(target=run_with_result)
                    t.start()
                    t.join(timeout=request_timeout)
                    
                    if t.is_alive():
                        if stream:
                            sse_write({"error": f"request timed out after {request_timeout:.0f}s"})
                            with sse_lock:
                                try:
                                    self.wfile.write(b"data: [DONE]\n\n")
                                    self.wfile.flush()
                                except Exception:
                                    pass
                            if stopper:
                                stopper.stop()
                        else:
                            self._send(504, {"error": f"request timed out after {request_timeout:.0f}s"})
                        return
                    
                    final = result_container.get('final', '')
                    member_status = result_container.get('status', {})
                    member_details = result_container.get('details', {})
                else:
                    if scfg.get("mode", "ensemble") == "agent":
                        mt = float(scfg.get("member_timeout", 0) or 0) or None
                        final, member_status, member_details = run_agent_swarm(
                            state["fleet"], state["bb"], problem, active, judge,
                            messages=messages, tools=req.get("tools"),
                            member_timeout=mt, history=messages,
                            stream_cb=stream_cb)
                    elif scfg.get("mode", "ensemble") == "swarm":
                        final, member_status, member_details = run_swarm(state["fleet"], state["bb"], problem,
                                          active, {}, history=messages,
                                          stream_cb=stream_cb)
                    elif use_fast_path:
                        fast_member = active[0]
                        final, member_status, member_details = run_solo(state["fleet"], state["bb"], problem, fast_member,
                                                                        stream_cb=stream_cb)
                    else:
                        mt = float(scfg.get("member_timeout", 0) or 0) or None
                        final, member_status, member_details = run_ensemble_chat(
                            state["fleet"], state["bb"], messages,
                            active, judge, params, judge_system,
                            member_timeout=mt, stream_cb=stream_cb)
            except Exception as e:
                if stream:
                    sse_write({"error": str(e)})
                    with sse_lock:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                    if stopper:
                        stopper.stop()
                else:
                    self._send(500, {"error": str(e)})
                return
            resp = {
                "id": "swarm-%d" % int(time.time()),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "swarm",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": final},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": sum(len(msg_text(m).split()) for m in messages),
                    "completion_tokens": len(final.split()),
                    "total_tokens": 0,
                },
            }
            if member_status:
                resp["member_status"] = member_status
            if member_details:
                resp["member_details"] = member_details
                state["last_details"] = {
                    "request_id": get_request_id(),
                    "timestamp": time.time(),
                    "members": member_details,
                    "final": final[:2000],
                }
            resp["request_id"] = get_request_id()
            log_request_end(final[:50], time.time() - t0_start)
            if stream:
                # The final answer was already streamed token-by-token via
                # stream_cb; only backfill if nothing streamed (fallback paths).
                if not streamed["any"] and final:
                    if is_completion:
                        obj = {"id": sse_id, "object": "text_completion",
                               "created": int(time.time()), "model": "swarm",
                               "choices": [{"index": 0, "text": final,
                                            "finish_reason": "stop"}]}
                    else:
                        obj = {"id": sse_id, "object": "chat.completion.chunk",
                               "created": int(time.time()), "model": "swarm",
                               "choices": [{"index": 0,
                                            "delta": {"content": final},
                                            "finish_reason": "stop"}]}
                        if member_status:
                            obj["member_status"] = member_status
                    sse_write(obj)
                # Finish chunk (empty delta, finish_reason=stop)
                if streamed["any"]:
                    if is_completion:
                        obj = {"id": sse_id, "object": "text_completion",
                               "created": int(time.time()), "model": "swarm",
                               "choices": [{"index": 0, "text": "",
                                            "finish_reason": "stop"}]}
                    else:
                        obj = {"id": sse_id, "object": "chat.completion.chunk",
                               "created": int(time.time()), "model": "swarm",
                               "choices": [{"index": 0,
                                            "delta": {},
                                            "finish_reason": "stop"}]}
                    sse_write(obj)
                with sse_lock:
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                if stopper:
                    stopper.stop()
            elif is_completion:
                # Text-completion clients expect choices[].text, not message.content
                self._completion_response(resp, False)
            else:
                self._send(200, resp)

        def _toggle(self, body):
            fleet = state["fleet"]
            name = body.get("name")
            if name not in fleet.members:
                self._send(404, {"error": "unknown member: %s" % name})
                return
            new = bool(body.get("enabled", not fleet.members[name].enabled))
            rows = norm_members(state["cfg"].get("member", []))
            for r in rows:
                if r["name"] == name:
                    r["enabled"] = new
            text = serialize_toml(state["cfg"], rows)
            try:
                open(state["toml_path"], "w").write(text)
                cfg2, members2, order2 = load_config(state["toml_path"])
                fleet2 = Fleet(cfg2, members2, order2)
                fleet2.pidfile = fleet.pidfile
                state["cfg"], state["fleet"] = cfg2, fleet2
                self._send(200, {"ok": True, "name": name, "enabled": new})
            except Exception as e:
                self._send(500, {"error": "toggle failed: %s" % e})

        def _save(self, body):
            fleet = state["fleet"]
            try:
                # absent "members" = serve-params-only save; keep existing
                new_members = (norm_members(body["members"])
                               if "members" in body
                               else norm_members(state["cfg"]["member"]))
            except Exception as e:
                self._send(400, {"error": "bad members: %s" % e})
                return
            # restart needed only for process-bound changes: model/port/ctx
            # or member-set membership. roles/env changes are config-only.
            # restart needed only when LOCAL (managed) members change; edits
            # to connect-only url members are live config
            old_key = sorted(
                (m.name, m.model, m.port, m.ctx, m.device, m.rpc)
                for name in fleet.order for m in [fleet.members[name]] if not m.url
            )
            new_key = sorted(
                (m["name"], m["model"], m["port"], m["ctx"],
                 m.get("device", ""), m.get("rpc", ""))
                for m in new_members if not m.get("url")
            )
            needs_restart = old_key != new_key
            scfg = body.get("serve", {})
            old_serve = state["cfg"]["serve"]
            new_cfg = dict(state["cfg"])
            new_cfg["serve"] = {**old_serve, **scfg,
                                "host": old_serve.get("host", "0.0.0.0")}
            toml_text = serialize_toml(new_cfg, new_members)
            try:
                open(state["toml_path"], "w").write(toml_text)
                cfg2, members2, order2 = load_config(state["toml_path"])
                fleet2 = Fleet(cfg2, members2, order2)
                if needs_restart:
                    pids = fleet.running()
                    for name in fleet.order:
                        pid = pids.get(name)
                        if pid and _alive(pid):
                            try:
                                os.killpg(os.getpgid(pid), signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                    fleet2.pidfile.unlink(missing_ok=True)
                    fleet2.up()
                    restarted = True
                else:
                    # serve params are live from the reloaded cfg; running
                    # member processes keep serving with new roles/env config
                    restarted = False
                state["cfg"], state["fleet"] = cfg2, fleet2
                self._send(200, {"ok": True, "restarted": restarted,
                                 "members": order2})
            except Exception as e:
                self._send(500, {"error": "save failed: %s" % e})

    return Handler


def _make_server(state):
    import http.server
    scfg = state["cfg"].get("serve", {})
    host = scfg.get("host", "0.0.0.0")
    port = scfg.get("port", 5100)
    print(f"swarm serving on http://{host}:{port}  (ui at /ui, config at /api/config)")
    server = http.server.ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    return server, (host, port)


def run_serve(state):
    """Passive OpenAI-compatible API mode: endpoints are the interface."""
    server, _ = _make_server(state)

    def shutdown(signum, frame):
        print(f"\n[serve] Received signal {signum}, shutting down gracefully...", file=sys.stderr)
        server.shutdown()
        print("[serve] Shutdown complete.", file=sys.stderr)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    except Exception as e:
        print(f"[serve] Error: {e}", file=sys.stderr)
    finally:
        server.server_close()


def run_horde(state):
    """Worker mode: poll an external horde cluster, process jobs locally, submit back.
    The endpoints stay; requests are marked HORDEREQ_<random> to distinguish them."""
    import http.server
    import threading
    cfg = state["cfg"]
    scfg = cfg.get("serve", {})
    hcfg = cfg.get("horde", {})
    cluster = hcfg.get("cluster", "http://localhost:5001").rstrip("/")
    api_key = hcfg.get("api_key", "1uee7tPB5e0CtOhGRQuBpw")
    name = hcfg.get("name", "Swarm_Test/Cyberneurova-3.8_Xortron-V4_Hemmway")
    worker_id = hcfg.get("worker_id", "Mandurin3")
    poll_seconds = float(hcfg.get("poll_interval", 3))
    max_length = int(hcfg.get("max_length", 1024))
    max_context = int(hcfg.get("max_context_length", 8192))
    quiet = hcfg.get("quiet", True)
    pop_models = hcfg.get("pop_models", "named")
    mode = scfg.get("mode", "ensemble")
    judge = scfg.get("judge")

    state["horde_stats"] = {"jobs": 0, "kudos_earned": 0.0, "kudos_paid": 0.0,
                            "tokens_in": 0, "tokens_out": 0, "started": time.time()}
    server = _make_server(state)[0]

    def _stop(signum, frame):
        print("\n[horde] shutdown", file=sys.stderr)
        server.shutdown()
        server.server_close()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[horde] worker '{name}' polling {cluster} (local API "
          f"{scfg.get('host','0.0.0.0')}:{scfg.get('port',5100)})", file=sys.stderr)
    headers = {"apikey": api_key,
               "User-Agent": "LLMSwarm/1.0",
               "Client-Agent": "llmswarm:1.0"}

    def api(method, path, body=None):
        req = urllib.request.Request(
            cluster + path,
            data=json.dumps(body).encode() if body else None,
            headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())

    for _ in range(10):
        try:
            api("GET", "/api/v1/info/version")
            break
        except Exception:
            time.sleep(2)

    exitcounter = punishcounter = rewardcounter = 0
    session_start = time.time()
    kudos_earned = 0.0
    jobs_done = 0
    last_local_req = time.time()
    while exitcounter < 10:
        time.sleep(poll_seconds)
        if punishcounter >= 5:
            punishcounter = 0
            exitcounter += 1
            if exitcounter >= 10:
                print("[horde] exit limit reached (too many errors)", file=sys.stderr)
                break
            penalty = 2 ** exitcounter
            print(f"[horde] paused {penalty} min - too many errors", file=sys.stderr)
            time.sleep(60 * penalty)
            print("[horde] resumed", file=sys.stderr)
            continue
        if time.time() - last_local_req > 20:
            time.sleep(1)
            continue
        active = [n for n in state["fleet"].order if state["fleet"].members[n].enabled]
        try:
            pop = api("POST", "/v2/generate/text/pop",
                      {"name": name, "worker_id": worker_id, "models": active if pop_models == "named" else [],
                       "max_length": max_length, "max_context_length": max_context})
        except Exception:
            punishcounter += 1
            print("[horde] pop failed; waiting 10s", file=sys.stderr)
            time.sleep(10)
            continue
        if not pop or not pop.get("id"):
            time.sleep(1)
            continue
        jobs = pop if isinstance(pop, list) else [pop]
        for j in jobs:
            payload = j["payload"] if isinstance(j.get("payload"), dict) else j
            prompt = payload.get("prompt") or j.get("prompt", "")
            jparams = payload.get("params", j.get("params", {}))
            if not prompt:
                punishcounter += 1
                continue
            jparams["genkey"] = "HORDEREQ_%d" % random.randint(100, 999)
            jparams.setdefault("quiet", True)
            jparams.setdefault("stream", True)
            messages = payload.get("messages") or [{"role": "user", "content": prompt}]
            try:
                if mode == "ensemble":
                    final, st, det = run_ensemble_chat(
                        state["fleet"], state["bb"], messages, active, judge, jparams,
                        scfg.get("judge_prompt", ""), member_timeout=600)
                elif mode == "swarm":
                    final, st, det = run_swarm(state["fleet"], state["bb"], prompt, active, {})
                elif mode == "solo":
                    final, st, det = run_solo(state["fleet"], state["bb"], prompt, active[0])
                else:
                    final, st, det = run_agent_swarm(
                        state["fleet"], state["bb"], prompt, active, judge, messages=messages)
            except Exception as e:
                punishcounter += 1
                print(f"[horde] job {j.get('id')} failed: {e}", file=sys.stderr)
                continue
            try:
                sub = api("POST", "/v2/generate/text/submit",
                          {"id": j.get("id"), "generation": final,
                           "state": "ok", "genkey": jparams["genkey"],
                           "worker_id": worker_id})
            except Exception as e:
                punishcounter += 1
                print(f"[horde] submit failed: {e}", file=sys.stderr)
                continue
            reward = float(sub.get("reward", 1.0) or 0)
            kudos_earned += reward
            rewardcounter += 1
            if rewardcounter > 50:
                rewardcounter = max(0, rewardcounter - 1)
                if not quiet:
                    print(f"[horde] {reward:.1f} kudos; total {kudos_earned:.0f} in "
                          f"{(time.time()-session_start)/3600:.2f}h; jobs {jobs_done}",
                          file=sys.stderr)
            jobs_done += 1
            state["horde_stats"] = {
                "jobs": jobs_done, "kudos_earned": round(kudos_earned, 1),
                "kudos_paid": 0.0, "tokens_in": 0, "tokens_out": 0,
                "started": session_start}
            if not quiet:
                print(f"[horde] job {j.get('id')} done (reward {reward:.1f})",
                      file=sys.stderr)
            last_local_req = time.time()

    server.shutdown()
    server.server_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["up", "down", "status", "ask", "board",
                                        "serve", "horde"])
    ap.add_argument("--config", default=str(HERE / "swarm.toml"))
    ap.add_argument("--mode", default="swarm", choices=["swarm", "ensemble", "solo", "agent"])
    ap.add_argument("--member", default=None, help="for solo mode")
    ap.add_argument("--problem", default=None)
    ap.add_argument("--judge", default=None, help="ensemble mode judge member")
    ap.add_argument("--roles", default="{}", help='JSON, e.g. {"planner":"atlas","critic":"atlas"}')
    ap.add_argument("--bb", default=str(RUNTIME / "blackboard.sqlite"))
    args = ap.parse_args()

    cfg, members, order = load_config(args.config)
    fleet = Fleet(cfg, members, order)

    if args.command == "up":
        fleet.up()
        return
    if args.command == "down":
        fleet.down()
        return
    if args.command == "status":
        fleet.status()
        return
    if args.command in ("serve", "horde"):
        bb = Blackboard(str(RUNTIME / "blackboard.sqlite"), cfg.get("blackboard", {}))
        dropped = bb.prune()
        if dropped:
            print(f"[blackboard] pruned {dropped} entries "
                  f"(retention {bb.retention_days:g} days)")
        state = {"cfg": cfg, "fleet": fleet, "bb": bb, "toml_path": args.config}
        if args.command == "serve":
            run_serve(state)
        else:
            run_horde(state)
        return
    if args.command == "board":
        bb = Blackboard(args.bb)
        for kind, member, snippet, ts in bb.tail():
            print(f"{ts} [{kind}/{member}] {snippet}")
        return

    # ask
    problem = args.problem or " ".join(sys.argv[3:])
    if not problem:
        ap.error("ask needs --problem or a positional question")
    bb = Blackboard(args.bb)
    roles = json.loads(args.roles)
    active = [n for n in order if members[n].enabled]
    if not active:
        print("no enabled members (check enabled=true in swarm.toml)", file=sys.stderr)
        sys.exit(1)
    if args.mode == "solo":
        name = args.member or active[0]
        final, status, details = run_solo(fleet, bb, problem, name)
        print(final)
    elif args.mode == "ensemble":
        judge = args.judge or active[-1]
        final, status, details = run_ensemble(fleet, bb, problem, active, judge)
        print(final)
    elif args.mode == "agent":
        tools = None  # CLI mode doesn't have tools yet
        final, status, details = run_agent_swarm(fleet, bb, problem, active, judge,
                                                 tools=tools)
        print(final)
    else:
        final, status, details = run_swarm(fleet, bb, problem, active, roles)
        print(final)


if __name__ == "__main__":
    main()
