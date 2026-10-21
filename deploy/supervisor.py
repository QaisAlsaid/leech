from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
START_CMD = (os.getenv("LEECH_START_CMD") or "python3 -m leech").strip()
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


def _log(message: str) -> None:
    print(f"[supervisor {time.strftime('%H:%M:%S')}] {message}", flush=True)


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, timeout=120,
    )
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

    def request(self, action: str, **kwargs) -> dict:
        slot: dict = {}
        done = threading.Event()
        self.requests.put({"action": action, "kwargs": kwargs,
                           "slot": slot, "done": done})
        done.wait(timeout=180)
        return slot.get("result") or {"ok": False, "error": "timeout"}

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
        except Exception as exc:  # noqa: BLE001 - sidecar must answer
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self.last_error[:400]}

    def status(self) -> dict:
        channel = self.state.get("channel", "stable")
        branch = CHANNEL_BRANCHES.get(str(channel), "?")
        app = self.app
        return {
            "ok": True,
            "channel": channel,
            "branch": branch,
            "commit": current_commit(),
            "describe": describe(),
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


    def _checkout(self, ref: str) -> str:
        """move the worktree to a ref; returns the landed commit."""
        git("fetch", "--all", "--quiet")
        git("checkout", "--quiet", "--force", ref)
        if ref.startswith("origin/"):
            git("reset", "--hard", "--quiet", ref)
        return current_commit()

    def _spawn(self) -> None:
        self._kill_app()
        env = dict(os.environ)
        env.update({
            "LEECH_GIT_CHANNEL": self.state.get("channel", "stable"),
            "LEECH_GIT_COMMIT": current_commit(),
            "LEECH_GIT_DESCRIBE": describe(),
        })
        self.app = subprocess.Popen(
            START_CMD, cwd=str(ROOT), env=env, shell=True,
            stdout=sys.stdout, stderr=sys.stderr,
        )
        self.app_started = time.time()
        _log(f"app spawned (pid {self.app.pid}, {current_commit()[:12]}, "
             f"channel {self.state.get('channel')})")

    def _kill_app(self) -> None:
        if self.app is None:
            return
        if self.app.poll() is None:
            self.app.terminate()
            try:
                self.app.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.app.kill()
                self.app.wait(timeout=10)
        self.app = None

    def _boot_and_probe(self, note: str) -> dict:
        self._spawn()
        deadline = time.time() + PROBE_SECS
        while time.time() < deadline:
            if self.app is None or self.app.poll() is not None:
                code = self.app.returncode if self.app else "?"
                bad = current_commit()
                self.last_error = f"app died during probe (exit {code})"
                self._mark_bad(bad)
                last_good = self.state.get("last_good")
                if last_good and last_good != bad:
                    _log(f"{note}: probe failed at {bad[:12]} - "
                         f"reverting to last_good {last_good[:12]}")
                    notify(f"leech: {note} probe failed at `{bad[:12]}` - "
                           f"auto-reverted to `{last_good[:12]}`.")
                    self._checkout(last_good)
                    self._spawn()
                    return {"ok": True, "reverted_to": last_good,
                            "bad_commit": bad, "note": note}
                _log(f"{note}: probe failed at {bad[:12]} and no last_good "
                     "to fall back to - respawning anyway (crash loop)")
                self._spawn()
                return {"ok": False, "bad_commit": bad,
                        "error": "probe failed, no last_good fallback"}
            time.sleep(1.0)
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


    def run(self) -> int:
        _log(f"supervisor up (start={START_CMD!r}, "
             f"channels={CHANNEL_BRANCHES}, state={STATE_PATH})")
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

        last_autopull = time.time()
        backoff = 15.0
        while not self.stop:
            try:
                while True:
                    req = self.requests.get_nowait()
                    req["slot"]["result"] = self._handle(req)
                    req["done"].set()
            except queue.Empty:
                pass

            if self.app is not None and self.app.poll() is not None:
                code = self.app.returncode
                self.last_error = f"app exited (code {code})"
                _log(f"app died (code {code}) - respawning in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                self._spawn()
            else:
                backoff = 15.0

            if (self.state.get("autopull", True)
                    and time.time() - last_autopull >= AUTOPULL_SECS):
                last_autopull = time.time()
                result = self.pull()
                if result.get("up_to_date"):
                    pass
                elif result.get("ok"):
                    _log(f"autopull moved to {result.get('commit', '?')[:12]}")
                else:
                    _log(f"autopull skipped: "
                         f"{result.get('error', '?')[:120]}")

            time.sleep(1.0)

        _log("shutdown requested - stopping the app")
        self._kill_app()
        return 0


def main() -> int:
    supervisor = Supervisor()
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
        supervisor._kill_app()
        return 0


if __name__ == "__main__":
    sys.exit(main())
