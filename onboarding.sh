#!/usr/bin/env bash
# LLMSwarm onboarding: interactive generator for swarm.toml.
# swarm.toml is the BOOT config (binary path, devices, rpc); the WebUI owns
# runtime serve params afterwards. Re-run this after moving binaries/models.
set -u

cd "$(dirname "$0")" || exit 1
TOML="swarm.toml"

# ---------- helpers (prompt on stderr, value on stdout) ----------
ask() {  # ask <prompt> <default>  -> echoes answer
    local prompt=$1 def=$2 ans
    while :; do
        printf '%s [%s] ' "$prompt" "$def" >&2
        read -r ans || ans=""
        printf -v ans '%s' "${ans:-$def}"
        [ -n "$ans" ] && { printf '%s\n' "$ans"; return; }
    done
}
ask_yn() {  # ask_yn <prompt> <y|n> -> echoes y or n
    local prompt=$1 def=$2 ans
    while :; do
        printf '%s [%s] ' "$prompt" "${def^^}" >&2
        read -r ans || ans="$def"
        ans=${ans:-$def}
        case "${ans,,}" in
            y|yes) echo y; return ;;
            n|no)  echo n; return ;;
        esac
    done
}
ask_choice() {  # ask_choice <prompt> <default> <opt1> <opt2>... -> echoes choice
    local prompt=$1 def=$2; shift 2
    local ans
    while :; do
        printf '%s [%s] ' "$prompt" "$def" >&2
        read -r ans || ans="$def"
        ans=${ans:-$def}
        for v in "$@"; do
            [ "$ans" = "$v" ] && { printf '%s\n' "$ans"; return; }
        done
        [ -t 0 ] || { printf '%s\n' "$ans"; return; }
    done
}
ask_masked() {  # like ask, but masks the default in the prompt (secrets)
    local prompt=$1 def=$2 ans
    local shown="(empty)"
    [ -n "$def" ] && shown="••••"
    while :; do
        printf '%s [%s] ' "$prompt" "$shown" >&2
        read -r ans || ans=""
        printf -v ans '%s' "${ans:-$def}"
        [ -n "$ans" ] && { printf '%s\n' "$ans"; return; }
    done
}
toml_q() {  # toml_q <string> -> "quoted toml basic string"
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    printf '"%s"' "$s"
}

# ---------- defaults (overridden by existing toml below) ----------
BIN="./llama.cpp/build/bin/llama-server"
ENGINE="llama.cpp"
BOOT_TIMEOUT=600
SERVE_HOST="0.0.0.0"
SERVE_PORT=5100
MODE="ensemble"
JUDGE=""
JUDGE_PROMPT=""
GPU_ENV="HIP_VISIBLE_DEVICES"
# horde defaults (overridden by existing toml below)
HORDE_ENABLED="n"
HORDE_CLUSTER="https://stablehorde.net"
HORDE_API_KEY=""
HORDE_NAME_PREFIX="Swarm_Test"
HORDE_WORKER_ID=""
HORDE_POLL_INTERVAL=3.0
HORDE_MAX_LENGTH=1024
HORDE_MAX_CONTEXT=8192
HORDE_CONCURRENCY=0
HORDE_JOB_TIMEOUT=120
HORDE_JUDGE_RESERVE=30
HORDE_ALT_JUDGE=""
HORDE_POP_MODELS="named"
HORDE_QUIET="y"
declare -a M=()   # member records: kind|name|model|port|ctx|temp|role|device|rpc|host

if [ -f "$TOML" ]; then
    echo "== existing $TOML: its values become defaults (kept in $TOML.bak) =="
    cp "$TOML" "$TOML.bak"
    eval "$(python3 - "$TOML" <<'PY'
import sys, tomllib, shlex
d = tomllib.load(open(sys.argv[1], "rb"))
l = d.get("llama", {}); s = d.get("serve", {})
print(f"BIN={shlex.quote(l.get('server_bin','./llama.cpp/build/bin/llama-server'))}")
print(f"BOOT_TIMEOUT={int(l.get('boot_timeout',600))}")
print(f"SERVE_HOST={shlex.quote(s.get('host','0.0.0.0'))}")
print(f"SERVE_PORT={int(s.get('port',5100))}")
print(f"MODE={shlex.quote(s.get('mode','ensemble'))}")
print(f"JUDGE={shlex.quote(s.get('judge',''))}")
print(f"JUDGE_PROMPT={shlex.quote(s.get('judge_prompt',''))}")
print(f"SERVE_MEMBER_TIMEOUT={float(s.get('member_timeout',300)):.0f}")
bb = d.get("blackboard", {})
print(f"BB_RETENTION_DAYS={float(bb.get('retention_days',0) or 0):g}")
h = d.get("horde", {})
print(f"HORDE_ENABLED={'y' if h else 'n'}")
print(f"HORDE_CLUSTER={shlex.quote(h.get('cluster','https://stablehorde.net'))}")
print(f"HORDE_API_KEY={shlex.quote(h.get('api_key',''))}")
print(f"HORDE_NAME_PREFIX={shlex.quote(h.get('name_prefix','Swarm_Test'))}")
print(f"HORDE_WORKER_ID={shlex.quote(h.get('worker_id',''))}")
print(f"HORDE_POLL_INTERVAL={float(h.get('poll_interval',3.0))}")
print(f"HORDE_MAX_LENGTH={int(h.get('max_length',1024))}")
print(f"HORDE_MAX_CONTEXT={int(h.get('max_context_length',8192))}")
print(f"HORDE_CONCURRENCY={int(h.get('concurrency',0))}")
print(f"HORDE_JOB_TIMEOUT={int(h.get('job_timeout',120))}")
print(f"HORDE_JUDGE_RESERVE={int(h.get('judge_reserve',30))}")
print(f"HORDE_ALT_JUDGE={shlex.quote(h.get('alt_judge',''))}")
print(f"HORDE_POP_MODELS={shlex.quote(h.get('pop_models','named'))}")
print(f"HORDE_QUIET={'y' if h.get('quiet',True) else 'n'}")
print("M=(")
for m in d.get("member", []):
    kind = "url" if m.get("url") else ("ssh" if m.get("host") else "local")
    role = m.get("role", "worker")
    rec = [kind, m.get("name",""), m.get("model") or "", str(m.get("port",8080)),
           str(m.get("ctx",8192)),
           "" if m.get("temperature") is None else str(m["temperature"]),
           role, m.get("device") or "", m.get("rpc") or "", m.get("host") or "",
           ";".join(f"{k}={v}" for k, v in (m.get("env") or {}).items()),
           m.get("remote_bin") or ""]
    if kind == "url":
        rec[9] = m.get("url") or ""
    print("  " + shlex.quote("|".join(rec)))
print(")")
PY
)"
fi

cat <<EOF

== LLMSwarm onboarding ==
Generated $TOML is the boot config; the WebUI (port below) owns
runtime serve params afterwards. Edit toml again with this script.

EOF

# ---------- 1. engine & binary ----------
echo "--- Engine & binary (boot) ---"
ENGINE=$(ask_choice "engine? llama.cpp | koboldcpp" "$ENGINE" llama.cpp koboldcpp)
while :; do
    BIN=$(ask "server binary path" "$BIN")
    if [ -x "$BIN" ]; then break; fi
    if [ ! -t 0 ]; then break; fi
    echo "  (not found/executable: $BIN)"
done
BOOT_TIMEOUT=$(ask "boot timeout seconds (health gives up after)" "$BOOT_TIMEOUT")

# ---------- 2. serve ----------
echo
echo "--- Serve endpoint (what SillyTavern sees) ---"
SERVE_HOST=$(ask "serve host" "$SERVE_HOST")
SERVE_MEMBER_TIMEOUT=$(ask "member timeout seconds, 0=wait forever (ensemble fan-out bound)" "${SERVE_MEMBER_TIMEOUT:-300}")
BB_RETENTION_DAYS=$(ask "blackboard retention days, 0=hoard forever" "${BB_RETENTION_DAYS:-0}")
SERVE_PORT=$(ask "serve port" "$SERVE_PORT")
echo "modes: solo | ensemble (parallel + judge merge) | swarm (plan/work/critique)"
MODE=$(ask_choice "mode?" "$MODE" solo ensemble swarm)
if [ "$MODE" = solo ]; then
    JUDGE=""
else
    JUDGE=$(ask "judge member name (empty = supervisor default prompt)" "$JUDGE")
fi
JUDGE_PROMPT=$(ask "judge system prompt (empty = built-in)" "$JUDGE_PROMPT")

# ---------- 2b. horde ----------
echo
HORDE_ENABLED=$(ask_yn "configure AI-Horde worker mode (python3 swarm.py horde)?" "$HORDE_ENABLED")
if [ "$HORDE_ENABLED" = y ]; then
    echo "--- Horde worker (AI-Horde) ---"
    HORDE_CLUSTER=$(ask "cluster URL" "$HORDE_CLUSTER")
    HORDE_API_KEY=$(ask_masked "api key (secret)" "$HORDE_API_KEY")
    HORDE_NAME_PREFIX=$(ask "name prefix (model id = prefix/membernames)" "$HORDE_NAME_PREFIX")
    HORDE_WORKER_ID=$(ask "worker id" "$HORDE_WORKER_ID")
    HORDE_POLL_INTERVAL=$(ask "poll interval seconds" "$HORDE_POLL_INTERVAL")
    HORDE_MAX_LENGTH=$(ask "max length offered to master" "$HORDE_MAX_LENGTH")
    HORDE_MAX_CONTEXT=$(ask "max context offered to master" "$HORDE_MAX_CONTEXT")
    HORDE_CONCURRENCY=$(ask "concurrency, 0=one job at a time" "$HORDE_CONCURRENCY")
    HORDE_JOB_TIMEOUT=$(ask "job timeout seconds" "$HORDE_JOB_TIMEOUT")
    HORDE_JUDGE_RESERVE=$(ask "judge reserve seconds" "$HORDE_JUDGE_RESERVE")
    HORDE_ALT_JUDGE=$(ask "alt judge member name (empty = none)" "$HORDE_ALT_JUDGE")
    HORDE_POP_MODELS=$(ask_choice "pop models [named|auto]" "$HORDE_POP_MODELS" named auto)
    HORDE_QUIET=$(ask_yn "quiet, suppress per-job chatter" "$HORDE_QUIET")
fi

# ---------- 3. members ----------
echo
echo "--- Members ---"
echo "kinds: local (supervisor launches) | url (connect-only: you own it)"
echo "       ssh  (supervisor ssh-launches; set host + remote_bin)"
GPU_ENV=$(ask "GPU env var for this build (HIP/CUDA_VISIBLE_DEVICES)" "$GPU_ENV")

if [ "${#M[@]}" -gt 0 ]; then
    echo
    echo "found ${#M[@]} existing member(s):"
    for rec in "${M[@]}"; do
        IFS='|' read -r k n _ _ _ _ r _ _ h <<< "$rec"
        echo "  $n ($k) role=$r"
    done
    yn=$(ask_yn "reconfigure each member? (n keeps them as-is)" n)
else
    yn=n
fi

if [ "${#M[@]}" -eq 0 ]; then
    # fresh: build from scratch
    M=()
    cnt=$(ask "how many members? (0-8)" 3)
    for i in $(seq 1 "$cnt"); do
        n=$(ask "member $i name" "m$i")
        k=$(ask_choice "  kind [local|url|ssh]" local local url ssh)
        case $k in
            local)
                md=$(ask "  model path (.gguf)" "")
                pt=$(ask "  port" "$((8080 + i))")
                ct=$(ask "  ctx" 8192)
                tp=$(ask "  temperature (empty = follow request)" "")
                r=$(ask_choice "  role [worker|judge|alt_judge|planner]" worker worker judge alt_judge planner)
                dv=$(ask "  device names (empty = auto; ROCM1 | RPC0,ROCM1)" "")
                rpc=$(ask "  rpc endpoints (empty = none)" "")
                ev=$(ask "  env pin (KEY=VAL; e.g. ${GPU_ENV}=1)" "")
                M+=("local|$n|$md|$pt|$ct|$tp|$r|$dv|$rpc||$ev")
                ;;
            url)
                u=$(ask "  url (http://host:port)" "http://127.0.0.1:5001")
                r=$(ask_choice "  role [worker|judge|alt_judge|planner]" worker worker judge alt_judge planner)
                M+=("url|$n||||$r|||$u")
                ;;
            ssh)
                h=$(ask "  ssh host (user@ip, or ip for same user; keys required)" "")
                md=$(ask "  model path on the remote box" "")
                pt=$(ask "  port" "$((8080 + i))")
                ct=$(ask "  ctx" 8192)
                tp=$(ask "  temperature (empty = follow request)" "")
                r=$(ask_choice "  role [worker|judge|alt_judge|planner]" worker worker judge alt_judge planner)
                dv=$(ask "  device names" "")
                rpc=$(ask "  rpc endpoints" "")
                ev=$(ask "  env pin (KEY=VAL)" "")
                rb=$(ask "  remote binary path (default: server_bin)" "")
                M+=("ssh|$n|$md|$pt|$ct|$tp|$r|$dv|$rpc|$h|$ev|$rb")
                ;;
        esac
    done
elif [ "$yn" = n ]; then
    :  # keep existing records untouched
else
    declare -a M2=()
    for rec in "${M[@]}"; do
        IFS='|' read -r k n md pt ct tp r dv rpc h ev rbin <<< "$rec"
        echo
        echo "reconfigure: $n"
        n=$(ask "  name" "$n")
        k=$(ask_choice "  kind [local|url|ssh]" "$k" local url ssh)
        case $k in
            local)
                md=$(ask "  model path" "$md")
                pt=$(ask "  port" "$pt")
                ct=$(ask "  ctx" "$ct")
                tp=$(ask "  temperature" "$tp")
                r=$(ask_choice "  role [worker|judge|alt_judge|planner]" "$r" worker judge alt_judge planner)
                dv=$(ask "  device names" "$dv")
                rpc=$(ask "  rpc endpoints" "$rpc")
                ev=$(ask "  env pin (KEY=VAL; ; separates pairs)" "${ev//;/, }")
                M2+=("local|$n|$md|$pt|$ct|$tp|$r|$dv|$rpc||${ev//, /;}")
                ;;
            url)
                u=$(ask "  url" "${h:-http://127.0.0.1:5001}")
                r=$(ask_choice "  role [worker|judge|alt_judge|planner]" "$r" worker judge alt_judge planner)
                M2+=("url|$n||||$r|||$u")
                ;;
            ssh)
                h=$(ask "  ssh host (user@ip; passwordless keys)" "$h")
                md=$(ask "  model path on remote" "$md")
                pt=$(ask "  port" "$pt")
                ct=$(ask "  ctx" "$ct")
                tp=$(ask "  temperature" "$tp")
                r=$(ask_choice "  role [worker|judge|alt_judge|planner]" "$r" worker judge alt_judge planner)
                dv=$(ask "  device names" "$dv")
                rpc=$(ask "  rpc endpoints" "$rpc")
                ev=$(ask "  env pin (KEY=VAL; ; separates pairs)" "$ev")
                rb=$(ask "  remote binary path (empty = server_bin)" "$rbin")
                M2+=("ssh|$n|$md|$pt|$ct|$tp|$r|$dv|$rpc|$h|$ev|$rb")
                ;;
        esac
    done
    M=("${M2[@]}")
fi

# ---------- 4. emit ----------
{
    echo "# generated by onboarding.sh on $(date +%F)"
    echo "[llama]"
    echo "server_bin = $(toml_q "$BIN")"
    echo "boot_timeout = $BOOT_TIMEOUT"
    echo
    echo "[serve]"
    echo "host = $(toml_q "$SERVE_HOST")"
    echo "port = $SERVE_PORT"
    echo "mode = $(toml_q "$MODE")"
    [ -n "$JUDGE" ] && echo "judge = $(toml_q "$JUDGE")"
    [ -n "$JUDGE_PROMPT" ] && echo "judge_prompt = $(toml_q "$JUDGE_PROMPT")"
    echo "member_timeout = $SERVE_MEMBER_TIMEOUT"
    echo
    echo "[blackboard]"
    echo "retention_days = $BB_RETENTION_DAYS"
    if [ "$HORDE_ENABLED" = y ]; then
        echo
        echo "[horde]"
        echo "cluster = $(toml_q "$HORDE_CLUSTER")"
        echo "api_key = $(toml_q "$HORDE_API_KEY")"
        echo "name_prefix = $(toml_q "$HORDE_NAME_PREFIX")"
        echo "worker_id = $(toml_q "$HORDE_WORKER_ID")"
        echo "poll_interval = $HORDE_POLL_INTERVAL"
        echo "max_length = $HORDE_MAX_LENGTH"
        echo "max_context_length = $HORDE_MAX_CONTEXT"
        echo "concurrency = $HORDE_CONCURRENCY"
        echo "job_timeout = $HORDE_JOB_TIMEOUT"
        echo "judge_reserve = $HORDE_JUDGE_RESERVE"
        echo "alt_judge = $(toml_q "$HORDE_ALT_JUDGE")"
        echo "pop_models = $(toml_q "$HORDE_POP_MODELS")"
        echo "quiet = $([ "$HORDE_QUIET" = y ] && echo true || echo false)"
    fi
} > "$TOML.new"

for rec in "${M[@]}"; do
    IFS='|' read -r k n md pt ct tp r dv rpc h ev rbin <<< "$rec"
    {
        echo
        echo "[[member]]"
        echo "name = $(toml_q "$n")"
        case $k in
            url) echo "url = $(toml_q "${h:-http://127.0.0.1:5001}")" ;;
            ssh)
                echo "host = $(toml_q "$h")"
                echo "port = $pt"
                echo "model = $(toml_q "$md")"
                echo "ctx = $ct"
                ;;
            local)
                echo "model = $(toml_q "$md")"
                echo "port = $pt"
                echo "ctx = $ct"
                ;;
        esac
        if [ -n "$tp" ]; then echo "temperature = $tp"; fi
        if [ "$k" = ssh ] && [ -n "${rbin:-}" ]; then echo "remote_bin = $(toml_q "$rbin")"; fi
        if [ "$k" != url ] && [ -n "$dv" ]; then echo "device = $(toml_q "$dv")"; fi
        if [ "$k" != url ] && [ -n "$rpc" ]; then echo "rpc = $(toml_q "$rpc")"; fi
        if [ "$ENGINE" != llama.cpp ]; then echo "engine = $(toml_q "$ENGINE")"; fi
        echo "role = $(toml_q "$r")"
        if [ -n "${ev:-}" ]; then
            printf 'env = {'
            first=1
            IFS=';' read -ra pairs <<< "$ev"
            for p in "${pairs[@]}"; do
                k2=${p%%=*}; v2=${p#*=}
                [ $first -eq 1 ] && first=0 || printf ','
                printf ' %s = "%s"' "$k2" "$v2"
            done
            printf ' }\n'
        fi
    } >> "$TOML.new"
done

# validate before swapping in
if python3 -c "import tomllib,sys; tomllib.load(open('$TOML.new','rb'))" 2>/dev/null; then
    mv "$TOML.new" "$TOML"
    echo
    echo "== $TOML written =="
else
    # keep .new so the user can inspect the broken file
    echo
    echo "== $TOML.new written (validation deferred: fix and re-run) =="
fi

echo
echo "engine: $ENGINE   binary: $BIN"
echo "serve : $SERVE_HOST:$SERVE_PORT  mode=$MODE  judge=${JUDGE:-default}"
[ "$HORDE_ENABLED" = y ] && echo "horde : $HORDE_CLUSTER  worker=${HORDE_WORKER_ID:-?}  concurrency=$HORDE_CONCURRENCY"
echo "members:"
for rec in "${M[@]}"; do
    IFS='|' read -r k n md _ _ _ r _ _ _ ev <<< "$rec"
    printf '  %-10s %-6s %-28s %s\n' "$n" "($k)" "${md:0:28}" "role=$r"
done
echo
echo "note: serve params (mode/judge/port) stay UI-editable; member changes"
echo "need a supervisor restart. Restart with: python3 swarm.py serve"
