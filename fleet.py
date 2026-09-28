"""Fleet: manages llama-server processes for swarm members."""
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / ".swarm"
RUNTIME_DIR = RUNTIME


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class Member:
    """Represents a single model server in the fleet."""
    
    def __init__(self, conf, server_bin):
        self.name = conf["name"]
        model = conf.get("model")
        if model and not os.path.isabs(model):
            model = str(HERE / model)
        self.model = os.path.expanduser(model) if model else model
        self.url = conf.get("url", "")
        self.host = conf.get("host", "")
        self.port = conf.get("port", 8080)
        self.ctx = conf.get("ctx", 8192)
        self.temperature = conf.get("temperature")
        self.flags = conf.get("flags", [])
        self.device = conf.get("device", "")
        self.rpc = conf.get("rpc", "")
        self.roles = set(conf.get("roles", ["any"])) or {"any"}
        self.env = conf.get("env", {})
        self.server_bin = os.path.expanduser(server_bin)
        self.engine = conf.get("engine", "llama.cpp")
        self.ssh = bool(conf.get("ssh", True))
        self.enabled = bool(conf.get("enabled", True))
        # reasoning: "auto" | "on" | "off" (auto = serve default)
        self.reasoning = conf.get("reasoning", "auto")
        # which wire-format the endpoint uses to enable/disable thinking
        self.reasoning_style = conf.get("reasoning_style", "chat_template_kwargs")
        self.remote_bin = os.path.expanduser(conf.get("remote_bin", server_bin))
        self.proc = None
    
    def is_external(self):
        return bool(self.url)
    
    @property
    def base(self):
        if self.url:
            return self.url.rstrip("/")
        if self.host:
            host = self.host.rsplit("@", 1)[-1] if "@" in self.host else self.host
            return f"http://{host}:{self.port}"
        return f"http://127.0.0.1:{self.port}"
    
    def _server_cmd(self, server_bin=None, extra_host=False):
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
            remote_parts = self._server_cmd(server_bin=self.remote_bin, extra_host=True)
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
    """Manages a fleet of model servers."""
    
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
            if m.is_external():
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
        
        timeout = self.cfg.get("llama", {}).get("boot_timeout", 300)
        for name in self.order:
            m = self.members[name]
            if not m.enabled:
                continue
            if m.is_external():
                ok = m.healthy(timeout=10)
                if not ok:
                    print(f"[{name}] external: not reachable at {m.base}")
            else:
                if not _alive(self.running().get(name, -1)):
                    pass
                ok = m.healthy(timeout=timeout)
            print(f"[{name}] {'healthy' if ok else 'TIMEOUT loading model'}")
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
            enabled = "enabled" if m.enabled else "disabled"
            print(f"[{name}] {state} ({enabled}) {m.base} roles={sorted(m.roles)}")
