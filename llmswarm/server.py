"""HTTP handler + server factory for the OpenAI-compatible facade and UI."""
import ipaddress
import json
import os
import signal
import sys
import threading
import time
import urllib.request
import uuid

from .agent import run_agent_swarm
from .client import (PASS_PARAMS, chat_with_tools, completion_messages,
                     get_request_id, healthy_members, log_request_end,
                     log_request_start, msg_text, set_request_id,
                     set_request_reasoning, set_serve_reasoning, _health_cache,
                     _member_latencies)
from .config import load_config, members_public, norm_members, serialize_toml
from .fleet import Fleet, _alive
from .swarm import JUDGE_CHAT_SYSTEM, run_ensemble_chat, run_solo, run_swarm
from .ui import UI_HTML


def make_handler(state):
    """HTTP handler class factory; the server itself is run by run_serve/run_horde."""
    import http.server

    scfg = state["cfg"].get("serve", {})

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # bound in run_serve/run_horde; serve is passive here

        def log_message(self, fmt, *a):
            sys.stderr.write("[serve] %s %s\n" % (self.address_string(), fmt % a))

        def _local_only_blocked(self):
            """The UI (/, /ui) and /api/* management routes are reachable only from
            loopback (127.0.0.1 / ::1), so the OpenAI-compatible API can be shared
            over the LAN while the management interface stays private.
            Returns True when the request should be rejected."""
            p = self.path
            is_local_only = (p == "/" or p == "/ui" or p.startswith("/api/"))
            if not is_local_only:
                return False
            try:
                return not ipaddress.ip_address(self.client_address[0]).is_loopback
            except Exception:
                # unparseable peer address: treat as non-local (safer default)
                return True

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
            if self._local_only_blocked():
                self._send(403, {"error": "this endpoint is only available from the local host (127.0.0.1)"})
                return
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
                    "horde": state["cfg"].get("horde", {}),
                    "members": members_public(state["fleet"]),
                }).encode())
            elif self.path == "/api/horde_models":
                # Live text-model roster proxied from the cluster (same query
                # KoboldHordeOverseer uses). There is no public roster WITHOUT
                # the type=text filter -- /api/v2/status/models alone returns
                # image-only. Curated list is an offline fallback.
                from .config import KNOWN_TEXT_MODELS
                hcfg = state["cfg"].get("horde", {})
                cluster = hcfg.get("cluster", "").rstrip("/")
                # This machine's own horde worker registers under a derived
                # name (name_prefix + enabled members) when it joins the
                # cluster; expose it so the user can route to their own worker.
                own = ""
                if hcfg.get("pop_models", "named") == "named":
                    own = hcfg.get("name_prefix", "Swarm_Test") + "/" + "_".join(
                        n for n in state["fleet"].order
                        if state["fleet"].members[n].enabled)
                models, meta, source = [], {}, "curated"
                if cluster:
                    try:
                        req = urllib.request.Request(
                            cluster + "/api/v2/status/models?type=text&model_state=all",
                            headers={"User-Agent": "LLMSwarm/1.0",
                                      "Client-Agent": "llmswarm:1.0"})
                        with urllib.request.urlopen(req, timeout=20) as r:
                            data = json.loads(r.read())
                        if isinstance(data, list):
                            for e in data:
                                nm = e.get("name", "")
                                if nm:
                                    models.append(nm)
                                    meta[nm] = {"workers": e.get("count", 0),
                                                "eta": e.get("eta", 0),
                                                "performance": e.get("performance", 0)}
                            if models:
                                source = "live"
                    except Exception:
                        models = []
                if not models:
                    models = list(KNOWN_TEXT_MODELS)
                self._send(200, json.dumps({
                    "models": models,
                    "meta": meta,
                    "own_worker": own,
                    "cluster": cluster,
                    "has_key": bool(hcfg.get("api_key")),
                    "source": source,
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
            if self._local_only_blocked():
                self._send(403, {"error": "this endpoint is only available from the local host (127.0.0.1)"})
                return
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
            
            # Optional raw-request debug dump: touch .swarm/dump_requests.
            # Suppressed while quiet is on (privacy: no plaintext prompt dumps).
            try:
                dump_quiet = state["cfg"].get("horde", {}).get(
                    "quiet", state["cfg"].get("serve", {}).get("quiet", True))
                if (os.path.exists(HERE / ".swarm" / "dump_requests")
                        and not dump_quiet):
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
            enabled_all = [n for n in state["fleet"].order
                           if state["fleet"].members[n].enabled]
            if not enabled_all:
                self._send(503, {"error": "no enabled members; enable one in /ui"})
                return
            # dedicated judges/alt_judges never generate; fan-out is workers (+planners)
            active = [n for n in enabled_all
                      if getattr(state["fleet"].members[n], "role", "worker")
                      in ("worker", "planner")] or enabled_all
            # skip members whose endpoint is dead (hangs on dropped packets)
            active, unreachable = healthy_members(state["fleet"], active)
            if not active:
                self._send(503, {"error": "all enabled members unreachable",
                                 "unreachable": unreachable})
                return
            judge = scfg.get("judge")
            if judge not in enabled_all:
                role_j = [n for n in enabled_all
                          if getattr(state["fleet"].members[n], "role", "worker") == "judge"]
                judge = role_j[0] if role_j else active[-1]
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
                            timeout=6000, params=params)
                    except Exception:
                        # one failover to the next healthy member
                        _health_cache[fast_member] = (time.time(), False)
                        if len(active) < 2:
                            raise
                        fast_member = active[1]
                        final, tool_calls = chat_with_tools(
                            state["fleet"], fast_member, messages,
                            timeout=6000, params=params)
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
                    
                    # daemon=True so an in-flight request cannot hold the process
                    # open after serve_forever() has been asked to shut down.
                    t = threading.Thread(target=run_with_result, daemon=True)
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
            # Defensive: normalize a None final (e.g. an empty stream) to "" so
            # usage/serialization below never sees None and crashes the handler.
            if final is None:
                final = ""
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
            # to connect-only url members AND horde-routed members (master,
            # no local process) are live config. Both are excluded here.
            old_key = sorted(
                (m.name, m.model, m.port, m.ctx, m.device, m.rpc)
                for name in fleet.order for m in [fleet.members[name]]
                if not m.url and not m.is_horde()
            )
            new_key = sorted(
                (m["name"], m["model"], m["port"], m["ctx"],
                 m.get("device", ""), m.get("rpc", ""))
                for m in new_members
                if not m.get("url") and not m.get("horde_model")
            )
            needs_restart = old_key != new_key
            scfg = body.get("serve", {})
            old_serve = state["cfg"]["serve"]
            new_cfg = dict(state["cfg"])
            new_cfg["serve"] = {**old_serve, **scfg,
                                "host": old_serve.get("host", "0.0.0.0")}
            hcfg_in = body.get("horde", {})
            old_horde = state["cfg"].get("horde", {})
            if hcfg_in:
                new_cfg["horde"] = {**old_horde, **hcfg_in}
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


