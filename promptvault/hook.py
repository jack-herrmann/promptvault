"""Hook entry point, run by Claude Code (and by Cursor, which reuses Claude Code's hooks).

Contract: print nothing, never fail, finish in tens of milliseconds. It keeps a whitelist
of fields, redacts the prompt, drops one spool file, and starts the daemon if needed.
"""

import json
import os
import sys
import time

MAX_STDIN = 20 * 1024 * 1024

CC_EVENTS = {"UserPromptSubmit": "prompt", "Stop": "stop", "StopFailure": "stop_failure", "SessionEnd": "session_end"}
CURSOR_EVENTS = {"beforeSubmitPrompt": "prompt", "stop": "stop", "sessionEnd": "session_end"}

# Apps whose Documents (TCC) permission a spawned daemon may inherit. A daemon started from anything
# else (launchd, tmux) can't write a vault under ~/Documents.
SPAWN_ANCESTORS = (
    "/Applications/iTerm.app/", "/iTerm2/iTermServer", "/System/Applications/Utilities/Terminal.app/",
    "/Applications/Utilities/Terminal.app/", "/Applications/Cursor.app/", "/Applications/Visual Studio Code.app/",
    "/Applications/Aside.app/",
)
SPAWN_LIMIT, SPAWN_WINDOW_S = 3, 600


def _atomic_write(path, data, mode=0o600):
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".tmp-%d-%s" % (os.getpid(), os.urandom(4).hex()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def paused(paths, now=None):
    p = _read_json(paths.PAUSE_PATH)
    if p is None:
        return False
    until = p.get("until") if isinstance(p, dict) else None
    return until is None or until > (now or time.time())


def excluded(paths, cwd):
    cfg = _read_json(paths.CONFIG_PATH, {}) or {}
    for prefix in cfg.get("exclude_cwd_prefixes") or []:
        if cwd and (cwd == prefix or cwd.startswith(prefix.rstrip("/") + "/")):
            return True
    return False


def build_event(payload, env, now):
    """Whitelist the fields we keep. Returns None for events we don't handle."""
    from promptvault.redact import redact

    ev = payload.get("hook_event_name")
    if ev in CURSOR_EVENTS:
        agent, kind = "cursor", CURSOR_EVENTS[ev]
    elif ev in CC_EVENTS:
        agent, kind = "claude-code", CC_EVENTS[ev]
    else:
        return None
    rec = {"v": 1, "agent": agent, "kind": kind, "ts": now, "hook_event": ev}
    if agent == "claude-code":
        for k in ("session_id", "prompt_id", "transcript_path", "cwd", "permission_mode", "session_title", "source"):
            if payload.get(k) is not None:
                rec[k] = payload[k]
        rec["env"] = {
            "attended": env.get("CLAUDE_CODE_SESSION_ATTENDED"),
            "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT"),
            "child": env.get("CLAUDE_CODE_CHILD_SESSION"),
            "term": env.get("TERM_PROGRAM"),
        }
        if kind == "stop":
            rec["reply_chars"] = len(payload.get("last_assistant_message") or "")  # length only, never the text
            rec["stop_hook_active"] = bool(payload.get("stop_hook_active"))
            rec["background_tasks"] = len(payload.get("background_tasks") or [])
        elif kind == "stop_failure":
            rec["error"] = payload.get("error")
        elif kind == "session_end":
            rec["reason"] = payload.get("reason")
    else:
        for k in ("conversation_id", "generation_id", "model", "composer_mode", "cursor_version", "transcript_path",
                  "status", "loop_count"):
            if payload.get(k) is not None:
                rec[k] = payload[k]
        roots = payload.get("workspace_roots") or []
        rec["session_id"] = payload.get("conversation_id") or payload.get("session_id")
        rec["cwd"] = roots[0] if roots else None
        # user_email and attachments are deliberately dropped.
    if kind == "prompt":
        text, n, kinds = redact(payload.get("prompt") or "")
        rec["prompt"] = text
        rec["redactions"] = n
        rec["redaction_kinds"] = kinds
    return rec


def _ancestors():
    import subprocess

    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=2).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    pid, chain = os.getppid(), []
    while pid in table and pid > 1 and len(chain) < 40:
        ppid, comm = table[pid]
        chain.append(comm)
        pid = ppid
    return chain


def spawn_allowed(paths, now):
    if os.environ.get("PROMPTVAULT_ALLOW_SPAWN") == "1":
        return True
    spawns = [t for t in (_read_json(paths.SPAWNS_PATH, []) or []) if now - t < SPAWN_WINDOW_S]
    if len(spawns) >= SPAWN_LIMIT:
        return False  # the daemon keeps dying; `promptvault doctor` reports it
    return any(any(a in c for a in SPAWN_ANCESTORS) for c in _ancestors())


def daemon_running(paths):
    import fcntl

    fd = os.open(paths.LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def spawn_daemon(paths, now):
    import subprocess

    spawns = [t for t in (_read_json(paths.SPAWNS_PATH, []) or []) if now - t < SPAWN_WINDOW_S] + [now]
    _atomic_write(paths.SPAWNS_PATH, json.dumps(spawns).encode())
    env = {"HOME": paths.HOME, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
    env.update(paths.env_overrides())
    subprocess.Popen([paths.PYTHON, "-I", "-S", os.path.join(paths.RUNTIME_DIR, "pvd.py")],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True, cwd=paths.STATE_DIR, env=env)


def main():
    now = time.time()
    status = {"ts": now, "ok": False}
    paths = None
    try:
        if os.environ.get("PROMPTVAULT_SKIP") == "1":
            return
        from promptvault import paths

        os.makedirs(paths.SPOOL_DIR, mode=0o700, exist_ok=True)
        raw = sys.stdin.buffer.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN:
            import hashlib

            rec = {"v": 1, "agent": "unknown", "kind": "oversize", "ts": now, "bytes": len(raw),
                   "sha256": hashlib.sha256(raw).hexdigest()}
        else:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            if paused(paths, now):
                status.update(ok=True, note="paused")
                return
            rec = build_event(payload, os.environ, now)
            if rec is None:
                status.update(ok=True, note="ignored event")
                return
            if excluded(paths, rec.get("cwd")):
                status.update(ok=True, note="excluded cwd")
                return
        name = "%019d-%d-%s.json" % (int(now * 1e9), os.getpid(), os.urandom(3).hex())
        _atomic_write(os.path.join(paths.SPOOL_DIR, name), json.dumps(rec, ensure_ascii=False).encode("utf-8"))
        status.update(ok=True, agent=rec.get("agent"), event=rec.get("kind"))
        if not daemon_running(paths) and spawn_allowed(paths, now):
            spawn_daemon(paths, now)
            status["spawned"] = True
    except Exception as e:  # never let a failure reach the agent
        status["error"] = type(e).__name__
    finally:
        if paths is not None:
            try:
                status["dur_ms"] = round((time.time() - now) * 1000, 1)
                _atomic_write(paths.HOOK_STATUS_PATH, json.dumps(status).encode())
            except Exception:
                pass
