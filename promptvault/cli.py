"""`promptvault` command line. Shows metadata only; it never prints prompt text."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from promptvault import __version__, install, paths, store


def _daemon_pid():
    from promptvault.hook import daemon_running

    if not os.path.exists(paths.LOCK_PATH) or not daemon_running(paths):
        return None
    try:
        with open(paths.LOCK_PATH) as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def cmd_start(_a):
    if _daemon_pid():
        print("daemon already running (pid %d)" % _daemon_pid())
        return 0
    install.ensure_state()
    env = {"HOME": paths.HOME, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
    env.update(paths.env_overrides())
    subprocess.Popen([paths.PYTHON, "-I", "-S", os.path.join(paths.RUNTIME_DIR, "pvd.py")],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True, cwd=paths.STATE_DIR, env=env)
    for _ in range(30):
        time.sleep(0.1)
        if _daemon_pid():
            print("daemon started (pid %d)" % _daemon_pid())
            return 0
    print("daemon did not start; see %s" % paths.LOG_PATH)
    return 1


def cmd_stop(_a):
    pid = _daemon_pid()
    if not pid:
        print("daemon not running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(50):
        time.sleep(0.1)
        if not _daemon_pid():
            print("daemon stopped")
            return 0
    print("daemon did not stop within 5 s (pid %d)" % pid)
    return 1


def cmd_install(_a):
    install.install_runtime()
    install.ensure_state()
    db = store.connect(paths.DB_PATH)
    if store.get_meta(db, "install_ts") is None:
        store.set_meta(db, "install_ts", time.time())
    if store.get_meta(db, "aside_hwm") is None and os.path.exists(paths.ASIDE_DB):
        # No backfill: start Aside capture after the newest turn that exists right now.
        import sqlite3

        con = sqlite3.connect("file:%s?mode=ro" % paths.ASIDE_DB, uri=True, timeout=2)
        try:
            store.set_meta(db, "aside_hwm", con.execute("SELECT COALESCE(MAX(id),0) FROM session_turns").fetchone()[0])
        finally:
            con.close()
    db.close()
    before, after, bak = install.edit_settings(
        install.add_hooks, lambda b, n: install.has_our_hooks(n) and install.remove_hooks(n) == install.remove_hooks(b))
    print("runtime: %s" % paths.RUNTIME_DIR)
    print("cli:     %s" % paths.CLI_SHIM)
    if bak:
        import difflib

        print("settings: added PROMPTVAULT hooks (%s); backup %s" % (", ".join(install.EVENTS), bak))
        diff = difflib.unified_diff(install._dump(before).splitlines(), install._dump(after).splitlines(),
                                    "settings.json (before)", "settings.json (after)", lineterm="", n=1)
        print("\n".join(diff))
    else:
        print("settings: PROMPTVAULT hooks already present")
    if _daemon_pid():
        cmd_stop(None)  # restart onto the freshly installed code
    return cmd_start(None)


def cmd_uninstall(_a):
    # Order matters: hooks first, so nothing restarts the daemon halfway through.
    before, after, bak = install.edit_settings(
        install.remove_hooks, lambda b, n: not install.has_our_hooks(n) and n == install.remove_hooks(b))
    print("settings: PROMPTVAULT hooks removed" + (" (backup %s)" % bak if bak else " (none were present)"))
    cmd_stop(None)
    shutil.rmtree(paths.RUNTIME_DIR, ignore_errors=True)
    shutil.rmtree(paths.STATE_DIR, ignore_errors=True)
    if os.path.exists(paths.CLI_SHIM):
        os.remove(paths.CLI_SHIM)
    print("removed runtime, state (incl. spool) and CLI shim; vault and database kept in %s" % paths.PROJECT_DIR)
    return 0


def _ago(ts):
    if not ts:
        return "never"
    d = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if d >= n:
            return "%d %s ago" % (d // n, unit)
    return "%d s ago" % d


def cmd_doctor(_a):
    ok = True

    def line(good, msg, warn=False):
        nonlocal ok
        mark = "✓" if good else ("!" if warn else "✗")
        if not good and not warn:
            ok = False
        print(" %s %s" % (mark, msg))

    print("PROMPTVAULT %s" % __version__)
    line(os.path.exists(os.path.join(paths.RUNTIME_DIR, "pv_hook.py")), "runtime installed at %s" % paths.RUNTIME_DIR)
    line(os.access(paths.PYTHON, os.X_OK), "python %s" % paths.PYTHON)
    try:
        settings = json.load(open(paths.CC_SETTINGS))
    except (OSError, ValueError):
        settings = {}
    line(install.has_our_hooks(settings), "hooks in %s (%s)" % (paths.CC_SETTINGS, ", ".join(install.EVENTS)))
    st = install._read_settings(paths.CC_SETTINGS)[0].get("disableAllHooks")
    line(not st, "disableAllHooks is not set" if not st else "disableAllHooks is TRUE: nothing is captured")
    try:
        hs = json.load(open(paths.HOOK_STATUS_PATH))
        line(hs.get("ok") and not hs.get("error"), "last hook run %s (%s %s, %s ms)%s" % (
            _ago(hs.get("ts")), hs.get("agent", "-"), hs.get("event", hs.get("note", "-")), hs.get("dur_ms"),
            (" error: " + hs["error"]) if hs.get("error") else ""))
    except (OSError, ValueError):
        line(False, "no hook has run yet (send a prompt in Claude Code)", warn=True)
    pid = _daemon_pid()
    line(bool(pid), "daemon running (pid %s)" % pid if pid else "daemon not running (`promptvault start`)")
    spool = [n for n in os.listdir(paths.SPOOL_DIR) if n.endswith(".json")] if os.path.isdir(paths.SPOOL_DIR) else []
    failed = os.listdir(paths.FAILED_DIR) if os.path.isdir(paths.FAILED_DIR) else []
    line(len(spool) < 20, "spool backlog: %d" % len(spool), warn=True)
    line(not failed, "failed events: %d%s" % (len(failed), " (`promptvault retry-failed`)" if failed else ""))
    spawns = json.load(open(paths.SPAWNS_PATH)) if os.path.exists(paths.SPAWNS_PATH) else []
    recent = [t for t in spawns if time.time() - t < 600]
    line(len(recent) < 3, "daemon restarts in the last 10 min: %d" % len(recent))
    if os.path.exists(paths.PAUSE_PATH):
        p = json.load(open(paths.PAUSE_PATH))
        line(False, "capture PAUSED %s" % ("until " + time.ctime(p["until"]) if p.get("until") else "until `resume`"),
             warn=True)
    if os.path.exists(paths.DB_PATH):
        db = store.connect(paths.DB_PATH)
        for r in db.execute("SELECT s.agent, MAX(p.ts) AS t, SUM(p.origin_class='human') AS yours, "
                            "SUM(p.origin_class='agent') AS agentn, SUM(p.origin_class='system') AS sys "
                            "FROM prompts p JOIN sessions s ON s.sid=p.sid GROUP BY s.agent"):
            print("   %s: %d yours, %d agent-written, %d system turns; last event %s" % (
                r["agent"], r["yours"] or 0, r["agentn"] or 0, r["sys"] or 0, _ago(r["t"])))
        dis = store.get_meta(db, "aside_disabled")
        line(not dis, "Aside capture on" if not dis else "Aside capture OFF, schema changed: %s" % dis.get("missing"))
        rc = store.get_meta(db, "reconciled_count", 0) or 0
        line(rc == 0, "prompts recovered from history.jsonl (hook missed them): %d" % rc, warn=True)
        ov = store.get_meta(db, "oversize_events", 0) or 0
        line(ov == 0, "oversize (>20 MB) prompts skipped: %d" % ov, warn=True)
        pend = db.execute("SELECT COUNT(*) FROM prompts WHERE origin_class='pending'").fetchone()[0]
        line(pend < 5, "prompts awaiting classification: %d" % pend, warn=True)
        db.close()
    try:
        os.makedirs(paths.VAULT_DIR, exist_ok=True)
        probe = os.path.join(paths.VAULT_DIR, ".pv-doctor-probe")
        open(probe, "w").close()
        os.remove(probe)
        line(True, "vault writable: %s" % paths.VAULT_DIR)
    except OSError as e:
        line(False, "vault NOT writable (%s): %s" % (type(e).__name__, paths.VAULT_DIR))
    return 0 if ok else 1


def cmd_status(a):
    pid = _daemon_pid()
    print("daemon: %s" % ("running (pid %d)" % pid if pid else "not running"))
    return 0


def _parse_duration(s):
    if s in (None, "indef", "forever"):
        return None
    units = {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}
    num = "".join(ch for ch in s if ch.isdigit())
    unit = s[len(num):] or "m"
    if not num or unit not in units:
        raise SystemExit("duration like 30m, 2h, 1d or indef")
    return int(num) * units[unit]


def _close_pause(now):
    if not os.path.exists(paths.PAUSE_PATH):
        return
    try:
        p = json.load(open(paths.PAUSE_PATH))
    except (OSError, ValueError):
        p = {}
    log_path = paths.STATE_DIR + "/pause_log.json"
    try:
        spans = json.load(open(log_path))
    except (OSError, ValueError):
        spans = []
    until = p.get("until")
    spans.append([p.get("since") or now, min(until, now) if until else now])
    with open(log_path, "w") as f:
        json.dump(spans[-200:], f)
    os.remove(paths.PAUSE_PATH)


def cmd_pause(a):
    now = time.time()
    _close_pause(now)
    secs = _parse_duration(a.duration)
    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    with open(paths.PAUSE_PATH, "w") as f:
        json.dump({"since": now, "until": (now + secs) if secs else None}, f)
    print("capture paused " + ("for %s" % a.duration if secs else "until `promptvault resume`"))
    return 0


def cmd_resume(_a):
    _close_pause(time.time())
    print("capture resumed")
    return 0


def cmd_rebuild(a):
    db = store.connect(paths.DB_PATH)
    if a.session:
        n = db.execute("UPDATE sessions SET dirty=1 WHERE session_id LIKE ? OR sid LIKE ?",
                       (a.session + "%", "%:" + a.session + "%")).rowcount
    else:
        n = db.execute("UPDATE sessions SET dirty=1").rowcount
    db.close()
    print("%d session(s) queued for re-render (the daemon picks them up within a second)" % n)
    return 0


def cmd_forget(a):
    """Delete sessions (DB rows and note) whose session id starts with the given prefix."""
    if len(a.session) < 6:
        raise SystemExit("give at least 6 characters of the session id")
    db = store.connect(paths.DB_PATH)
    rows = db.execute("SELECT sid, note_path FROM sessions WHERE session_id LIKE ?", (a.session + "%",)).fetchall()
    for r in rows:
        db.execute("DELETE FROM prompts WHERE sid=?", (r["sid"],))
        db.execute("DELETE FROM sessions WHERE sid=?", (r["sid"],))
        if r["note_path"]:
            note = os.path.join(paths.VAULT_DIR, r["note_path"])
            if os.path.exists(note):
                os.remove(note)
            d = os.path.dirname(note)
            while d != paths.VAULT_DIR and d.startswith(paths.VAULT_DIR) and not os.listdir(d):
                os.rmdir(d)  # drop month/agent folders the note leaves empty
                d = os.path.dirname(d)
    store.set_meta(db, "home_dirty", True)
    db.close()
    print("forgot %d session(s); Home updates within a minute" % len(rows))
    return 0


def cmd_retry_failed(_a):
    if not os.path.isdir(paths.FAILED_DIR):
        print("no failed events")
        return 0
    names = os.listdir(paths.FAILED_DIR)
    for n in names:
        os.replace(os.path.join(paths.FAILED_DIR, n), os.path.join(paths.SPOOL_DIR, n))
    print("requeued %d event(s)" % len(names))
    return 0


def cmd_tail(a):
    db = store.connect(paths.DB_PATH)
    rows = db.execute("SELECT p.ts, s.agent, p.origin_class, p.origin_kind, p.prompt_source, p.status, p.chars, "
                      "p.redactions, p.files_changed, p.tool_calls, p.likely_correction, s.note_path, p.capture_source "
                      "FROM prompts p JOIN sessions s ON s.sid=p.sid ORDER BY p.ts DESC LIMIT ?", (a.n,)).fetchall()
    for r in reversed(rows):
        files = len(store.jload(r["files_changed"], []) or [])
        print("%s  %-11s %-6s %-17s %-13s %5d ch  tools=%s files=%d%s%s  %s" % (
            time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"])), r["agent"], r["origin_class"],
            (r["origin_kind"] or "") + ("/" + r["prompt_source"] if r["prompt_source"] else ""), r["status"],
            r["chars"] or 0, r["tool_calls"] or 0, files, " corr" if r["likely_correction"] else "",
            " red=%d" % r["redactions"] if r["redactions"] else "", r["note_path"] or "(not rendered)"))
    db.close()
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="promptvault", description="Capture every prompt into an Obsidian vault.")
    sub = ap.add_subparsers(dest="cmd")
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall), ("start", cmd_start),
                     ("stop", cmd_stop), ("status", cmd_status), ("doctor", cmd_doctor), ("resume", cmd_resume),
                     ("retry-failed", cmd_retry_failed)):
        sub.add_parser(name).set_defaults(fn=fn)
    p = sub.add_parser("pause")
    p.add_argument("duration", nargs="?", default="indef", help="30m, 2h, 1d or indef")
    p.set_defaults(fn=cmd_pause)
    p = sub.add_parser("rebuild")
    p.add_argument("session", nargs="?")
    p.set_defaults(fn=cmd_rebuild)
    p = sub.add_parser("forget")
    p.add_argument("session", help="session id prefix (>= 6 chars)")
    p.set_defaults(fn=cmd_forget)
    p = sub.add_parser("tail")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_tail)
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    return a.fn(a) or 0
