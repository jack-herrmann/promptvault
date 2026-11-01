"""Incremental reader for Claude Code transcripts (~/.claude/projects/<proj>/<session>.jsonl).

Transcripts can be truncated, rewritten or compacted, so nothing here relies on byte
positions surviving: a file that shrinks or changes inode is re-indexed from scratch,
and entries are found by promptId / tool_use id.
"""

import json
import os

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
INTERRUPT_PREFIX = "[Request interrupted by user"


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class TranscriptIndex:
    def __init__(self, path):
        self.path = path
        self._reset()

    SIG = 64

    def _reset(self):
        self.inode = None
        self.offset = 0
        self.head_sig = b""  # first bytes of the file
        self.tail_sig = b""  # bytes just before self.offset
        self.prompts = {}  # promptId -> dict(turn_origin, prompt_source, origin_kind, entrypoint, text)
        self.queued = []  # dicts(text, origin_kind, human_turn)
        self.interrupted = set()  # promptIds
        self.tool_names = {}  # tool_use id -> name
        self.tool_results = {}  # promptId -> count
        self.files = {}  # promptId -> ordered list of file paths
        self._pending_results = []  # (promptId, tool_use_id, filePath) whose tool name was not known yet
        self.model = None  # model of the latest assistant message

    def refresh(self):
        """Read whatever was appended since last time. Returns False if the file is missing."""
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        with open(self.path, "rb") as f:
            if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset
                                           or not self._unchanged(f)):
                self._reset()  # truncated / rewritten / replaced: re-index from scratch
            self.inode = st.st_ino
            if st.st_size == self.offset:
                return True
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return True  # only a partial line so far
        for raw in data[: end + 1].splitlines():
            if raw.strip():
                self._ingest(raw)
        prev = self.offset
        self.offset += end + 1
        if prev == 0:
            self.head_sig = data[: self.SIG]
        self.tail_sig = (self.tail_sig + data[: end + 1])[-self.SIG:]
        return True

    def _unchanged(self, f):
        """Same bytes at the start and just before our offset as when we last read?"""
        if self.offset == 0:
            return True
        f.seek(0)
        if f.read(len(self.head_sig)) != self.head_sig:
            return False
        f.seek(self.offset - len(self.tail_sig))
        return f.read(len(self.tail_sig)) == self.tail_sig

    def _ingest(self, raw):
        try:
            e = json.loads(raw)
        except ValueError:
            return
        if not isinstance(e, dict):
            return
        etype = e.get("type")
        pid = e.get("promptId")
        if etype == "assistant":
            model = (e.get("message") or {}).get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                self.model = model
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    self.tool_names[b["id"]] = b.get("name")
            if self._pending_results:
                still = []
                for p, tid, fp in self._pending_results:
                    if tid in self.tool_names:
                        self._add_file(p, tid, fp)
                    else:
                        still.append((p, tid, fp))
                self._pending_results = still
            return
        if etype == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "queued_command":
                self.queued.append({"text": a.get("prompt") if isinstance(a.get("prompt"), str) else _text_of(a.get("prompt")),
                                    "origin_kind": (a.get("origin") or {}).get("kind"),
                                    "human_turn": a.get("humanTurn")})
            return
        if etype != "user":
            return
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            if pid:
                self.tool_results[pid] = self.tool_results.get(pid, 0) + 1
                tur = e.get("toolUseResult")
                fp = tur.get("filePath") if isinstance(tur, dict) else None
                if fp:
                    tid = next((b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"), None)
                    if tid in self.tool_names:
                        self._add_file(pid, tid, fp)
                    else:
                        self._pending_results.append((pid, tid, fp))
            text = _text_of(content)
            if pid and INTERRUPT_PREFIX in json.dumps(content):
                self.interrupted.add(pid)
            return
        text = _text_of(content)
        if pid and text.startswith(INTERRUPT_PREFIX):
            self.interrupted.add(pid)
            return
        if not pid:
            return
        origin = e.get("origin") if isinstance(e.get("origin"), dict) else None
        info = {"turn_origin": e.get("turnOrigin"), "prompt_source": e.get("promptSource"),
                "origin_kind": origin.get("kind") if origin else None, "entrypoint": e.get("entrypoint"),
                "text": text}
        have = self.prompts.get(pid)
        # Several entries can share a promptId (meta/caveat rows); prefer the one carrying turnOrigin, then origin.
        rank = 2 if info["turn_origin"] else (1 if info["origin_kind"] else 0)
        if have is None or rank > have["_rank"]:
            info["_rank"] = rank
            self.prompts[pid] = info

    def _add_file(self, pid, tid, fp):
        if self.tool_names.get(tid) in FILE_TOOLS:
            lst = self.files.setdefault(pid, [])
            if fp not in lst:
                lst.append(fp)

    def find_queued(self, text, transform=None):
        """Origin of a mid-turn (queued) prompt, matched by its text.

        The hook's copy is already redacted, so `transform` (redaction) is applied to the
        transcript's raw copy before comparing.
        """
        norm = (text or "").strip()
        for q in reversed(self.queued):
            cand = (q.get("text") or "").strip()
            if transform:
                cand = transform(cand).strip()
            if cand == norm:
                return q
        return None


class TranscriptCache:
    """Keeps one index per transcript path; drops the least recently used beyond a cap."""

    def __init__(self, cap=64):
        self.cap = cap
        self._items = {}

    def get(self, path):
        if not path:
            return None
        idx = self._items.pop(path, None) or TranscriptIndex(path)
        self._items[path] = idx
        while len(self._items) > self.cap:
            self._items.pop(next(iter(self._items)))
        return idx if idx.refresh() else None
