"""Turn the database into Obsidian notes. Notes are machine-owned and regenerated atomically."""

import datetime
import json
import os
import re
import unicodedata

from promptvault.store import jload

AGENT_LABEL = {"claude-code": "Claude Code", "cursor": "Cursor", "aside": "Aside"}
STATUS_LABEL = {
    "completed": "✅ completed", "interrupted": "⏹ interrupted", "error": "⚠️ error",
    "running": "⏳ running", "merged": "↪ sent mid-turn", "ended-unknown": "❔ ended without a result",
    None: "⏳ running",
}
LONG_PROMPT = 4000
_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]`\x00-\x1f\x7f]')


def local_dt(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone()


def iso(ts):
    return local_dt(ts).isoformat(timespec="seconds") if ts else None


def _clip_bytes(s, limit):
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore").rstrip()


def clean_name(text, limit_bytes):
    s = unicodedata.normalize("NFC", text or "")
    s = _BAD_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(".").strip()
    return _clip_bytes(s, limit_bytes)


def first_words(text, n=8):
    words = re.findall(r"\S+", text or "")
    return " ".join(words[:n])


def note_relpath(session, first_text, note_class):
    """vault-relative path; fixed once created."""
    started = session["started"] or 0
    dt = local_dt(started)
    agent = session["agent"]
    folder = "sessions" if note_class == "human" else "agent-written"
    project = clean_name(session["project"] or AGENT_LABEL.get(agent, agent), 40) or "session"
    words = clean_name(first_words(first_text), 60) or "untitled"
    sid6 = re.sub(r"[^A-Za-z0-9]", "", session["session_id"] or "")[:6] or "000000"
    base = "%s %s - %s %s" % (dt.strftime("%Y-%m-%d %H%M"), project, words, sid6)
    base = _clip_bytes(base, 120)
    return os.path.join(folder, agent, dt.strftime("%Y-%m"), base + ".md")


def fence_for(text):
    longest = max([len(m) for m in re.findall(r"`+", text or "")] + [0])
    return "`" * max(4, longest + 1)


def _yaml(value):
    # JSON (ASCII-escaped) is valid YAML and immune to U+2028/U+0085 line-break surprises.
    return json.dumps(value, ensure_ascii=True)


def _outcome_line(p):
    bits = [STATUS_LABEL.get(p["status"], p["status"] or "")]
    if p["duration_s"] is not None and p["status"] in ("completed", "interrupted", "error"):
        bits.append(_human_duration(p["duration_s"]))
    if p["tool_calls"]:
        bits.append("%d tool call%s" % (p["tool_calls"], "" if p["tool_calls"] == 1 else "s"))
    files = jload(p["files_changed"], []) or []
    if files:
        shown = ", ".join("`%s`" % os.path.basename(f).replace("`", "'") for f in files[:8])
        bits.append("files: " + shown + (" +%d more" % (len(files) - 8) if len(files) > 8 else ""))
    if p["likely_correction"]:
        bits.append("next prompt looks like a correction")
    if p["redactions"]:
        bits.append("%d secret%s redacted" % (p["redactions"], "" if p["redactions"] == 1 else "s"))
    return " · ".join(b for b in bits if b)


def _human_duration(s):
    s = int(round(s or 0))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min %02d s" % (s // 60, s % 60)
    return "%d h %02d min" % (s // 3600, (s % 3600) // 60)


def _quote_block(lines):
    return "\n".join(("> " + ln) if ln else ">" for ln in lines)


def render_prompt(p, n):
    text = p["text"] if p["text"] is not None else "(empty)"
    fence = fence_for(text)
    body = [fence + "text"] + text.split("\n") + [fence]
    when = local_dt(p["ts"]).strftime("%H:%M:%S")
    anchor = " ^p%d" % n
    flags = jload(p["flags"], []) or []
    if p["origin_class"] == "agent":
        label = "agent-written" + (" (%s)" % p["origin_kind"] if p["origin_kind"] else "")
        head = "> [!quote]- %s · %s #agent-written" % (when, label)
        return "\n".join([head, _quote_block(body), ">", "> " + _outcome_line(p) + anchor])
    title = "### Prompt %d · %s" % (n, when)
    extra = []
    if p["prompt_source"] == "queued" or p["status"] == "merged":
        extra.append("sent while the previous turn was running")
    if p["prompt_source"] == "suggestion_accepted":
        extra.append("accepted suggestion")
    if p["prompt_source"] == "steering":
        extra.append("steering message")
    if "correction" in flags:
        extra.append("looks like a correction of the previous prompt")
    if p["capture_source"] == "history-reconcile":
        extra.append("recovered from history (hook missed it)")
    lines = [title]
    if extra:
        lines.append("*" + "; ".join(extra) + "*")
    if len(text) > LONG_PROMPT:
        lines.append("> [!note]- Long prompt (%s characters)" % format(len(text), ","))
        lines.append(_quote_block(body))
    else:
        lines.extend(body)
    lines.append("")
    lines.append(_outcome_line(p) + anchor)
    return "\n".join(lines)


def render_session(session, prompts):
    visible = [p for p in prompts if p["origin_class"] in ("human", "agent")]
    human = [p for p in visible if p["origin_class"] == "human"]
    agent = session["agent"]
    title_src = human[0]["text"] if human else (visible[0]["text"] if visible else "")
    fm = [
        ("agent", agent), ("session_id", session["session_id"]), ("project", session["project"]),
        ("cwd", session["cwd"]), ("model", session["model"]), ("source", session["source"]),
        ("started", iso(session["started"])), ("last_prompt", iso(session["last_prompt"])),
        ("prompts", len(human)), ("agent_prompts", len(visible) - len(human)),
        ("interrupted", sum(1 for p in human if p["status"] == "interrupted")),
        ("corrections", sum(1 for p in human if p["likely_correction"])),
        ("redactions", sum(p["redactions"] or 0 for p in visible)),
        ("tags", ["promptvault/session", "agent/" + agent]),
    ]
    out = ["---"] + ["%s: %s" % (k, _yaml(v)) for k, v in fm] + ["---", ""]
    out.append("# " + (clean_name(first_words(title_src, 12), 200) or "Session"))
    meta = [AGENT_LABEL.get(agent, agent)]
    if session["project"]:
        meta.append(session["project"])
    if session["started"]:
        meta.append(local_dt(session["started"]).strftime("%a %d %b %Y, %H:%M"))
    out.append(" · ".join(meta) + " · [[Home]]")
    out.append("")
    out.append("%% Generated by PROMPTVAULT from its database; edits here are overwritten. "
               "Annotate in your own notes and link to a prompt as note-name#^p1 inside double brackets. %%")
    out.append("")
    n = 0
    for p in visible:
        n += 1
        out.append(render_prompt(p, n))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_home(rows, totals, now):
    out = ["---", "tags: %s" % _yaml(["promptvault/home"]), "---", "", "# Prompt vault", "",
           "%% Generated by PROMPTVAULT; rewritten automatically. %%", "",
           "Updated %s." % local_dt(now).strftime("%a %d %b %Y, %H:%M"), "", "## Your prompts", "",
           "| Agent | Today | Last 7 days | All |", "|---|---|---|---|"]
    for agent, t in sorted(totals.items()):
        out.append("| %s | %d | %d | %d |" % (AGENT_LABEL.get(agent, agent), t["today"], t["week"], t["all"]))
    out += ["", "## Recent sessions", ""]
    for r in rows:
        name = os.path.splitext(os.path.basename(r["note_path"]))[0]
        out.append("- [[%s]] · %d prompt%s" % (name, r["n"], "" if r["n"] == 1 else "s"))
    out += ["", "Agent-written sessions are under `agent-written/`. A table of every session: [[Prompts.base]]", ""]
    return "\n".join(out)


PROMPTS_BASE = """filters:
  and:
    - file.inFolder("sessions")
views:
  - type: table
    name: Sessions
    order:
      - file.name
      - agent
      - project
      - started
      - prompts
      - interrupted
      - corrections
"""
