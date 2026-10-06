"""Swarm modes: ensemble, swarm (plan/work/critique/synth) and solo."""
import itertools
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from .client import chat, chat_stream, chat_with_retry, msg_text


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


def try_parse_subtasks(text):
    """Return the subtask list from planner output, or None if unusable."""
    if not text or not text.strip():
        return None
    try:
        s = text[text.index("{"): text.rindex("}") + 1]
        data = json.loads(s)
        if isinstance(data, list):
            data = {"subtasks": data}
        tasks = [t for t in data.get("subtasks", []) if t.get("task")]
        return tasks or None
    except Exception:
        return None


def parse_subtasks(text):
    tasks = try_parse_subtasks(text)
    if tasks is not None:
        return tasks
    return [{"title": "solve", "task": "answer the question directly"}] if text.strip() else []


# Per-request rotation offset for role assignment (itertools.count.next is
# thread-safe in CPython).
_role_offset = itertools.count()


def run_swarm(fleet, bb, problem, member_names, roles, history=None, stream_cb=None,
              spread=False):
    def eligible(role):
        pool = [n for n in member_names if role in fleet.members[n].roles]
        if not pool:
            pool = [n for n in member_names if "any" in fleet.members[n].roles]
        return pool or list(member_names)

    # Rotate role assignment once per request so the roles are spread across
    # members over time instead of always landing on the first match.
    off = next(_role_offset)

    def rotated(pool):
        if len(pool) <= 1:
            return pool
        k = off % len(pool)
        return pool[k:] + pool[:k]

    # Explicit role pins (serve config) win over rotation and spread.
    taken = set()

    def assign(role):
        n = roles.get(role)
        if not n:
            pool = rotated(eligible(role))
            if spread:
                # distinct members when possible so the orchestration roles
                # are not all concentrated on one model
                n = next((x for x in pool if x not in taken), pool[0])
            else:
                n = pool[0]
        taken.add(n)
        return n

    planner = assign("planner")
    critic = assign("critic")
    synth = assign("synth")

    context = bb.recall(problem)
    ctx_blob = "\n".join(context) if context else "(empty)"
    if history:
        history_text = "\n".join(f"{m.get('role','?')}: {msg_text(m)}" for m in history[-10:])
    else:
        history_text = "(no history)"
    failed = []
    print(f"== plan ({planner}) ==")
    # Try the chosen planner first, then fall through to the next members if
    # the call fails or returns nothing. Cap at 3 attempts to bound latency.
    plan_pool = [planner] + [n for n in member_names if n != planner]
    subtasks = None
    draft_direct = None  # (member, text): planner wrote the answer, not a plan
    for p in plan_pool[:3]:
        try:
            plan_text = chat_with_retry(fleet, p, [
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": f"Conversation history:\n{history_text}\n\nBlackboard context:\n{ctx_blob}\n\nProblem: {problem}"},
            ], temperature=0.2)
        except Exception as e:
            print(f"-- planner {p} failed ({e}); trying next member")
            failed.append(p)
            continue
        tasks = try_parse_subtasks(plan_text)
        if tasks:
            planner = p
            subtasks = tasks
            bb.put("plan", p, problem, plan_text)
            break
        if plan_text.strip():
            # The model wrote the answer instead of a plan (common for
            # roleplay traffic): reuse it as the draft and skip the workers.
            draft_direct = (p, plan_text)
            print(f"-- planner {p} wrote prose instead of a plan "
                  f"(raw: {plan_text[:200]!r}); using it as the draft")
            break
        print(f"-- planner {p} returned empty output; trying next member")
    if subtasks is None and draft_direct:
        planner, plan_text = draft_direct
        subtasks = [{"title": "draft", "task": problem}]
        bb.put("result", planner, problem, plan_text)
        results = [(0, planner, plan_text)]
        print("-- skipping worker phase; critique + synthesize refine the draft")
    else:
        if subtasks is None:
            # No planner produced a usable plan: fall back to a single direct task
            print("-- no usable plan; falling back to direct answer")
            subtasks = [{"title": "direct", "task": problem}]

        workers = [n for n in member_names if n not in (planner,)] or member_names
        if len(subtasks) == 1 and len(workers) > 1:
            # Single subtask (planner trivial/fallback): fan it out to every
            # worker so the swarm still answers with independent views.
            jobs = [(0, subtasks[0], w) for w in workers]
        else:
            jobs = [(i, st, workers[i % len(workers)]) for i, st in enumerate(subtasks)]
        results = [None] * len(jobs)
        print(f"== work ({len(subtasks)} subtask(s), {len(jobs)} job(s) on {len(workers)} workers) ==")

        # Serialize calls per member: koboldcpp rate-limits concurrent
        # requests from the same IP (HTTP 503 "sending requests too").
        member_locks = {w: threading.Lock() for _, _, w in jobs}

        def do(i, st, w):
            ctx = bb.recall(st.get("task", problem))
            msgs = [
                {"role": "system", "content": WORKER_SYSTEM},
                {"role": "user", "content": "Problem: " + problem +
                 "\nYour subtask: " + st.get("task", "") +
                 "\nBlackboard:\n" + ("\n".join(ctx) if ctx else "(none)")},
            ]
            with member_locks[w]:
                ans = chat_with_retry(fleet, w, msgs)
            bb.put("result", w, problem, ans)
            return w, ans

        with ThreadPoolExecutor(max_workers=min(8, len(jobs) + 1)) as ex:
            futs = {ex.submit(do, i, st, w): j for j, (i, st, w) in enumerate(jobs)}
            for f in futs:
                j = futs[f]
                i, st, w = jobs[j]
                try:
                    w, ans = f.result()
                except Exception as e:
                    print(f"-- subtask {i} ({w}) failed: {e}")
                    failed.append(st.get("title", f"subtask-{i}"))
                    continue
                results[j] = (i, w, ans)
                print(f"-- [{w}] {st.get('title','subtask')}: {ans[:200]}...")
        if not any(results):
            raise RuntimeError("all subtask workers failed")

    print(json.dumps(subtasks, indent=1)[:800])

    draft = "\n\n".join(
        f"### {subtasks[i].get('title','subtask')}\n({w})\n{ans}"
        for i, w, ans in results if i is not None
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
        "participated": [planner, critic, synth] + [w for _, w, _ in results if w],
        "failed": failed,
    }
    member_details = {}
    for i, w, ans in results:
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


