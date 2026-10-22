from __future__ import annotations

import atexit
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent 

START_CMD = (os.getenv("LEECH_START_CMD") or "python -m leech").strip()
STATE_PATH = Path(os.getenv("LEECH_CHANNEL_STATE")
                  or ROOT / "persistent/v1/channel.json")
PROBE_SECS = float(os.getenv("SUPERVISOR_PROBE_SECS", "75") or 75)
AUTOPULL_SECS = float(os.getenv("AUTOPULL_SECS", "300") or 300)
PORT = int(os.getenv("SUPERVISOR_PORT", "8765") or 8765)
TOKEN = (os.getenv("SUPERVISOR_TOKEN") or "").strip()
WEBHOOK = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()

CHANNEL_BRANCHES: dict[str, str] = {}
for pair in (os.getenv("LEECH_CHANNEL_BRANCHES")
             or "stable:master,canary:canary").split(","):
    if ":" in pair:
        name, _, branch = pair.partition(":")
        CHANNEL_BRANCHES[name.strip().lower()] = branch.strip()

try:
    START_ARGV: list[str] = shlex.split(START_CMD)
except ValueError:
    START_ARGV = []
SHELL_FALLBACK = not START_ARGV

APP_TOKENS: list[str] = START_ARGV[1:] if START_ARGV else [START_CMD]


def _log(message: str) -> None:
    print(f"[supervisor {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _detect_source_dir() -> Path:
    for candidate in (ROOT, ROOT / "src"):
        if (candidate / "leech" / "__init__.py").is_file():
            return candidate
    return ROOT


SRC_DIR = _detect_source_dir()


def _argv_of(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [chunk.decode("utf-8", "replace")
            for chunk in raw.split(b"\0") if chunk]


def _has_tokens(argv: list[str], tokens: list[str]) -> bool:
    span = len(tokens)
    if not span or len(argv) < span:
        return False
    for i in range(len(argv) - span + 1):
        if argv[i:i + span] == tokens:
            return True
    return False


def _pid_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    try:
        return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X", "x")
    except IndexError:
        return False


def git(*args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {exc}") from exc
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def git_ok(*args: str) -> bool:
    try:
        git(*args, check=True)
        return True
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def current_commit() -> str:
    try:
        return git("rev-parse", "HEAD")
    except RuntimeError:
        return ""


def branch_head(branch: str) -> str:
    try:
        return git("rev-parse", f"origin/{branch}")
    except RuntimeError:
        return ""


def describe() -> str:
    try:
        return git("describe", "--tags", "--always", "--dirty")
    except RuntimeError:
        return current_commit()[:12]


def notify(text: str) -> None:
    if not WEBHOOK:
        return
    try:
        import urllib.request

        body = json.dumps({"content": text[:1900]}).encode()
        req = urllib.request.Request(
            WEBHOOK, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception: 
        pass


class ChannelState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "channel": "stable",
            "autopull": True,
            "last_good": None,
            "bad_commits": [],
            "pinned": None,
        }
        self.lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self.data.update(stored)
        except (OSError, ValueError):
            pass 

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        with self.lock:
            return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        with self.lock:
            self.data[key] = value
        self.save()


class Supervisor:
    def __init__(self):
        self.state = ChannelState(STATE_PATH)
        self.requests: "queue.Queue[dict]" = queue.Queue()
        self.app: subprocess.Popen | None = None
        self.app_started = 0.0
        self.last_error = ""
        self.stop = False
        self.backoff = 15.0
        self.last_autopull = 0.0
        self.spawn_failed = False

    def request(self, action: str, **kwargs) -> dict:
        slot: dict = {}
        done = threading.Event()
        self.requests.put({"action": action, "kwargs": kwargs,
                           "slot": slot, "done": done})
        done.wait(timeout=180)
        return slot.get("result") or {"ok": False, "error": "timeout"}

    def _drain_requests(self) -> None:
        while True:
            try:
                req = self.requests.get_nowait()
            except queue.Empty:
                return
            try:
                req["slot"]["result"] = self._handle(req)
            except Exception as exc: 
                req["slot"]["result"] = {
                    "ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
            finally:
                req["done"].set()

    def _handle(self, req: dict) -> dict:
        action, kwargs = req["action"], req["kwargs"]
        try:
            if action == "status":
                return self.status()
            if action == "switch":
                return self.switch(str(kwargs.get("channel") or ""))
            if action == "pull":
                return self.pull(force=bool(kwargs.get("force")))
            if action == "revert":
                return self.revert()
            if action == "autopull":
                return self.autopull(bool(kwargs.get("on")))
            if action == "shutdown":
                self.stop = True
                return {"ok": True}
            return {"ok": False, "error": f"unknown action {action!r}"}
        except Exception as exc: 
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self.last_error[:400]}


    def status(self) -> dict:
        channel = self.state.get("channel", "stable")
        branch = CHANNEL_BRANCHES.get(channel, "?")
        app = self.app
        return {
            "ok": True,
            "channel": channel,
            "branch": branch,
            "commit": current_commit(),
            "describe": describe(),
            "source": str(SRC_DIR),
            "pinned": self.state.get("pinned"),
            "autopull": bool(self.state.get("autopull", True)),
            "last_good": self.state.get("last_good"),
            "bad_commits": self.state.get("bad_commits", [])[-10:],
            "app_alive": app is not None and app.poll() is None,
            "app_pid": app.pid if app is not None else None,
            "app_uptime": (time.time() - self.app_started
                           if self.app_started else 0),
            "last_error": self.last_error[:300],
            "channels": CHANNEL_BRANCHES,
        }

    def _reap_strays(self, reason: str) -> int:
        if not os.path.isdir("/proc"):
            return 0
        protected = {os.getpid(), os.getppid(), 1}
        if self.app is not None:
            protected.add(self.app.pid)
        reaped = 0
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in protected:
                continue
            argv = _argv_of(pid)
            if not argv or not _has_tokens(argv, APP_TOKENS):
                continue
            cmdline = " ".join(argv)[:90]
            _log(f"STRAY app process pid {pid} ({cmdline}) - "
                 f"reaping ({reason})")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.6)
                if not _pid_alive(pid):
                    reaped += 1
                    break
            else:
                _log(f"could not reap pid {pid} - check permissions")
        if reaped:
            _log(f"reaped {reaped} stray app process(es) ({reason}) - "
                 "there can be only one")
        return reaped

    def _checkout(self, ref: str) -> str:
        git("fetch", "--all", "--quiet")
        git("checkout", "--quiet", "--force", ref)
        if ref.startswith("origin/"):
            git("reset", "--hard", "--quiet", ref)
        commit = current_commit()
        self._sync_install()
        return commit

    def _sync_install(self) -> None:
        """v9: two jobs now.

        1. dependency sync - when requirements.txt / pyproject.toml
           changed since the last spawn, run a REAL `pip install -r
           requirements.txt` before the app restarts. that's the answer
           to "pulls never rebuild - what if we added a dep?": code-only
           pulls just restart the bot (python needs no build), dep pulls
           install first so the probe judges the new code with its deps
           actually present instead of dying on ImportError and
           auto-reverting.
        2. the editable install - keeps entry points and any metadata
           readers pointed at the git tree (cheap, runs every checkout).
        """
        if not (ROOT / "pyproject.toml").is_file():
            return
        import hashlib

        parts = []
        for name in ("requirements.txt", "pyproject.toml"):
            try:
                parts.append(name + ":"
                             + hashlib.sha256(
                                 (ROOT / name).read_bytes()).hexdigest()[:16])
            except OSError:
                parts.append(name + ":missing")
        signature = "|".join(parts)
        if signature != self.state.get("deps_signature"):
            self.state.set("deps_signature", signature)
            opt_out = (os.getenv("LEECH_PIP_SYNC") or "1").strip().lower() \
                in ("0", "false", "off", "no")
            reqs = ROOT / "requirements.txt"
            if opt_out:
                _log("deps changed but LEECH_PIP_SYNC is off - "
                     "skipping the pip install")
            elif reqs.is_file():
                budget = float(os.getenv("LEECH_PIP_TIMEOUT", "900") or 900)
                _log("dependency files changed - pip install -r "
                     "requirements.txt (first boot after a container "
                     "rebuild this is a no-op sanity pass)")
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-q",
                         "-r", str(reqs)],
                        cwd=str(ROOT), capture_output=True, text=True,
                        timeout=budget,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    _log(f"deps install error ({exc}) - the probe will "
                         "judge the app as-is")
                    proc = None
                if proc is not None:
                    if proc.returncode == 0:
                        _log("deps install ok")
                        notify("leech: deps changed - installed, "
                               "restarting on the new set.")
                    else:
                        tail = (proc.stderr or proc.stdout or "").strip()
                        _log(f"deps install FAILED (code "
                             f"{proc.returncode}): {tail[:300]}")
                        notify("leech: pip install of new deps FAILED - "
                               "the probe will likely revert.")
            else:
                _log("deps changed but there is no requirements.txt - "
                     "nothing to install")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "--no-deps",
                 "--no-build-isolation", "-e", str(ROOT)],
                cwd=str(ROOT), capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"editable install error ({exc}) - continuing "
                 "(imports still come from the git tree)")
            return
        if proc.returncode == 0:
            _log("editable install ok - >version now reads the git tree")
        else:
            _log(f"editable install failed (code {proc.returncode}) - "
                 f"imports still come from the git tree: "
                 f"{proc.stderr.strip()[:160]}")

    def _spawn(self) -> None:
        self._kill_app()
        self._reap_strays(reason="before spawn")
        self.spawn_failed = False
        env = dict(os.environ)
        env.update({
            "LEECH_GIT_CHANNEL": self.state.get("channel", "stable"),
            "LEECH_GIT_COMMIT": current_commit(),
            "LEECH_GIT_DESCRIBE": describe(),
            "LEECH_GIT_SOURCE": str(SRC_DIR),
        })
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SRC_DIR)]
            + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        try:
            self.app = subprocess.Popen(
                START_CMD if SHELL_FALLBACK else START_ARGV,
                cwd=str(ROOT), env=env, shell=SHELL_FALLBACK,
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout, stderr=sys.stderr,
                start_new_session=True, 
            )
        except (OSError, ValueError) as exc:
            self.spawn_failed = True
            self.last_error = f"spawn failed: {type(exc).__name__}: {exc}"
            _log(f"app spawn FAILED: {self.last_error}")
            self.app = None
            return
        self.app_started = time.time()
        _log(f"app spawned (pid {self.app.pid}, {current_commit()[:12]}, "
             f"channel {self.state.get('channel')}) "
             f"- source {SRC_DIR}")

    def _kill_app(self) -> None:
        app, self.app = self.app, None
        if app is None:
            return
        if app.poll() is not None:
            _log(f"app already dead (pid {app.pid}, exit {app.returncode})")
            return
        started = time.time()
        _log(f"stopping app (pid {app.pid})")
        try:
            own_group = os.getpgid(0)
        except (ProcessLookupError, PermissionError, OSError):
            own_group = None
        try:
            group = os.getpgid(app.pid)
        except (ProcessLookupError, PermissionError, OSError):
            group = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if group is not None and group != own_group:
                try:
                    os.killpg(group, sig)  
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                app.send_signal(sig)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
            try:
                app.wait(timeout=12)
                _log(f"app stopped (pid {app.pid}, exit {app.returncode}, "
                     f"{time.time() - started:.1f}s)")
                return
            except subprocess.TimeoutExpired:
                continue
        _log(f"app stop FAILED (pid {app.pid}) - the stray reaper will "
             "catch it before the next spawn")

    def _boot_and_probe(self, note: str) -> dict:
        self._spawn()
        if self.app is None:
            return {"ok": False, "note": note,
                    "error": "app failed to spawn - see supervisor log"}
        deadline = time.time() + PROBE_SECS
        while time.time() < deadline and not self.stop:
            self._drain_requests() 
            if self.app is None or self.app.poll() is not None:
                code = self.app.returncode if self.app else "?"
                bad = current_commit()
                self.last_error = f"app died during probe (exit {code})"
                self._mark_bad(bad)
                last_good = self.state.get("last_good")
                if last_good and last_good != bad:
                    _log(f"{note}: probe FAILED at {bad[:12]} - "
                         f"reverting to last_good {last_good[:12]}")
                    notify(f"leech: {note} probe failed at `{bad[:12]}` - "
                           f"auto-reverted to `{last_good[:12]}`.")
                    self._checkout(last_good)
                    self._spawn()
                    return {"ok": True, "reverted_to": last_good,
                            "bad_commit": bad, "note": note}
                _log(f"{note}: probe FAILED at {bad[:12]} and no last_good "
                     "to fall back to - respawning anyway (crash loop)")
                self._spawn()
                return {"ok": False, "bad_commit": bad,
                        "error": "probe failed, no last_good fallback"}
            time.sleep(1.0)
        if self.stop:
            return {"ok": False, "note": note,
                    "error": "interrupted by shutdown"}
        good = current_commit()
        self.state.set("last_good", good)
        _log(f"{note}: probe passed - {good[:12]} is the new last_good")
        return {"ok": True, "running": good, "note": note}

    def _mark_bad(self, commit: str) -> None:
        if not commit:
            return
        bad = list(self.state.get("bad_commits", []))
        if commit not in bad:
            bad.append(commit)
        self.state.set("bad_commits", bad[-20:])


    def switch(self, channel: str) -> dict:
        channel = channel.strip().lower()
        if channel not in CHANNEL_BRANCHES:
            return {"ok": False,
                    "error": f"channel must be one of "
                             f"{sorted(CHANNEL_BRANCHES)}"}
        self.state.set("channel", channel)
        branch = CHANNEL_BRANCHES[channel]
        head = branch_head(branch)
        if head and head in (self.state.get("bad_commits") or []):
            return {"ok": False,
                    "error": f"head of {branch} is a known-bad commit - "
                             f"fix it or `>channel pull force=on`"}
        _log(f"switching to {channel} (origin/{branch})")
        commit = self._checkout(f"origin/{branch}")
        result = self._boot_and_probe(f"switch to {channel}")
        result["channel"] = channel
        result["branch"] = branch
        result["commit"] = commit
        return result

    def pull(self, force: bool = False) -> dict:
        channel = self.state.get("channel", "stable")
        branch = CHANNEL_BRANCHES.get(channel, "master")
        git("fetch", "--all", "--quiet")
        head = branch_head(branch)
        current = current_commit()
        pinned = self.state.get("pinned")
        if pinned:
            return {"ok": False,
                    "error": f"pinned to {str(pinned)[:12]} - unpin first"}
        if not head:
            return {"ok": False, "error": f"origin/{branch} not found"}
        if head == current:
            return {"ok": True, "up_to_date": True, "commit": current}
        if not force and head in (self.state.get("bad_commits") or []):
            return {"ok": False,
                    "error": f"origin/{branch} head is known-bad "
                             f"{head[:12]} - skipped (force=on overrides)"}
        _log(f"pulling {channel}: {current[:12]} -> {head[:12]}")
        commit = self._checkout(f"origin/{branch}")
        result = self._boot_and_probe(f"pull {channel}")
        result["commit"] = commit
        return result

    def revert(self) -> dict:
        last_good = self.state.get("last_good")
        if not last_good:
            return {"ok": False, "error": "no last_good commit recorded yet"}
        if last_good == current_commit():
            return {"ok": True, "already_there": True, "commit": last_good}
        _log(f"reverting to last_good {last_good[:12]}")
        commit = self._checkout(last_good)
        result = self._boot_and_probe("revert")
        result["commit"] = commit
        notify(f"leech: manual revert to `{last_good[:12]}` (channel "
               f"{self.state.get('channel')}).")
        return result

    def autopull(self, on: bool) -> dict:
        self.state.set("autopull", bool(on))
        return {"ok": True, "autopull": bool(on)}

    def _watch_app(self) -> None:
        if self.app is not None and self.app.poll() is None:
            self.backoff = 15.0
            return
        code = self.app.returncode if self.app is not None else "spawn error"
        self.last_error = f"app exited (code {code})"
        _log(f"app died (code {code}) - respawning in {self.backoff:.0f}s")
        deadline = time.time() + self.backoff
        self.backoff = min(self.backoff * 2, 300.0)
        while time.time() < deadline and not self.stop:
            self._drain_requests()
            time.sleep(1.0)
        if not self.stop:
            self._spawn()

    def _maybe_autopoll(self) -> None:
        if not (self.state.get("autopull", True)
                and time.time() - self.last_autopull >= AUTOPULL_SECS):
            return
        self.last_autopull = time.time()
        try:
            result = self.pull()
        except Exception as exc:              
            self.last_error = f"autopull: {type(exc).__name__}: {exc}"
            _log(f"autopull ERROR (supervisor stays up): "
                 f"{self.last_error[:200]}")
            return
        if result.get("up_to_date"):
            return
        if result.get("ok"):
            _log(f"autopull moved to {str(result.get('commit'))[:12]}")
        else:
            _log(f"autopull skipped: {str(result.get('error'))[:160]}")

    def run(self) -> int:
        _log(f"supervisor up (start={START_CMD!r}, "
             f"channels={CHANNEL_BRANCHES}, state={STATE_PATH}, "
             f"source={SRC_DIR})")
        self._reap_strays(reason="supervisor boot")
        channel = self.state.get("channel", "stable")
        branch = CHANNEL_BRANCHES.get(channel)
        if not branch:
            _log(f"unknown channel {channel!r} in state - using stable")
            self.state.set("channel", "stable")
            branch = CHANNEL_BRANCHES["stable"]

        pinned = self.state.get("pinned")
        try:
            target = pinned or f"origin/{branch}"
            self._checkout(target)
        except RuntimeError as exc:
            _log(f"initial checkout failed ({exc}) - spawning whatever "
                 "the worktree is on")
        result = self._boot_and_probe(f"boot {channel}")
        if not result.get("ok") and self.app is None:
            return 1

        self.last_autopull = time.time()
        while not self.stop:
            self._drain_requests()
            self._watch_app()
            self._maybe_autopoll()
            time.sleep(1.0)

        _log("shutdown requested - stopping the app")
        self._kill_app()
        return 0

    def final_cleanup(self) -> None:
        if self.app is not None and self.app.poll() is None:
            _log("supervisor exiting - stopping the app")
        self._kill_app()


def main() -> int:
    supervisor = Supervisor()

    def _bail(*_args) -> None:
        supervisor.stop = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _bail)
        except (ValueError, OSError): 
            pass
    atexit.register(supervisor.final_cleanup)

    try:
        from control import ControlSidecar 
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "control", HERE / "control.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ControlSidecar = module.ControlSidecar

    sidecar = ControlSidecar(supervisor, port=PORT, token=TOKEN)
    sidecar.start()
    try:
        return supervisor.run()
    except KeyboardInterrupt:
        return 0 


if __name__ == "__main__":
    sys.exit(main())
