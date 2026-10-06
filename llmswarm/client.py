"""Chat client: call one member endpoint (retry/stream/tools), health gate,
reasoning control and per-request logging."""
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


PASS_PARAMS = ("temperature", "max_tokens", "top_p", "presence_penalty",
               "frequency_penalty", "stop", "seed", "tools", "tool_choice",
               "genkey")


# --- AI Horde text routing -------------------------------------------------
# A horde member has no direct endpoint; it submits to the horde master
# (async) and polls until a worker with the requested model produces output.

def _horde_cfg(fleet):
    hcfg = fleet.cfg.get("horde", {})
    return hcfg.get("cluster", "").rstrip("/"), hcfg.get("api_key", "")


def _messages_to_prompt(messages):
    """Serialize chat messages into the flat prompt the horde text API expects."""
    parts = []
    for m in messages:
        role = (m.get("role") or "user").upper()
        content = m.get("content", "")
        if isinstance(content, list):  # multi-part content -> text only
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict))
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


def horde_text_generate(cluster, api_key, model, prompt, max_length=2048,
                        temperature=0.7, min_p=None, priority="relaxed",
                        timeout=120, poll_interval=2.0):
    """Submit a text job to the horde master and poll until it is ready.

    AI Horde text API: POST /api/v2/generate/text/async returns an id, then
    GET /api/v2/generate/text/status/{id} is polled for the generation. The
    status path is /status/ -- /check/ 404s.

    priority: relaxed (free, standard) | immediate/instant (spend kudos for a
    faster queue position) | stall/slow/queue/undetermined (lower).
    """
    hdrs = {"apikey": api_key, "User-Agent": "LLMSwarm/1.0",
            "Client-Agent": "llmswarm:1.0", "Content-Type": "application/json"}
    params = {"models": [model], "max_length": int(max_length),
              "temperature": temperature}
    if min_p is not None:
        params["min_p"] = min_p
    body = {"prompt": prompt, "params": params, "priority": priority}
    req = urllib.request.Request(cluster + "/api/v2/generate/text/async",
                                 data=json.dumps(body).encode(), headers=hdrs,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    job_id = res.get("id")
    if not job_id:
        raise RuntimeError(f"horde async returned no id: {res}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(poll_interval)
        creq = urllib.request.Request(
            cluster + "/api/v2/generate/text/status/" + job_id,
            headers={"apikey": api_key, "User-Agent": "LLMSwarm/1.0"})
        with urllib.request.urlopen(creq, timeout=30) as r:
            cres = json.loads(r.read())
        if cres.get("faulted"):
            raise RuntimeError(f"horde text job {job_id} faulted: {cres}")
        gens = cres.get("generations") or []
        if gens:
            text = gens[0].get("text", "")
            if text.strip():
                return text
    raise TimeoutError(f"horde text job {job_id} not ready within {timeout}s")


def _horde_call(fleet, name, m, prompt, max_length, temperature, min_p, timeout):
    """Route one member call through the horde master. Returns the text."""
    cluster, api_key = _horde_cfg(fleet)
    if not cluster or not api_key:
        raise RuntimeError(
            f"horde member {name} needs [horde] cluster and api_key in swarm.toml")
    priority = fleet.cfg.get("horde", {}).get("priority", "relaxed")
    t0 = time.time()
    try:
        text = horde_text_generate(cluster, api_key, m.horde_model, prompt,
                                   max_length=max_length,
                                   temperature=(m.temperature if m.temperature is not None
                                               else temperature),
                                   min_p=min_p, priority=priority, timeout=timeout)
        log_member_call(name, time.time() - t0, True)
        return text
    except Exception:
        log_member_call(name, time.time() - t0, False)
        raise


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
        if fleet.members[n].is_horde():
            # horde-routed members are always considered available; they reach
            # the master, not a direct endpoint, so there is nothing to probe.
            alive.append(n)
            continue
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


def chat(fleet, name, messages, temperature=0.7, max_tokens=2048, timeout=6000, params=None):
    m = fleet.members[name]
    if m.is_horde():
        return _horde_call(fleet, name, m, _messages_to_prompt(messages),
                           max_tokens, temperature, None, timeout)
    member = fleet.members[name]
    messages = with_member_system(member, messages)
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


def raw_complete(fleet, name, prompt, params=None, max_length=200, temperature=1.0,
                 min_p=None, timeout=6000):
    """Raw KoboldCpp-style generation via /v1/completions.
    Horde text jobs arrive in koboldcpp raw-prompt format (<|turn> markers) with
    koboldcpp sampling params; the native raw endpoint handles them, but chat
    completions do not (reasoning models bury the answer in reasoning_content
    and return empty content)."""
    m = fleet.members[name]
    if m.is_horde():
        return _horde_call(fleet, name, m, prompt, max_length, temperature,
                           min_p, timeout)
    body = {"prompt": prompt, "max_tokens": int(max_length), "temperature": temperature,
            "stream": False}
    if min_p is not None:
        body["min_p"] = min_p
    if params:
        for k in ("max_length", "min_p", "temperature", "top_p", "dynatemp_range",
                  "dynatemp_exponent", "smoothing_factor", "stop", "seed", "tfs"):
            v = params.get(k)
            if v is None:
                continue
            if k == "max_length":
                body["max_tokens"] = int(v)
            elif k == "temperature" and m.temperature is not None:
                continue  # member's own temp wins
            else:
                body[k] = v
    req = urllib.request.Request(m.base + "/v1/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        latency = time.time() - t0
        log_member_call(name, latency, True)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log_member_call(name, time.time() - t0, False)
        raise RuntimeError(f"member {name} HTTP {e.code}: {detail}") from e
    return out["choices"][0]["text"]


def chat_stream(fleet, name, messages, on_delta=None, temperature=0.7,
                max_tokens=2048, timeout=6000, params=None):
    m = fleet.members[name]
    if m.is_horde():
        # The horde cannot stream; emit the whole answer once it is ready.
        text = _horde_call(fleet, name, m, _messages_to_prompt(messages),
                           max_tokens, temperature, None, timeout)
        if on_delta and text:
            on_delta(text)
        return text
    messages = with_member_system(m, messages)
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


def chat_with_tools(fleet, name, messages, timeout=6000, params=None):
    """Like chat() but preserves tool_calls in the response.
    Returns (text, tool_calls) tuple."""
    m = fleet.members[name]
    if m.is_horde():
        # Horde text API has no tool support; plain generation, no tool_calls.
        text = _horde_call(fleet, name, m, _messages_to_prompt(messages),
                           2048, 0.7, None, timeout)
        return text, []
    messages = with_member_system(m, messages)
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


def with_member_system(m, messages):
    """Apply the member's system prompt to messages (returns a copy).

    If the messages already start with a system message, merge into it:
    some chat templates (e.g. Qwen3) reject a second system message with
    'System message must be at the beginning'.
    """
    if not (m.system_prompt_enabled and m.system_prompt):
        return list(messages)
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        first["content"] = m.system_prompt + "\n" + msg_text(first)
        return [first] + list(messages[1:])
    return [{"role": "system", "content": m.system_prompt}] + list(messages)


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


