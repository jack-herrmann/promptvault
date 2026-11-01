"""End-to-end: real hook subprocess -> spool -> daemon -> DB -> vault, all inside a temp dir."""

import importlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest

ROOT = tempfile.mkdtemp(prefix="pvtest-")
ENV = {
    "PROMPTVAULT_PROJECT": ROOT + "/proj", "PROMPTVAULT_VAULT": ROOT + "/proj/vault",
    "PROMPTVAULT_DATA": ROOT + "/proj/data.noindex", "PROMPTVAULT_RUNTIME": ROOT + "/runtime",
    "PROMPTVAULT_STATE": ROOT + "/state", "PROMPTVAULT_ASIDE_DB": ROOT + "/aside.db",
    "PROMPTVAULT_CC_HISTORY": ROOT + "/history.jsonl", "PROMPTVAULT_CC_SETTINGS": ROOT + "/settings.json",
    "PROMPTVAULT_CLI_SHIM": ROOT + "/bin/promptvault",
}
os.environ.update(ENV)

from promptvault import paths  # noqa: E402

importlib.reload(paths)
from promptvault import daemon, install, store  # noqa: E402

SECRET = "Tulip4Lantern88"
FIXTURE_SCHEMA = os.path.join(os.path.dirname(__file__), "fixtures", "aside_schema.sql")


def run_hook(payload, attended="1", entrypoint="cli", extra_env=None):
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "CLAUDE_CODE_SESSION_ATTENDED": attended,
           "CLAUDE_CODE_ENTRYPOINT": entrypoint}
    env.update(ENV)
    env.update(extra_env or {})
    p = subprocess.run([paths.PYTHON, "-I", "-S", paths.RUNTIME_DIR + "/pv_hook.py"],
                       input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=10)
    return p


def write_transcript(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def user(pid, text, **kw):
    e = {"type": "user", "promptId": pid, "message": {"role": "user", "content": text}, "entrypoint": "cli"}
    e.update(kw)
    return e


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install.install_runtime()
        install.ensure_state()
        with open(paths.CONFIG_PATH, "w") as f:
            json.dump(dict(daemon.DEFAULT_CONFIG, agent_text_prefixes=["[relay "]), f)
        cls.lock = daemon.acquire_lock()  # held by the test, so hooks never spawn a real daemon
        assert cls.lock is not None
        cls.t0 = time.time() - 1
        con = sqlite3.connect(paths.ASIDE_DB)
        with open(FIXTURE_SCHEMA) as f:
            con.executescript(f.read())
        now = int(time.time())
        con.execute("INSERT INTO sessions(id,title,trigger,status,incognito,cwd,created_at,updated_at) "
                    "VALUES('A_old','t','{\"type\":\"user\",\"source\":\"sidepanel\"}','idle',0,'/tmp',?,?)", (now, now))
        con.execute("INSERT INTO session_turns(session_id,user_message,started_at,finished_at,steering_messages,"
                    "files_changed,token_usage,last_message_timestamp,jsonl_read_offset,jsonl_read_size) "
                    "VALUES('A_old','[{\"role\":\"user\",\"content\":\"before install\"}]',?,?,'[]','[]','{}',?,0,0)",
                    (now, now, now))
        con.commit()
        con.close()
        cls.d = daemon.Daemon()
        store.set_meta(cls.d.db, "install_ts", cls.t0 - 5)
        cls.d.install_ts = cls.t0 - 5

    def tick(self, dt=0.0, aside=False):
        if aside:
            self.d.next["aside"] = 0
        self.d.tick(time.time() + dt)

    def prompts(self):
        return {r["text"]: r for r in self.d.db.execute("SELECT * FROM prompts")}

    def vault_text(self):
        out = []
        for dp, _, files in os.walk(paths.VAULT_DIR):
            for fn in files:
                with open(os.path.join(dp, fn), encoding="utf-8") as f:
                    out.append(f.read())
        return "\n".join(out)

    def test_01_hook_is_silent_and_redacts(self):
        tr = ROOT + "/s1.jsonl"
        p = run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S1", "prompt_id": "P1",
                      "transcript_path": tr, "cwd": ROOT, "prompt": "fix the parser; my password is " + SECRET})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(p.stderr, b"")
        spooled = os.listdir(paths.SPOOL_DIR)
        self.assertEqual(len([n for n in spooled if n.endswith(".json")]), 1)
        raw = open(os.path.join(paths.SPOOL_DIR, [n for n in spooled if n.endswith(".json")][0])).read()
        self.assertNotIn(SECRET, raw)
        status = json.load(open(paths.HOOK_STATUS_PATH))
        self.assertTrue(status["ok"], status)
        self.assertNotIn("spawned", status)

    def test_02_claude_code_flow(self):
        tr = ROOT + "/s1.jsonl"
        write_transcript(tr, [user("P1", "fix the parser; my password is " + SECRET, turnOrigin="human",
                                   promptSource="typed", origin={"kind": "human"})])
        # queued prompt typed mid-turn: same prompt_id, different text
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S1", "prompt_id": "P1",
                  "transcript_path": tr, "cwd": ROOT, "prompt": "also update the README"})
        write_transcript(tr, [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit"}]}},
            {"type": "user", "promptId": "P1", "toolUseResult": {"filePath": ROOT + "/parser.py"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "also update the README",
                                                  "origin": {"kind": "human"}, "humanTurn": True}},
        ])
        run_hook({"hook_event_name": "Stop", "session_id": "S1", "prompt_id": "P1", "transcript_path": tr,
                  "cwd": ROOT, "last_assistant_message": "done, " + SECRET})
        # a peer message and a task notification in the same session
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S1", "prompt_id": "P2",
                  "transcript_path": tr, "cwd": ROOT, "prompt": "[relay request req-1] From agent"})
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S1", "prompt_id": "P3",
                  "transcript_path": tr, "cwd": ROOT, "prompt": "<task-notification>\n<task-id>x</task-id>"})
        write_transcript(tr, [user("P2", "[relay request req-1] From agent", turnOrigin="peer",
                                   promptSource="system", origin={"kind": "peer"}),
                              user("P3", "<task-notification>\n<task-id>x</task-id>", turnOrigin="task_notification",
                                   promptSource="system", origin={"kind": "task-notification"})])
        # an interrupted turn: no Stop, interrupt marker in transcript
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S1", "prompt_id": "P4",
                  "transcript_path": tr, "cwd": ROOT, "prompt": "no, actually use the other file"})
        write_transcript(tr, [user("P4", "no, actually use the other file", turnOrigin="human", promptSource="typed"),
                              user("P4", "[Request interrupted by user for tool use]")])
        self.tick()
        self.tick(4)  # outcome scan after the Stop lag window
        self.tick(6)
        ps = self.prompts()
        p1 = ps["fix the parser; my password is «redacted:password»"]
        self.assertEqual((p1["origin_class"], p1["status"]), ("human", "completed"))
        self.assertEqual(json.loads(p1["files_changed"]), [ROOT + "/parser.py"])
        self.assertEqual(p1["tool_calls"], 1)
        self.assertEqual(p1["likely_correction"], 0)  # P4 "no, actually" follows the task-notification turn
        q = ps["also update the README"]
        self.assertEqual((q["origin_class"], q["prompt_source"], q["status"]), ("human", "queued", "merged"))
        self.assertEqual(ps["[relay request req-1] From agent"]["origin_class"], "agent")
        tn = self.d.db.execute("SELECT * FROM prompts WHERE native_id='P3'").fetchone()
        self.assertEqual((tn["origin_class"], tn["text"]), ("system", None))  # boundary only, no harness text
        self.assertEqual(ps["no, actually use the other file"]["status"], "interrupted")
        notes = [os.path.join(dp, f) for dp, _, fs in os.walk(paths.VAULT_DIR + "/sessions") for f in fs]
        self.assertEqual(len(notes), 1)
        text = open(notes[0], encoding="utf-8").read()
        self.assertIn("````text\nfix the parser", text)
        self.assertNotIn("<task-notification>", text)  # system turns are not rendered
        self.assertIn("#agent-written", text)  # the peer message, collapsed
        self.assertNotIn(SECRET, self.vault_text())

    def test_03_unattended_session_goes_to_agent_written(self):
        tr = ROOT + "/s2.jsonl"
        write_transcript(tr, [user("Q1", "Reply with just: ok", turnOrigin="human", promptSource="typed")])
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S2", "prompt_id": "Q1",
                  "transcript_path": tr, "cwd": ROOT, "prompt": "Reply with just: ok"}, attended="0")
        self.tick()
        p = self.prompts()["Reply with just: ok"]
        self.assertEqual((p["origin_class"], p["origin_kind"]), ("agent", "unattended"))
        agent_notes = [f for _, _, fs in os.walk(paths.VAULT_DIR + "/agent-written") for f in fs]
        self.assertEqual(len(agent_notes), 1)

    def test_04_skip_env_and_pause(self):
        n0 = len(os.listdir(paths.SPOOL_DIR))
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S3", "prompt_id": "R1", "cwd": ROOT,
                  "prompt": "skipped"}, extra_env={"PROMPTVAULT_SKIP": "1"})
        with open(paths.PAUSE_PATH, "w") as f:
            json.dump({"since": time.time(), "until": None}, f)
        run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "S3", "prompt_id": "R2", "cwd": ROOT,
                  "prompt": "paused"})
        os.remove(paths.PAUSE_PATH)
        self.assertEqual(len(os.listdir(paths.SPOOL_DIR)), n0)

    def test_05_cursor(self):
        run_hook({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "C1", "generation_id": "G1",
                  "prompt": "cursor prompt", "workspace_roots": [ROOT], "user_email": "x@example.com",
                  "attachments": [{"type": "file", "filePath": "/secret.txt"}], "model": "m"})
        run_hook({"hook_event_name": "stop", "conversation_id": "C1", "generation_id": "G1", "status": "aborted",
                  "workspace_roots": [ROOT]})
        self.tick()
        p = self.prompts()["cursor prompt"]
        self.assertEqual((p["origin_class"], p["status"]), ("human", "interrupted"))
        self.assertNotIn("x@example.com", json.dumps([dict(r) for r in self.d.db.execute("SELECT * FROM sessions")]))

    def test_06_aside(self):
        con = sqlite3.connect(paths.ASIDE_DB)
        def sess(i, trig, incog=0):
            con.execute("INSERT INTO sessions(id,title,trigger,status,incognito,cwd,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", (i, "t", trig, "idle", incog, "/tmp", int(time.time()), int(time.time())))
        def turn(sid, msg, finished=True, abort=None, steering=None):
            now = int(time.time())
            cur = con.execute("INSERT INTO session_turns(session_id,user_message,started_at,finished_at,aborted_at,"
                              "abort_reason,steering_messages,files_changed,token_usage,last_message_timestamp,"
                              "jsonl_read_offset,jsonl_read_size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (sid, json.dumps(msg), now, now + 5 if finished else None, now + 5 if abort else None,
                               json.dumps(abort) if abort else None, json.dumps(steering or []), "[]", "{}", now, 0, 0))
            return cur.lastrowid
        sess("A1", '{"type":"user","source":"sidepanel"}')
        sess("A2", '{"type":"user","source":"cli"}')
        sess("A3", '{"type":"user","source":"tasks-page"}', incog=1)
        sess("A4", '{"type":"subagent","toolCallId":"x"}')
        turn("A1", [{"role": "system-message", "content": "skill docs"},
                    {"role": "user", "content": "aside human prompt; pwd is \"" + SECRET + "\""}],
             steering=[{"role": "steering", "id": "st1", "content": "and in German please", "timestamp": time.time() * 1000},
                       {"role": "steering", "id": "st2", "content": "[relay message req-2] From agent",
                        "timestamp": time.time() * 1000}])
        # agents post into the chat you started (aside session resume): list content, [Sender] tag
        turn("A1", [{"role": "user", "content": [{"type": "text", "text": "[Reviewer] batch 4 is in"}]}])
        turn("A1", [{"role": "user", "content": "no, actually stop"}])  # answers the Reviewer turn, not yours
        turn("A1", [{"role": "user", "content": "wait, keep the tests"}])  # answers your own turn
        turn("A2", [{"role": "user", "content": "aside cli prompt"}], abort={"kind": "error", "error": "x"})
        turn("A3", [{"role": "user", "content": "incognito prompt"}])
        turn("A4", [{"role": "system-message", "kind": "subagent_done", "content": "done"}])
        con.commit()
        self.tick(3, aside=True)
        ps = self.prompts()
        self.assertNotIn("before install", ps)
        h = ps['aside human prompt; pwd is "«redacted:password»"']
        self.assertEqual((h["origin_class"], h["status"]), ("human", "completed"))
        self.assertEqual((ps["and in German please"]["prompt_source"], ps["and in German please"]["origin_class"]),
                         ("steering", "human"))
        self.assertEqual(ps["[relay message req-2] From agent"]["origin_class"], "agent")
        rv = ps["[Reviewer] batch 4 is in"]
        self.assertEqual((rv["origin_class"], rv["origin_kind"]), ("agent", "Reviewer"))
        self.assertEqual(ps["no, actually stop"]["origin_class"], "human")
        self.assertEqual(h["likely_correction"], 0)
        self.assertEqual(ps["no, actually stop"]["likely_correction"], 1)
        self.assertIn("#agent-written", self.vault_text())
        self.assertEqual((ps["aside cli prompt"]["origin_class"], ps["aside cli prompt"]["status"]), ("agent", "error"))
        self.assertNotIn("incognito prompt", ps)
        self.assertNotIn("done", ps)
        # paused: new turns are skipped and not caught up after resume
        with open(paths.PAUSE_PATH, "w") as f:
            json.dump({"since": time.time(), "until": None}, f)
        turn("A1", [{"role": "user", "content": "typed while paused"}])
        con.commit()
        self.tick(6, aside=True)
        os.remove(paths.PAUSE_PATH)
        self.tick(9, aside=True)
        self.assertNotIn("typed while paused", self.prompts())
        con.close()
        self.assertNotIn(SECRET, self.vault_text())

    def test_07_history_reconcile(self):
        now = time.time()
        with open(paths.CC_HISTORY, "a") as f:
            for d in [{"display": "a prompt the hook missed", "sessionId": "S9", "timestamp": int(now * 1000), "project": ROOT},
                      {"display": "fix the parser; my password is " + SECRET, "sessionId": "S1", "timestamp": int(now * 1000)},
                      {"display": "/effort", "sessionId": "S9", "timestamp": int(now * 1000)},
                      {"display": "old one", "sessionId": "S9", "timestamp": int((self.t0 - 100) * 1000)}]:
                f.write(json.dumps(d) + "\n")
        self.d.next["history"] = 0
        self.tick(1)  # read
        self.d.next["history"] = 0
        self.tick(200)  # past grace period
        ps = self.prompts()
        self.assertEqual(ps["a prompt the hook missed"]["capture_source"], "history-reconcile")
        self.assertNotIn("/effort", ps)
        self.assertNotIn("old one", ps)
        n = sum(1 for t in ps if t and t.startswith("fix the parser"))
        self.assertEqual(n, 1)  # already captured by the hook: no duplicate

    def test_08_replayed_event_is_idempotent(self):
        before = self.d.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        name = "0000000000000000001-1-aaaaaa.json"
        rec = {"v": 1, "agent": "claude-code", "kind": "prompt", "ts": time.time(), "session_id": "S1",
               "prompt_id": "P1", "prompt": "also update the README", "env": {"attended": "1"}}
        for _ in range(2):
            with open(os.path.join(paths.SPOOL_DIR, name), "w") as f:
                json.dump(rec, f)
            self.tick()
        self.assertEqual(self.d.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], before)

    def test_09_home_and_base(self):
        self.d.home_dirty = True
        self.d.next["home"] = 0
        self.tick()
        self.assertTrue(os.path.exists(paths.VAULT_DIR + "/Home.md"))
        self.assertTrue(os.path.exists(paths.VAULT_DIR + "/Prompts.base"))


if __name__ == "__main__":
    unittest.main()
