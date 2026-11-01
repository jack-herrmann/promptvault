"""Canonical SQLite store (single writer: the daemon). Text in here is already redacted."""

import json
import os
import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, ts REAL, agent TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY, agent TEXT, session_id TEXT, project TEXT, cwd TEXT, model TEXT, title TEXT,
  source TEXT, attended INTEGER, started REAL, last_prompt REAL, ended REAL, end_reason TEXT,
  transcript_path TEXT, note_path TEXT, note_class TEXT, dirty INTEGER DEFAULT 1, extra TEXT);
CREATE TABLE IF NOT EXISTS prompts(
  pid TEXT PRIMARY KEY, sid TEXT, seq INTEGER, ts REAL, native_id TEXT, text TEXT, chars INTEGER,
  redactions INTEGER DEFAULT 0, redaction_kinds TEXT, origin_class TEXT, origin_kind TEXT,
  prompt_source TEXT, turn_origin TEXT, surface TEXT, flags TEXT, status TEXT, ended REAL,
  duration_s REAL, files_changed TEXT, tool_calls INTEGER, reply_chars INTEGER,
  likely_correction INTEGER DEFAULT 0, capture_source TEXT, resolve_deadline REAL,
  outcome_scan_at REAL, extra TEXT);
CREATE INDEX IF NOT EXISTS prompts_sid ON prompts(sid, seq);
CREATE INDEX IF NOT EXISTS prompts_pending ON prompts(origin_class) WHERE origin_class = 'pending';
CREATE INDEX IF NOT EXISTS prompts_native ON prompts(sid, native_id);
"""


def connect(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; explicit BEGIN for txns
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    if get_meta(db, "schema_version") is None:
        set_meta(db, "schema_version", SCHEMA_VERSION)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))


def event_seen(db, event_id):
    return db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is not None


def mark_event(db, event_id, ts, agent, kind):
    db.execute("INSERT OR IGNORE INTO events(event_id,ts,agent,kind) VALUES(?,?,?,?)", (event_id, ts, agent, kind))


def get_session(db, sid):
    return db.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()


def upsert_session(db, sid, **fields):
    """Insert a session or fill in fields that are still empty (never overwrite with None)."""
    row = get_session(db, sid)
    if row is None:
        cols = ["sid"] + list(fields)
        db.execute("INSERT INTO sessions(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                   [sid] + list(fields.values()))
        return
    updates = {k: v for k, v in fields.items() if v is not None and (row[k] is None or k in _ALWAYS_UPDATE)}
    if updates:
        db.execute("UPDATE sessions SET %s WHERE sid=?" % ",".join("%s=?" % k for k in updates),
                   list(updates.values()) + [sid])


_ALWAYS_UPDATE = {"last_prompt", "ended", "end_reason", "dirty", "model", "title"}


def update_session(db, sid, **fields):
    if fields:
        db.execute("UPDATE sessions SET %s WHERE sid=?" % ",".join("%s=?" % k for k in fields),
                   list(fields.values()) + [sid])


def next_seq(db, sid):
    row = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM prompts WHERE sid=?", (sid,)).fetchone()
    return row[0]


def get_prompt(db, pid):
    return db.execute("SELECT * FROM prompts WHERE pid=?", (pid,)).fetchone()


def insert_prompt(db, **fields):
    cols = list(fields)
    db.execute("INSERT OR IGNORE INTO prompts(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
               list(fields.values()))


def update_prompt(db, pid, **fields):
    if fields:
        db.execute("UPDATE prompts SET %s WHERE pid=?" % ",".join("%s=?" % k for k in fields),
                   list(fields.values()) + [pid])


def prompts_for(db, sid):
    return db.execute("SELECT * FROM prompts WHERE sid=? ORDER BY seq", (sid,)).fetchall()


def jdump(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def jload(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
