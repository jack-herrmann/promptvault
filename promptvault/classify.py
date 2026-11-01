"""Who wrote a prompt: you ("human"), another program/agent ("agent"), or the harness ("system").

Pure functions over the evidence each source gives us.
"""

import json
import re

HUMAN, AGENT, SYSTEM = "human", "agent", "system"

# Claude Code transcript turnOrigin values -> class
_TURN_ORIGIN = {
    "human": HUMAN,
    "peer": AGENT, "sdk": AGENT, "scheduled": AGENT,
    "task_notification": SYSTEM, "system": SYSTEM, "auto_continuation": SYSTEM,
}
# Older Claude Code versions only wrote origin.kind
_ORIGIN_KIND = {
    "human": HUMAN, "peer": AGENT, "task-notification": SYSTEM, "auto-continuation": SYSTEM,
    "scheduled": AGENT, "sdk": AGENT,
}


def claude_code_unattended(env):
    """`claude -p` and `claude --bg` sessions run with CLAUDE_CODE_SESSION_ATTENDED=0."""
    return env.get("attended") == "0" or env.get("entrypoint") == "sdk-cli"


def from_transcript(info):
    """(class, origin) from a transcript prompt entry, or (None, None) if it says nothing useful."""
    if not info:
        return None, None
    if info.get("entrypoint") == "sdk-cli":
        return AGENT, "sdk"
    to = info.get("turn_origin")
    if to in _TURN_ORIGIN:
        return _TURN_ORIGIN[to], to
    kind = info.get("origin_kind")
    if kind in _ORIGIN_KIND:
        return _ORIGIN_KIND[kind], kind
    return None, None


def from_queued(q):
    if not q:
        return None, None
    kind = q.get("origin_kind")
    if kind == "human" or (kind is None and q.get("human_turn")):
        return HUMAN, "queued"
    if kind in _ORIGIN_KIND:
        return _ORIGIN_KIND[kind], kind
    return None, None


def from_text(text, agent_prefixes=()):
    """Last-resort patterns for harness/agent messages; `agent_prefixes` come from config.json."""
    t = (text or "").lstrip()
    if t.startswith("<task-notification>"):
        return SYSTEM, "task_notification"
    if any(p and t.startswith(p) for p in agent_prefixes):
        return AGENT, "peer"
    return None, None


# --- Aside ---------------------------------------------------------------------------------
ASIDE_HUMAN_SOURCES = {"tasks-page", "sidepanel", "new-tab"}


def aside_session(trigger_raw):
    """(class, origin) for an Aside session from its `trigger` JSON."""
    try:
        trig = json.loads(trigger_raw) if trigger_raw else None
    except ValueError:
        trig = None
    if not isinstance(trig, dict):
        return HUMAN, "unknown"  # pre-trigger sessions; excluded from coaching via origin
    ttype, source = trig.get("type"), trig.get("source")
    if ttype == "user" and source == "cli":
        return AGENT, "cli"
    if ttype == "user":
        return HUMAN, source or "unknown"
    return AGENT, ttype or "unknown"  # subagent, routine, future types


_ASIDE_TAG = re.compile(r"^\s*\[([^\]\n]{1,80})\]")


def aside_message(content, text, agent_prefixes=()):
    """(class, kind) for one role:user message inside a session you started, or None if it's yours.

    The Aside UI stores typed text as a plain string; `aside session resume` (agents, other Aside
    chats) stores a list of text parts that opens with a [Sender] tag. In testing the two signals
    always agreed. Text starting with one of `agent_prefixes` (config.json) is an agent's either way.
    """
    known = next((p for p in agent_prefixes if p and (text or "").lstrip().startswith(p)), None)
    if known:
        return AGENT, known.strip("[ ")[:40] or "message"
    if not isinstance(content, list):
        return None
    tag = _ASIDE_TAG.match(text or "")
    return AGENT, tag.group(1).strip()[:40] if tag else "message"


# --- follow-up corrections -----------------------------------------------------------------
# Documented cue list (EN + DE). Phase 2 recomputes from the raw sequence; this is a cheap flag.
CORRECTION_RE = re.compile(
    r"^\W*(?:no\b|nope\b|not\s+(?:what|that|quite|like\s+that)\b|actually\b|instead\b|wait\b|"
    r"that'?s\s+(?:wrong|not|incorrect)\b|wrong\b|still\b|undo\b|revert\b|"
    r"(?:it\s+)?(?:still\s+)?(?:doesn'?t|didn'?t|does\s+not|did\s+not)\s+work|"
    r"it\s+(?:still\s+)?(?:fails|failed|breaks|broke)\b|you\s+(?:forgot|missed|didn'?t|did\s+not)\b|"
    r"why\s+did\s+you\b|stop\b|nein\b|nicht\s+so\b|stattdessen\b|doch\s+nicht\b|falsch\b|"
    r"immer\s+noch\b|funktioniert\s+(?:immer\s+noch\s+)?nicht\b|rückgängig\b)",
    re.IGNORECASE,
)
CORRECTION_WINDOW_S = 15 * 60


def looks_like_correction(text):
    return bool(CORRECTION_RE.match((text or "")[:200]))
