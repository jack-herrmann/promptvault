"""promptvaultd: the single writer. Drains the hook spool, watches Aside, reconciles against
Claude Code's history.jsonl, classifies prompts, tracks outcomes and renders the vault.

It never runs an agent and never logs prompt text.
"""

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import time

from promptvault import classify, paths, render, store
from promptvault.redact import redact
from promptvault.transcript import TranscriptCache

TICK_S = 1.0
ASIDE_EVERY_S = 2.0
HISTORY_EVERY_S = 60.0
HOME_EVERY_S = 60.0
RUNNING_CHECK_EVERY_S = 5.0
RESOLVE_S, RESOLVE_QUEUED_S = 10.0, 120.0
HISTORY_GRACE_S, HISTORY_MATCH_S = 120.0, 300.0
MAX_FAILS = 5
STALE_RUNNING_S = 6 * 3600

ASIDE_REQUIRED = {
    "session_turns": {"id", "session_id", "user_message", "steering_messages", "started_at", "finished_at",
                      "aborted_at", "abort_reason", "files_changed"},
    "sessions": {"id", "trigger", "incognito", "title", "model", "created_at"},
}
DEFAULT_CONFIG = {"exclude_cwd_prefixes": [], "agent_cwd_patterns": [], "agent_text_prefixes": []}


def h12(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def project_name(cwd):
    if not cwd:
        return None
    if os.path.realpath(cwd) == os.path.realpath(paths.HOME):
        return "~"
    d = cwd
    for _ in range(12):
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d) or d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.basename(cwd.rstrip("/")) or cwd


class Log:
    def __init__(self, path, cap=1_000_000):
        self.path, self.cap = path, cap

    def __call__(self, msg, **kv):
        line = "%s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg, json.dumps(kv, default=str) if kv else "")
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.cap:
                for i in (2, 1):
                    if os.path.exists("%s.%d" % (self.path, i)):
                        os.replace("%s.%d" % (self.path, i), "%s.%d" % (self.path, i + 1))
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a") as f:
                f.write(line)
        except OSError:
            pass


class Daemon:
    def __init__(self, db_path=None, log=None):
        self.log = log or Log(paths.LOG_PATH)
        self.db = store.connect(db_path or paths.DB_PATH)
        self.tc = TranscriptCache()
        self.fails = {}
        self.stop = False
        self.next = {"aside": 0.0, "history": 0.0, "home": 0.0, "running": 0.0}
        self.home_dirty = True
        self.history_recent = []  # dicts(session, ts, prefix, text, matched)
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(_read_json(paths.CONFIG_PATH, {}) or {})
        if store.get_meta(self.db, "install_ts") is None:
            store.set_meta(self.db, "install_ts", time.time())
        self.install_ts = store.get_meta(self.db, "install_ts")

    # ---------------------------------------------------------------- main loop
    def run_forever(self):
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        self.log("daemon start", pid=os.getpid())
        while not self.stop:
            if os.path.exists(paths.STOP_REQUEST_PATH):
                try:
                    os.remove(paths.STOP_REQUEST_PATH)
                except OSError:
                    pass
                break
            try:
                self.tick(time.time())
            except Exception as e:  # keep running; details without prompt text
                self.log("tick error", error=type(e).__name__, detail=str(e)[:200])
            time.sleep(TICK_S)
        self.log("daemon stop", pid=os.getpid())

    def tick(self, now):
        self.drain_spool(now)
        self.resolve_pending(now)
        self.scan_outcomes(now)
        if now >= self.next["running"]:
            self.check_running(now)
            self.next["running"] = now + RUNNING_CHECK_EVERY_S
        if now >= self.next["aside"]:
            self.poll_aside(now)
            self.next["aside"] = now + ASIDE_EVERY_S
        if now >= self.next["history"]:
            self.reconcile_history(now)
            self.next["history"] = now + HISTORY_EVERY_S
        self.render_dirty(now)
        if store.get_meta(self.db, "home_dirty"):  # set by `promptvault forget`
            store.set_meta(self.db, "home_dirty", False)
            self.home_dirty = True
        if self.home_dirty and now >= self.next["home"]:
            self.render_home(now)
            self.next["home"] = now + HOME_EVERY_S

    def txn(self):
        return _Txn(self.db)

    # ---------------------------------------------------------------- spool
    def drain_spool(self, now):
        try:
            names = sorted(n for n in os.listdir(paths.SPOOL_DIR) if n.endswith(".json") and not n.startswith("."))
        except OSError:
            return
        for name in names[:500]:
            path = os.path.join(paths.SPOOL_DIR, name)
            if store.event_seen(self.db, name):
                _unlink(path)
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
                with self.txn():
                    self.handle(rec, now)
                    store.mark_event(self.db, name, rec.get("ts"), rec.get("agent"), rec.get("kind"))
            except Exception as e:
                n = self.fails.get(name, 0) + 1
                self.fails[name] = n
                self.log("event failed", event=name, attempt=n, error=type(e).__name__, detail=str(e)[:200])
                if n >= MAX_FAILS:
                    os.makedirs(paths.FAILED_DIR, mode=0o700, exist_ok=True)
                    try:
                        os.replace(path, os.path.join(paths.FAILED_DIR, name))
                    except OSError:
                        pass
                    self.fails.pop(name, None)
                continue
            _unlink(path)

    def handle(self, rec, now):
        agent, kind = rec.get("agent"), rec.get("kind")
        if kind == "oversize":
            store.set_meta(self.db, "oversize_events", (store.get_meta(self.db, "oversize_events", 0) or 0) + 1)
            return
        if agent == "claude-code":
            {"prompt": self.cc_prompt, "stop": self.cc_stop, "stop_failure": self.cc_stop,
             "session_end": self.cc_session_end}.get(kind, _noop)(rec, now)
        elif agent == "cursor":
            {"prompt": self.cursor_prompt, "stop": self.cursor_stop,
             "session_end": self.cursor_session_end}.get(kind, _noop)(rec, now)

    def agent_cwd(self, cwd):
        return bool(cwd) and any(p in cwd for p in self.config.get("agent_cwd_patterns") or [])

    # ---------------------------------------------------------------- Claude Code
    def cc_prompt(self, rec, now):
        if not rec.get("session_id"):
            return
        sid = "claude-code:" + rec["session_id"]
        env = rec.get("env") or {}
        unattended = classify.claude_code_unattended(env)
        cwd = rec.get("cwd")
        store.upsert_session(self.db, sid, agent="claude-code", session_id=rec["session_id"], cwd=cwd,
                             project=project_name(cwd), source="unattended" if unattended else "interactive",
                             attended=0 if env.get("attended") == "0" else (1 if env.get("attended") == "1" else None),
                             started=rec["ts"], last_prompt=rec["ts"], transcript_path=rec.get("transcript_path"),
                             title=rec.get("session_title"), dirty=1)
        text = rec.get("prompt") or ""
        native = rec.get("prompt_id") or ("ts-%d" % int(rec["ts"] * 1000))
        pid = "%s:%s:%s" % (sid, native, h12(text))
        if store.get_prompt(self.db, pid):
            return
        queued = self.db.execute("SELECT 1 FROM prompts WHERE sid=? AND native_id=? LIMIT 1",
                                 (sid, native)).fetchone() is not None
        cls, kind = "pending", None
        if unattended:
            cls, kind = classify.AGENT, ("sdk" if env.get("entrypoint") == "sdk-cli" else "unattended")
        elif self.agent_cwd(cwd):
            cls, kind = classify.AGENT, "agent-folder"
        store.insert_prompt(
            self.db, pid=pid, sid=sid, seq=store.next_seq(self.db, sid), ts=rec["ts"], native_id=native, text=text,
            chars=len(text), redactions=rec.get("redactions") or 0, redaction_kinds=store.jdump(rec.get("redaction_kinds")),
            origin_class=cls, origin_kind=kind, prompt_source="queued" if queued else None,
            status="merged" if queued else "running", capture_source="hook",
            flags=store.jdump(["empty-hook-text"] if not text else []),
            resolve_deadline=rec["ts"] + (RESOLVE_QUEUED_S if queued else RESOLVE_S))
        if cls != "pending":
            self.after_classified(store.get_prompt(self.db, pid))
        store.update_session(self.db, sid, dirty=1)

    def _stop_like(self, sid, native, rec, status):
        rows = self.db.execute("SELECT * FROM prompts WHERE sid=? AND native_id=?", (sid, native)).fetchall()
        for p in rows:
            fields = {"ended": rec["ts"], "duration_s": max(0.0, rec["ts"] - p["ts"]),
                      "outcome_scan_at": rec["ts"] + 3}
            if p["status"] in ("running", None, "ended-unknown"):
                fields["status"] = status
            if rec.get("reply_chars") is not None:
                fields["reply_chars"] = rec["reply_chars"]
            if rec.get("error"):
                extra = store.jload(p["extra"], {}) or {}
                extra["error"] = rec["error"]
                fields["extra"] = store.jdump(extra)
            store.update_prompt(self.db, p["pid"], **fields)
        if rows:
            store.update_session(self.db, sid, dirty=1)

    def cc_stop(self, rec, now):
        if rec.get("session_id") and rec.get("prompt_id"):
            self._stop_like("claude-code:" + rec["session_id"], rec["prompt_id"], rec,
                            "error" if rec.get("kind") == "stop_failure" else "completed")

    def cc_session_end(self, rec, now):
        if not rec.get("session_id"):
            return
        sid = "claude-code:" + rec["session_id"]
        if store.get_session(self.db, sid) is None:
            return
        store.update_session(self.db, sid, ended=rec["ts"], end_reason=rec.get("reason"), dirty=1)
        self.db.execute("UPDATE prompts SET outcome_scan_at=? WHERE sid=? AND status='running'", (rec["ts"] + 2, sid))

    # ---------------------------------------------------------------- Cursor
    def cursor_prompt(self, rec, now):
        if not rec.get("session_id"):
            return
        sid = "cursor:" + rec["session_id"]
        cwd = rec.get("cwd")
        store.upsert_session(self.db, sid, agent="cursor", session_id=rec["session_id"], cwd=cwd,
                             project=project_name(cwd), model=rec.get("model"), source=rec.get("composer_mode") or "cursor",
                             attended=1, started=rec["ts"], last_prompt=rec["ts"],
                             transcript_path=rec.get("transcript_path"), dirty=1)
        text = rec.get("prompt") or ""
        native = rec.get("generation_id") or ("ts-%d" % int(rec["ts"] * 1000))
        pid = "%s:%s:%s" % (sid, native, h12(text))
        if store.get_prompt(self.db, pid):
            return
        store.insert_prompt(
            self.db, pid=pid, sid=sid, seq=store.next_seq(self.db, sid), ts=rec["ts"], native_id=native, text=text,
            chars=len(text), redactions=rec.get("redactions") or 0, redaction_kinds=store.jdump(rec.get("redaction_kinds")),
            origin_class=classify.HUMAN, origin_kind="cursor", surface=rec.get("composer_mode") or "unknown",
            status="running", capture_source="hook", flags=store.jdump([]))
        self.after_classified(store.get_prompt(self.db, pid))

    def cursor_stop(self, rec, now):
        if not rec.get("session_id"):
            return
        sid = "cursor:" + rec["session_id"]
        status = {"completed": "completed", "aborted": "interrupted", "error": "error"}.get(rec.get("status"), "completed")
        native = rec.get("generation_id")
        if native and self.db.execute("SELECT 1 FROM prompts WHERE sid=? AND native_id=?", (sid, native)).fetchone():
            self._stop_like(sid, native, rec, status)
            return
        row = self.db.execute("SELECT native_id FROM prompts WHERE sid=? AND status='running' ORDER BY seq DESC LIMIT 1",
                              (sid,)).fetchone()
        if row:
            self._stop_like(sid, row["native_id"], rec, status)

    def cursor_session_end(self, rec, now):
        if rec.get("session_id"):
            sid = "cursor:" + rec["session_id"]
            if store.get_session(self.db, sid):
                store.update_session(self.db, sid, ended=rec["ts"], end_reason="session-end", dirty=1)
                self.db.execute("UPDATE prompts SET status='ended-unknown' WHERE sid=? AND status='running'", (sid,))

    # ---------------------------------------------------------------- classification
    def resolve_pending(self, now):
        rows = self.db.execute("SELECT p.*, s.transcript_path FROM prompts p JOIN sessions s ON s.sid=p.sid "
                               "WHERE p.origin_class='pending' ORDER BY p.ts LIMIT 200").fetchall()
        for p in rows:
            with self.txn():
                self._resolve_one(p, now)

    def _resolve_one(self, p, now):
        idx = self.tc.get(p["transcript_path"])
        cls = kind = None
        fields = {}
        text = p["text"] or ""
        if idx is not None:
            if p["prompt_source"] == "queued":
                cls, kind = classify.from_queued(idx.find_queued(text, transform=lambda t: redact(t)[0]))
            else:
                info = idx.prompts.get(p["native_id"])
                cls, kind = classify.from_transcript(info)
                if info:
                    fields.update(turn_origin=info.get("turn_origin"), prompt_source=info.get("prompt_source"))
                    if not text and info.get("text"):
                        t, n, kinds = redact(info["text"])
                        fields.update(text=t, chars=len(t), redactions=n, redaction_kinds=store.jdump(kinds))
                        text = t
        if cls is None and self._history_says_typed(p, text):
            cls, kind = classify.HUMAN, "history"
        if cls is None:
            cls, kind = classify.from_text(text, self.config.get("agent_text_prefixes") or ())
        if cls is None and now >= (p["resolve_deadline"] or 0):
            cls, kind = classify.HUMAN, "unknown"
        if cls is None:
            return
        fields.update(origin_class=cls, origin_kind=kind or fields.get("turn_origin"))
        if cls == classify.SYSTEM:
            fields.update(text=None)  # harness output (task notifications etc.): keep only the turn boundary
        store.update_prompt(self.db, p["pid"], **fields)
        store.update_session(self.db, p["sid"], dirty=1)
        self.after_classified(store.get_prompt(self.db, p["pid"]))

    def _history_says_typed(self, p, text):
        sess = p["sid"].split(":", 1)[1]
        for h in self.history_recent:
            if h["session"] == sess and abs(h["ts"] - p["ts"]) < HISTORY_MATCH_S and _text_matches(h, text):
                return True
        return False

    def after_classified(self, p):
        """Flag likely corrections and keep the session's last_prompt current."""
        if p is None or p["origin_class"] != classify.HUMAN:
            return
        # A correction answers the previous *turn*, so skip prompts merged into a running turn. If an
        # agent or system message started that turn, the correction is about that turn, not your prompt.
        prev = self.db.execute(
            "SELECT * FROM prompts WHERE sid=? AND seq<? AND "
            "COALESCE(prompt_source,'') NOT IN ('queued','steering') ORDER BY seq DESC LIMIT 1",
            (p["sid"], p["seq"])).fetchone()
        if prev and prev["origin_class"] == classify.HUMAN and \
                p["ts"] - prev["ts"] <= classify.CORRECTION_WINDOW_S and classify.looks_like_correction(p["text"]):
            store.update_prompt(self.db, prev["pid"], likely_correction=1)
            flags = store.jload(p["flags"], []) or []
            if "correction" not in flags:
                store.update_prompt(self.db, p["pid"], flags=store.jdump(flags + ["correction"]))
        self.home_dirty = True

    # ---------------------------------------------------------------- outcomes (Claude Code transcripts)
    def scan_outcomes(self, now):
        rows = self.db.execute("SELECT p.*, s.transcript_path, s.ended AS s_ended FROM prompts p JOIN sessions s "
                               "ON s.sid=p.sid WHERE p.outcome_scan_at IS NOT NULL AND p.outcome_scan_at<=? LIMIT 200",
                               (now,)).fetchall()
        for p in rows:
            with self.txn():
                fields = {"outcome_scan_at": None}
                idx = self.tc.get(p["transcript_path"]) if p["sid"].startswith("claude-code:") else None
                if idx is not None:
                    # Tool calls and files belong to the turn's main prompt, not to prompts merged into it.
                    if p["prompt_source"] != "queued":
                        files = idx.files.get(p["native_id"])
                        if files:
                            fields["files_changed"] = store.jdump(files)
                        if idx.tool_results.get(p["native_id"]):
                            fields["tool_calls"] = idx.tool_results[p["native_id"]]
                    if idx.model:
                        store.update_session(self.db, p["sid"], model=idx.model)
                    if p["status"] == "running" and p["native_id"] in idx.interrupted:
                        fields["status"] = "interrupted"
                extra = store.jload(p["extra"], {}) or {}
                scans = extra.get("scans", 0) + 1
                extra["scans"] = scans
                fields["extra"] = store.jdump(extra)
                if p["status"] == "running" and "status" not in fields and p["s_ended"]:
                    fields["status"] = "ended-unknown"
                if scans < 2 and p["status"] in ("completed", "error", "merged"):
                    fields["outcome_scan_at"] = now + 30  # one late rescan: transcripts can lag the Stop hook
                store.update_prompt(self.db, p["pid"], **fields)
                store.update_session(self.db, p["sid"], dirty=1)

    def check_running(self, now):
        rows = self.db.execute("SELECT p.*, s.transcript_path, s.ended AS s_ended FROM prompts p JOIN sessions s "
                               "ON s.sid=p.sid WHERE p.status='running' AND p.sid LIKE 'claude-code:%' AND p.ts<?",
                               (now - 3,)).fetchall()
        for p in rows:
            # Esc doesn't fire Stop; the transcript's interrupt marker (with this promptId) is the evidence.
            idx = self.tc.get(p["transcript_path"])
            status = None
            if idx is not None and p["native_id"] in idx.interrupted:
                status = "interrupted"
            elif p["s_ended"] or now - p["ts"] > STALE_RUNNING_S:
                status = "ended-unknown"
            if status:
                with self.txn():
                    store.update_prompt(self.db, p["pid"], status=status)
                    store.update_session(self.db, p["sid"], dirty=1)

    # ---------------------------------------------------------------- Aside
    def poll_aside(self, now):
        if store.get_meta(self.db, "aside_disabled"):
            return
        if not os.path.exists(paths.ASIDE_DB):
            return
        try:
            con = sqlite3.connect("file:%s?mode=ro" % paths.ASIDE_DB, uri=True, timeout=2)
        except sqlite3.Error:
            return
        try:
            con.row_factory = sqlite3.Row
            if not store.get_meta(self.db, "aside_schema_ok"):
                missing = []
                for table, cols in ASIDE_REQUIRED.items():
                    have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
                    missing += ["%s.%s" % (table, c) for c in sorted(cols - have)]
                if missing:
                    store.set_meta(self.db, "aside_disabled", {"missing": missing, "ts": now})
                    self.log("aside capture disabled: schema changed", missing=missing)
                    return
                store.set_meta(self.db, "aside_schema_ok", True)
            hwm = store.get_meta(self.db, "aside_hwm")
            if hwm is None:
                hwm = con.execute("SELECT COALESCE(MAX(id),0) FROM session_turns").fetchone()[0]
                store.set_meta(self.db, "aside_hwm", hwm)  # no backfill
                return
            pending = store.get_meta(self.db, "aside_pending", []) or []
            marks = ",".join("?" * len(pending)) or "NULL"
            rows = con.execute(
                "SELECT t.id, t.session_id, t.user_message, t.steering_messages, t.started_at, t.finished_at, "
                "t.aborted_at, t.abort_reason, t.files_changed, s.trigger, s.incognito, s.title, s.model, s.created_at "
                "FROM session_turns t JOIN sessions s ON s.id=t.session_id "
                "WHERE t.id>? OR t.id IN (%s) ORDER BY t.id LIMIT 300" % marks, [hwm] + pending).fetchall()
        except sqlite3.Error as e:
            self.log("aside read error", error=type(e).__name__)
            return
        finally:
            con.close()  # never hold a read transaction open on Aside's database
        is_paused = _paused(now)
        new_pending = set(pending)
        new_hwm = hwm
        with self.txn():
            for r in rows:
                fresh = r["id"] > hwm
                if fresh:
                    new_hwm = max(new_hwm, r["id"])
                if r["incognito"]:
                    new_pending.discard(r["id"])
                    continue  # incognito: never stored
                if fresh and is_paused:
                    continue  # paused: skipped for good, not caught up later
                if self.aside_turn(r, now):
                    new_pending.add(r["id"])
                else:
                    new_pending.discard(r["id"])
            store.set_meta(self.db, "aside_hwm", new_hwm)
            store.set_meta(self.db, "aside_pending", sorted(new_pending)[-500:])

    def aside_turn(self, r, now):
        """Store/refresh one Aside turn. Returns True while the turn is still running."""
        cls, kind = classify.aside_session(r["trigger"])
        sid = "aside:" + r["session_id"]
        try:
            model = (json.loads(r["model"]) or {}).get("modelId") if r["model"] else None
        except (ValueError, AttributeError):
            model = None
        try:
            trig_source = (json.loads(r["trigger"]) or {}).get("source") if r["trigger"] else None
        except (ValueError, AttributeError):
            trig_source = None
        prefixes = self.config.get("agent_text_prefixes") or ()
        groups = {}  # (class, kind) -> texts; agents can post into a chat you started
        for m in _json_list(r["user_message"]):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    t = c
                elif isinstance(c, list):
                    t = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                else:
                    continue
                key = (cls, kind) if cls != classify.HUMAN else (classify.aside_message(c, t, prefixes) or (cls, kind))
                groups.setdefault(key, []).append(t)
        abort = _json_obj(r["abort_reason"])
        if abort.get("kind") == "aborted-by-user":
            status = "interrupted"
        elif abort.get("kind"):
            status = "error"
        elif r["finished_at"]:
            status = "completed"
        else:
            status = "running"
        ended = r["aborted_at"] or r["finished_at"]
        files = [f.get("path") for f in _json_list(r["files_changed"]) if isinstance(f, dict) and f.get("path")]
        bodies = [(k, "\n\n".join(t for t in ts if t.strip())) for k, ts in groups.items()]
        bodies = [(k, b) for k, b in bodies if b]
        steering = [m for m in _json_list(r["steering_messages"]) if isinstance(m, dict) and m.get("content")]
        if not bodies and not steering:
            return status == "running"  # a system/agent turn without your text
        store.upsert_session(self.db, sid, agent="aside", session_id=r["session_id"], project="Aside",
                             title=r["title"], model=model, source=trig_source or kind, attended=1,
                             started=r["created_at"] or r["started_at"], last_prompt=r["started_at"], dirty=1)
        for (pcls, pkind), body in bodies:
            text, n, kinds = redact(body)
            pid = "%s:%s:%s" % (sid, r["id"], h12(text))
            fields = dict(status=status, ended=ended,
                          duration_s=(ended - r["started_at"]) if ended and r["started_at"] else None,
                          files_changed=store.jdump(files) if files else None)
            if store.get_prompt(self.db, pid) is None:
                store.insert_prompt(
                    self.db, pid=pid, sid=sid, seq=store.next_seq(self.db, sid), ts=r["started_at"] or now,
                    native_id=str(r["id"]), text=text, chars=len(text), redactions=n,
                    redaction_kinds=store.jdump(kinds), origin_class=pcls, origin_kind=pkind, capture_source="aside",
                    flags=store.jdump([]), **fields)
                self.after_classified(store.get_prompt(self.db, pid))
            else:
                store.update_prompt(self.db, pid, **fields)
        for m in steering:
            content = m["content"] if isinstance(m["content"], str) else str(m["content"])
            scls, skind = (cls, kind) if cls != classify.HUMAN else (
                classify.aside_message(m["content"], content, prefixes) or (cls, kind))
            text, n, kinds = redact(content)
            spid = "%s:steer:%s" % (sid, m.get("id") or h12(text))
            if store.get_prompt(self.db, spid) is None:
                ts = (m.get("timestamp") or 0) / 1000.0 or r["started_at"] or now
                store.insert_prompt(
                    self.db, pid=spid, sid=sid, seq=store.next_seq(self.db, sid), ts=ts, native_id=str(r["id"]),
                    text=text, chars=len(text), redactions=n, redaction_kinds=store.jdump(kinds), origin_class=scls,
                    origin_kind=skind, prompt_source="steering", status="merged", capture_source="aside",
                    flags=store.jdump([]))
                self.after_classified(store.get_prompt(self.db, spid))
        store.update_session(self.db, sid, dirty=1)
        return status == "running"

    # ---------------------------------------------------------------- history.jsonl reconciliation
    def reconcile_history(self, now):
        pos = store.get_meta(self.db, "history_pos") or {}
        try:
            st = os.stat(paths.CC_HISTORY)
        except OSError:
            return
        offset = pos.get("offset", 0)
        if pos.get("inode") != st.st_ino or st.st_size < offset:
            offset = 0
        if st.st_size > offset:
            with open(paths.CC_HISTORY, "rb") as f:
                f.seek(offset)
                data = f.read()
            end = data.rfind(b"\n")
            if end >= 0:
                for raw in data[: end + 1].splitlines():
                    self._history_line(raw)
                offset += end + 1
        store.set_meta(self.db, "history_pos", {"inode": st.st_ino, "offset": offset})
        keep = []
        for h in self.history_recent:
            if not h["matched"] and now - h["ts"] >= HISTORY_GRACE_S:
                with self.txn():
                    self._reconcile_one(h, now)
                h["matched"] = True
            if now - h["ts"] < 3600:
                keep.append(h)
        self.history_recent = keep

    def _history_line(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        display = d.get("display") or ""
        ts = (d.get("timestamp") or 0) / 1000.0
        if ts < (self.install_ts or 0) or not d.get("sessionId"):
            return
        if display.startswith(("/", "!")) or not display.strip():
            return  # local commands and shell-mode lines are not prompts to the model
        if _paused_at(ts) or _excluded(self.config, d.get("project")):
            return
        text = redact(display)[0]
        self.history_recent.append({"session": d["sessionId"], "ts": ts, "text": text,
                                    "prefix": _history_prefix(text), "project": d.get("project"),
                                    "matched": False})

    def _reconcile_one(self, h, now):
        sid = "claude-code:" + h["session"]
        for p in self.db.execute("SELECT * FROM prompts WHERE sid=? AND ts BETWEEN ? AND ?",
                                 (sid, h["ts"] - HISTORY_MATCH_S, h["ts"] + HISTORY_MATCH_S)):
            if _text_matches(h, p["text"] or ""):
                return
        sess = store.get_session(self.db, sid)
        cwd = (sess["cwd"] if sess else None) or h.get("project")
        agent = (sess is not None and sess["attended"] == 0) or self.agent_cwd(cwd)
        store.upsert_session(self.db, sid, agent="claude-code", session_id=h["session"], cwd=cwd,
                             project=project_name(cwd), source="interactive", started=h["ts"], last_prompt=h["ts"],
                             dirty=1)
        pid = "%s:history:%d:%s" % (sid, int(h["ts"] * 1000), h12(h["text"]))
        store.insert_prompt(
            self.db, pid=pid, sid=sid, seq=store.next_seq(self.db, sid), ts=h["ts"], native_id="history",
            text=h["text"], chars=len(h["text"]), origin_class=classify.AGENT if agent else classify.HUMAN,
            origin_kind="history", status="ended-unknown", capture_source="history-reconcile", flags=store.jdump([]))
        store.set_meta(self.db, "reconciled_count", (store.get_meta(self.db, "reconciled_count", 0) or 0) + 1)
        store.set_meta(self.db, "last_reconciled_ts", now)
        self.after_classified(store.get_prompt(self.db, pid))
        self.log("history reconcile added a missed prompt", session=h["session"][:8])

    # ---------------------------------------------------------------- rendering
    def render_dirty(self, now):
        for s in self.db.execute("SELECT * FROM sessions WHERE dirty=1").fetchall():
            prompts = store.prompts_for(self.db, s["sid"])
            if any(p["origin_class"] == "pending" for p in prompts):
                continue  # wait until classified so the note lands in the right folder
            visible = [p for p in prompts if p["origin_class"] in ("human", "agent")]
            if not visible:
                store.update_session(self.db, s["sid"], dirty=0)
                continue
            note_class = "human" if any(p["origin_class"] == "human" for p in visible) else "agent"
            rel = s["note_path"]
            if rel is None or s["note_class"] != note_class:
                first = next((p for p in visible if p["origin_class"] == note_class), visible[0])
                new_rel = self._unique_path(render.note_relpath(s, first["text"], note_class), s["sid"])
                if rel and os.path.exists(os.path.join(paths.VAULT_DIR, rel)):
                    os.makedirs(os.path.dirname(os.path.join(paths.VAULT_DIR, new_rel)), exist_ok=True)
                    os.replace(os.path.join(paths.VAULT_DIR, rel), os.path.join(paths.VAULT_DIR, new_rel))
                rel = new_rel
            _atomic_write_text(os.path.join(paths.VAULT_DIR, rel), render.render_session(s, prompts))
            store.update_session(self.db, s["sid"], note_path=rel, note_class=note_class, dirty=0)
            self.home_dirty = True

    def _unique_path(self, rel, sid):
        base, ext = os.path.splitext(rel)
        cand, i = rel, 2
        while True:
            taken = self.db.execute("SELECT 1 FROM sessions WHERE note_path=? AND sid<>?", (cand, sid)).fetchone()
            if not taken and not os.path.exists(os.path.join(paths.VAULT_DIR, cand)):
                return cand
            cand = "%s %d%s" % (base, i, ext)
            i += 1

    def render_home(self, now):
        day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        totals = {}
        for r in self.db.execute("SELECT s.agent, p.ts FROM prompts p JOIN sessions s ON s.sid=p.sid "
                                 "WHERE p.origin_class='human'"):
            t = totals.setdefault(r["agent"], {"today": 0, "week": 0, "all": 0})
            t["all"] += 1
            t["week"] += r["ts"] >= now - 7 * 86400
            t["today"] += r["ts"] >= day0
        rows = self.db.execute(
            "SELECT s.note_path, s.last_prompt, (SELECT COUNT(*) FROM prompts p WHERE p.sid=s.sid AND "
            "p.origin_class='human') AS n FROM sessions s WHERE s.note_class='human' AND s.note_path IS NOT NULL "
            "ORDER BY s.last_prompt DESC LIMIT 30").fetchall()
        _atomic_write_text(os.path.join(paths.VAULT_DIR, "Home.md"), render.render_home(rows, totals, now))
        base = os.path.join(paths.VAULT_DIR, "Prompts.base")
        if not os.path.exists(base):
            _atomic_write_text(base, render.PROMPTS_BASE)
        self.home_dirty = False


# -------------------------------------------------------------------- helpers
class _Txn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, *_):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _noop(*_):
    pass


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _json_list(text):
    v = store.jload(text, [])
    return v if isinstance(v, list) else []


def _json_obj(text):
    v = store.jload(text, {})
    return v if isinstance(v, dict) else {}


def _paused(now):
    p = _read_json(paths.PAUSE_PATH)
    if p is None:
        return False
    until = p.get("until") if isinstance(p, dict) else None
    return until is None or until > now


def _paused_at(ts):
    """Was capture paused at time ts? Uses the current pause plus the log of past pauses."""
    p = _read_json(paths.PAUSE_PATH)
    spans = list(_read_json(paths.STATE_DIR + "/pause_log.json", []) or [])
    if isinstance(p, dict):
        spans.append([p.get("since") or 0, p.get("until")])
    for since, until in spans:
        if ts >= (since or 0) and (until is None or ts <= until):
            return True
    return False


def _excluded(config, cwd):
    return bool(cwd) and any(cwd == x or cwd.startswith(x.rstrip("/") + "/") for x in config.get("exclude_cwd_prefixes") or [])


def _history_prefix(display):
    """Text before the first paste/image placeholder, which the hook sees expanded."""
    cut = len(display)
    for marker in ("[Pasted text #", "[Image #", "[Pasted image"):
        i = display.find(marker)
        if i >= 0:
            cut = min(cut, i)
    return display[:cut].strip()[:80]


def _text_matches(h, text):
    prefix = h["prefix"]
    if not prefix:
        return True  # paste-only prompt: the timestamp window is the evidence
    return (text or "").strip().startswith(prefix) or prefix.startswith((text or "").strip()[:80])


def _atomic_write_text(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def acquire_lock():
    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(paths.LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def check_writable():
    """Fail fast (and quietly) if this process can't write the vault, e.g. no Documents permission."""
    for d in (paths.VAULT_DIR, paths.DATA_DIR, paths.SPOOL_DIR):
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".pv-write-probe-%d" % os.getpid())
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)


def main():
    log = Log(paths.LOG_PATH)
    fd = acquire_lock()
    if fd is None:
        return 0  # another daemon owns the lock
    try:
        check_writable()
    except OSError as e:
        log("daemon cannot write vault/data; exiting", error=type(e).__name__)
        return 1
    Daemon(log=log).run_forever()
    return 0
