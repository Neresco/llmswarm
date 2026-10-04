"""Config: load, validate and serialize swarm.toml."""
import json
import sys
import tomllib

from .fleet import Member


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
        
        # members are connect-only: identity is the url. Show each enabled endpoint
        # explicitly rather than warning on matching ports (different hosts may reuse a
        # port). Disabled members are noise in the startup banner.
        en = m.get("enabled", True)
        if isinstance(en, str):
            en = en.strip().lower() not in ("0", "false", "no", "off")
        url = m.get("url", "")
        if url and en:
            issues.append(f"INFO: {name} - {url}")
        
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

        # Validate system_prompt
        sp = m.get("system_prompt")
        if sp is not None and not isinstance(sp, str):
            issues.append(f"WARNING: Member {name} system_prompt must be a string")
        sp_on = m.get("system_prompt_enabled")
        if sp_on is not None and not isinstance(sp_on, bool):
            issues.append(f"WARNING: Member {name} system_prompt_enabled must be a boolean")
    
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


def toml_str(s):
    return json.dumps(s)


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
        for k in ("cluster", "api_key", "name_prefix", "worker_id", "poll_interval",
                  "max_length", "max_context_length", "concurrency",
                  "job_timeout", "judge_reserve", "alt_judge"):
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
        lines.append("role = " + toml_str(m.get("role", "worker")))
        if m.get("system_prompt"):
            lines.append("system_prompt = " + toml_str(m["system_prompt"]))
        if m.get("system_prompt_enabled"):
            lines.append("system_prompt_enabled = " + ("true" if m["system_prompt_enabled"] else "false"))
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
            "role": m.get("role") if m.get("role") in
                    ("worker", "judge", "alt_judge", "planner") else "worker",
            "system_prompt": m.get("system_prompt", ""),
            "system_prompt_enabled": bool(m.get("system_prompt_enabled", False)),
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
            "role": getattr(m, "role", "worker"),
            "system_prompt": getattr(m, "system_prompt", ""),
            "system_prompt_enabled": getattr(m, "system_prompt_enabled", False),
        })
    return rows


