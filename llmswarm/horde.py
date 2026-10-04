"""Horde runner: poll an external horde cluster, process jobs locally, submit."""
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .client import chat, raw_complete
from .server import _make_server


def _fmt_uptime(seconds):
    """Format a duration in seconds as HH:MM:SS."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def run_horde(state):
    """Worker mode: poll an external horde cluster, process jobs locally, submit back.
    The endpoints stay; requests are marked HORDEREQ_<random> to distinguish them."""
    import http.server
    import threading
    cfg = state["cfg"]
    scfg = cfg.get("serve", {})
    hcfg = cfg.get("horde", {})
    cluster = hcfg.get("cluster", "http://localhost:5001").rstrip("/")
    api_key = hcfg.get("api_key", "")
    if not api_key:
        print("[horde] no api_key in [horde] of swarm.toml - set your horde key "
              "there (swarm.toml is local-only and gitignored)", file=sys.stderr)
        sys.exit(1)
    # Master model identifier derived from the live enabled member roster so renames
    # propagate. Only the prefix is hand-set in config.
    name_prefix = hcfg.get("name_prefix", "Swarm_Test")
    name = name_prefix + "/" + "_".join(
        n for n in state["fleet"].order if state["fleet"].members[n].enabled)
    worker_id = hcfg.get("worker_id", "swarm-worker")
    poll_seconds = float(hcfg.get("poll_interval", 3))
    max_length = int(hcfg.get("max_length", 1024))
    max_context = int(hcfg.get("max_context_length", 8192))
    quiet = hcfg.get("quiet", True)
    pop_models = hcfg.get("pop_models", "named")
    mode = scfg.get("mode", "ensemble")
    judge = scfg.get("judge")
    alt_judge = hcfg.get("alt_judge", "")

    state["horde_stats"] = {"jobs": 0, "kudos_earned": 0.0, "kudos_last": 0.0,
                            "kudos_avg_per_job": 0.0, "kudos_paid": 0.0,
                            "tokens_in": 0, "tokens_out": 0, "total_chars": 0,
                            "uptime_s": 0.0, "uptime_human": "00:00:00",
                            "avg_job_time_s": 0.0, "last_job": None,
                            "started": time.time()}
    server = _make_server(state)[0]

    def _stop(signum, frame):
        print(f"\n[horde] Received signal {signum}, shutting down...", file=sys.stderr)
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        # Force a clean exit: concurrent.futures ThreadPoolExecutor threads are
        # non-daemon and would otherwise wait for in-flight jobs at interpreter
        # shutdown (atexit), so SystemExit alone can hang Ctrl+C. All member
        # servers are external processes, so there is nothing to reap here.
        os._exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[horde] worker '{name}' polling {cluster} (local API "
          f"{scfg.get('host','0.0.0.0')}:{scfg.get('port',5100)})", file=sys.stderr)
    headers = {"apikey": api_key,
               "User-Agent": "LLMSwarm/1.0",
               "Client-Agent": "llmswarm:1.0",
               "Content-Type": "application/json"}

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

    exitcounter = punishcounter = 0
    session_start = time.time()
    kudos_earned = 0.0
    jobs_done = 0
    stats_lock = threading.Lock()

    def punish():
        nonlocal punishcounter
        with stats_lock:
            punishcounter += 1

    # One job end-to-end (fan-out + judge + submit) runs in its own worker thread so
    # queued jobs overlap: while job A is being judged/submitted, job B's member
    # fan-out can already be generating. Bounded by `concurrency`.
    def fault(j, why):
        # Submitting faulted quickly is better than letting the job expire on the
        # master: an expired (dropped) job counts against us, a faulted one does not.
        try:
            api("POST", "/api/v2/generate/text/submit",
                {"id": j.get("id"), "generation": "", "state": "faulted", "seed": -1})
        except Exception as e:
            print(f"[horde] faulted submit failed: {e}", file=sys.stderr)

    def process_job(j):
        nonlocal kudos_earned, jobs_done, in_flight
        with stats_lock:
            in_flight += 1
        try:
            _process_job(j)
        finally:
            with stats_lock:
                in_flight -= 1

    def _process_job(j):
        nonlocal kudos_earned, jobs_done
        payload = j["payload"] if isinstance(j.get("payload"), dict) else {}
        prompt = payload.get("prompt") or j.get("prompt", "")
        if not prompt:
            punish()
            return
        t_job = time.time()
        # Hard deadline: submit (even faulted) before the master's job timeout so
        # the job is never counted as dropped.
        deadline = t_job + job_timeout
        job_max_length = int(payload.get("max_length", max_length))
        min_p = payload.get("min_p")
        temp = payload.get("temperature", 1.0)
        active_all = [n for n in state["fleet"].order if state["fleet"].members[n].enabled]
        # dedicated judges/alt_judges/planners do not generate horde jobs
        active = [n for n in active_all
                  if getattr(state["fleet"].members[n], "role", "worker") == "worker"] \
            or active_all
        # Horde entries go to the dedicated horde blackboard, tagged with the
        # job id so each job flushes only its own rows (concurrency-safe).
        bbh = state.get("bb_horde")
        job_tag = "horde:" + str(j.get("id"))
        candidates = {}
        def flush_job():
            if bbh:
                bbh.delete_by_problem(job_tag)
        def raw_one(nm):
            try:
                # per-member timeout: leave judge_reserve for the merge step
                rem = deadline - time.time() - judge_reserve
                ans = raw_complete(state["fleet"], nm, prompt, params=payload,
                                   max_length=job_max_length, min_p=min_p,
                                   temperature=temp, timeout=max(10, int(rem)))
                out = (ans or "").strip() or None
                if out and bbh:
                    bbh.put("answer", nm, job_tag, out)
                return nm, out
            except Exception:
                return nm, None
        with ThreadPoolExecutor(max_workers=len(active)) as ex:
            for nm, ans in ex.map(raw_one, active):
                if ans:
                    candidates[nm] = ans
        if not candidates:
            punish()
            fault(j, "no member answers")
            return
        if time.time() > deadline - 10:
            # generation ate the budget; fault now rather than expire mid-judge
            fault(j, "deadline reached before merge")
            flush_job()
            return
        merged = "\n\n".join(f"[{nm}] {ans[:600]}" for nm, ans in candidates.items())
        # Judge ladder: primary judge, then alt_judge (horde config), then raw
        # candidate fallback. A 503-prone judge must never fault the whole job.
        mem = state["fleet"].members
        judge_names = []
        # role-assigned judges follow the config overrides (judge, alt_judge)
        role_j = [n for n in active_all
                  if getattr(mem[n], "role", "worker") in ("judge", "alt_judge")]
        role_j.sort(key=lambda n: mem[n].role != "judge")
        for cand in [judge] + role_j + [alt_judge]:
            if cand and cand in mem and mem[cand].enabled and cand not in judge_names:
                judge_names.append(cand)
        if not judge_names:
            judge_names = [active[0]]
        judge_msgs = [
            {"role": "system",
             "content": "Several models answered the same request. Produce one single "
                        "final answer that best resolves the request. Do not mention the "
                        "models or list alternatives; just give the answer."},
            {"role": "user",
             "content": "Original request:\n" + prompt +
                        "\n\nCandidate answers:\n" + merged},
        ]
        # Deadline-aware judge: chat_with_retry applies its timeout per-attempt, so
        # 3 retries could run minutes past the master's countdown. Retry 503s only
        # while budget remains; every attempt is capped at the time actually left.
        final = None
        jerr = None
        judge_used = None
        for jm in judge_names:
            for attempt in range(2):
                rem = int(deadline - time.time())
                if rem < 10:
                    break
                try:
                    final = chat(state["fleet"], jm, judge_msgs,
                                 temperature=0.4, timeout=rem)
                    judge_used = jm
                    break
                except urllib.error.HTTPError as e:
                    jerr = e
                    if e.code != 503:
                        break
                    # honour koboldcpp's "try again in N seconds"
                    delay = 2.0 * (attempt + 1)
                    try:
                        m = re.search(r"try again in (\d+)",
                                      e.read().decode(errors="replace"))
                        if m:
                            delay = int(m.group(1)) + 1.0
                    except Exception:
                        pass
                    rem_f = deadline - time.time() - 10
                    if rem_f <= 0:
                        break
                    time.sleep(min(delay, rem_f))
                except Exception as e:
                    jerr = e
                    break
            if final is not None:
                break
        if final is None:
            # Judges busy/rate-limited: submit the longest raw answer. One
            # model's reply earns kudos; a faulted job punishes us, and the
            # punish storm maintenance-flagged the worker on the master.
            final = max(candidates.values(), key=len)
            judge_used = "(raw)"
            print(f"[horde] job {str(j.get('id'))[:8]} judge failed ({jerr}); "
                  f"submitting raw candidate", file=sys.stderr)
        if not final or not final.strip():
            # fall back to the best raw candidate rather than submit empty
            final = next(iter(candidates.values()))
        if not final or not final.strip():
            try:
                api("POST", "/api/v2/generate/text/submit",
                    {"id": j.get("id"), "generation": "", "state": "faulted", "seed": -1})
            except Exception as e:
                punish()
                print(f"[horde] faulted submit failed: {e}", file=sys.stderr)
            flush_job()
            return
        # Judge output goes on the horde board right before it is sent out,
        # so the board holds exactly one in-flight job's material per job.
        if bbh and final and final.strip():
            bbh.put("final", judge_used or "?", job_tag, final.strip())
        try:
            sub = api("POST", "/api/v2/generate/text/submit",
                      {"id": j.get("id"), "generation": final, "seed": 0})
        except Exception as e:
            punish()
            body = ""
            if getattr(e, "read", None):
                try:
                    body = e.read().decode(errors="replace")[:200]
                except Exception:
                    pass
            print(f"[horde] submit failed: {e} {body}", file=sys.stderr)
            flush_job()
            return
        # Sent to the requester: flush this job's entries from the horde board.
        flush_job()
        reward = float(sub.get("reward", 1.0) or 0)
        elapsed = time.time() - t_job
        with stats_lock:
            kudos_earned += reward
            jobs_done += 1
            prev = state.get("horde_stats") or {}
            total_chars = prev.get("total_chars", 0) + len(final or "")
            total_job_time = prev.get("total_job_time", 0.0) + elapsed
            uptime = time.time() - session_start
            avg_kudos = kudos_earned / jobs_done if jobs_done else 0.0
            avg_time = total_job_time / jobs_done if jobs_done else 0.0
            state["horde_stats"] = {
                "jobs": jobs_done,
                "kudos_earned": round(kudos_earned, 2),
                "kudos_last": round(reward, 2),
                "kudos_avg_per_job": round(avg_kudos, 2),
                "kudos_paid": prev.get("kudos_paid", 0.0),
                "tokens_in": prev.get("tokens_in", 0),
                "tokens_out": prev.get("tokens_out", 0),
                "total_chars": total_chars,
                "uptime_s": round(uptime, 1),
                "uptime_human": _fmt_uptime(uptime),
                "avg_job_time_s": round(avg_time, 1),
                "total_job_time": total_job_time,
                "last_job": {
                    "id": str(j.get("id") or ""),
                    "kudos": round(reward, 2),
                    "elapsed_s": round(elapsed, 1),
                    "chars": len(final or ""),
                    "members": len(candidates),
                    "judge": judge_used,
                },
                "started": session_start,
            }
        print(f"[horde] job {str(j.get('id'))[:8]}: +{reward:.2f} kudos "
              f"| session {kudos_earned:.2f} kudos over {jobs_done} jobs "
              f"({avg_kudos:.2f}/job) | uptime {_fmt_uptime(uptime)} | "
              f"{elapsed:.1f}s, {len(final or '')} chars, {len(candidates)} members, "
              f"judge={judge_used}",
              file=sys.stderr)

    concurrency = max(1, int(hcfg.get("concurrency", 0)))
    # Hard per-job wall-clock budget. Submitting faulted before this beats letting
    # the master expire the job (expired = dropped = punished).
    job_timeout = int(hcfg.get("job_timeout", 120))
    judge_reserve = int(hcfg.get("judge_reserve", 30))
    job_pool = ThreadPoolExecutor(max_workers=concurrency)
    in_flight = 0
    while exitcounter < 10:
        time.sleep(poll_seconds)
        with stats_lock:
            over = punishcounter >= 5
            if over:
                punishcounter = 0
                exitcounter += 1
        if over:
            if exitcounter >= 10:
                print("[horde] exit limit reached (too many errors)", file=sys.stderr)
                break
            penalty = 2 ** exitcounter
            print(f"[horde] paused {penalty} min - too many errors", file=sys.stderr)
            time.sleep(60 * penalty)
            print("[horde] resumed", file=sys.stderr)
            continue
        # The master starts the job countdown the moment we pop. Popping while all
        # pool slots are busy puts jobs in a local queue where they expire on the
        # master and count as drops. Only pop when a slot is genuinely free.
        with stats_lock:
            busy = in_flight >= concurrency
        if busy:
            time.sleep(1)
            continue
        try:
            pop = api("POST", "/api/v2/generate/text/pop", {
                "name": worker_id,
                "models": [name] if pop_models == "named" else [],
                "max_length": max_length,
                "max_context_length": max_context,
                "softprompts": [],
                "bridge_agent": "llmswarm:1.0:local"})
        except Exception as e:
            punish()
            print(f"[horde] pop failed: {e}; waiting 10s", file=sys.stderr)
            time.sleep(10)
            continue
        # master returns a single job envelope; id is null/empty when no job is queued.
        if isinstance(pop, list):
            pop = pop[0] if pop else {}
        if not isinstance(pop, dict) or not pop.get("id"):
            # no job right now — normal idle state, not an error
            time.sleep(1)
            continue
        job_pool.submit(process_job, pop)

    server.shutdown()
    server.server_close()


