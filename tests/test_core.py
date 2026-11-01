import json
import os
import tempfile
import unittest

from promptvault import classify, install, render
from promptvault.transcript import TranscriptIndex


def _user(pid, text, **kw):
    e = {"type": "user", "promptId": pid, "message": {"role": "user", "content": text}}
    e.update(kw)
    return e


class ClassifyTest(unittest.TestCase):
    def test_turn_origins(self):
        cases = {"human": "human", "peer": "agent", "sdk": "agent", "scheduled": "agent",
                 "task_notification": "system", "system": "system", "auto_continuation": "system"}
        for to, cls in cases.items():
            self.assertEqual(classify.from_transcript({"turn_origin": to})[0], cls, to)

    def test_old_versions_origin_kind(self):
        self.assertEqual(classify.from_transcript({"origin_kind": "human"})[0], "human")
        self.assertEqual(classify.from_transcript({"origin_kind": "task-notification"})[0], "system")

    def test_sdk_entrypoint_wins(self):
        self.assertEqual(classify.from_transcript({"turn_origin": "human", "entrypoint": "sdk-cli"})[0], "agent")

    def test_unattended(self):
        self.assertTrue(classify.claude_code_unattended({"attended": "0", "entrypoint": "cli"}))
        self.assertTrue(classify.claude_code_unattended({"attended": None, "entrypoint": "sdk-cli"}))
        self.assertFalse(classify.claude_code_unattended({"attended": "1", "entrypoint": "cli"}))

    def test_queued(self):
        self.assertEqual(classify.from_queued({"origin_kind": "human", "human_turn": True}), ("human", "queued"))
        self.assertEqual(classify.from_queued({"origin_kind": "peer"})[0], "agent")

    def test_text_patterns(self):
        self.assertEqual(classify.from_text("<task-notification>\n<task-id>x")[0], "system")
        self.assertEqual(classify.from_text("[relay request req-1] From ...", ("[relay ",))[0], "agent")
        self.assertEqual(classify.from_text("[relay request req-1] From ..."), (None, None))  # not configured
        self.assertEqual(classify.from_text("hello", ("[relay ",)), (None, None))

    def test_aside_sessions(self):
        self.assertEqual(classify.aside_session('{"type":"user","source":"sidepanel"}'), ("human", "sidepanel"))
        self.assertEqual(classify.aside_session('{"type":"user","source":"cli"}'), ("agent", "cli"))
        self.assertEqual(classify.aside_session('{"type":"subagent","toolCallId":"x"}')[0], "agent")
        self.assertEqual(classify.aside_session('{"type":"routine"}')[0], "agent")
        self.assertEqual(classify.aside_session(None), ("human", "unknown"))

    def test_aside_messages(self):
        parts = lambda t: [{"type": "text", "text": t}]  # noqa: E731
        self.assertIsNone(classify.aside_message("so what are we still waiting on?", "so what are we still waiting on?"))
        self.assertIsNone(classify.aside_message("[urgent] fix it", "[urgent] fix it"))  # typed: tag alone isn't enough
        self.assertEqual(classify.aside_message(parts("[Reviewer] batch 4 is in"), "[Reviewer] batch 4 is in"),
                         ("agent", "Reviewer"))
        t = '[relay reply req-x] Claude Code agent "a" answered'
        relay = ("[relay ",)
        self.assertEqual(classify.aside_message(parts(t), t, relay), ("agent", "relay"))
        self.assertEqual(classify.aside_message(t, t, relay), ("agent", "relay"))  # known sender, even as a string
        self.assertIsNone(classify.aside_message(t, t))  # a string with an unconfigured tag is yours
        self.assertEqual(classify.aside_message(parts(t), t), ("agent", "relay reply req-x"))
        self.assertEqual(classify.aside_message(parts("no tag"), "no tag"), ("agent", "message"))

    def test_corrections(self):
        for t in ["no, use the other file", "Actually, revert that", "that's wrong", "it still doesn't work",
                  "nein, nicht so", "funktioniert immer noch nicht", "why did you delete it?"]:
            self.assertTrue(classify.looks_like_correction(t), t)
        for t in ["now add tests", "great, next step", "notice the bug in line 3", "nothing else to do?"]:
            self.assertFalse(classify.looks_like_correction(t), t)


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "s.jsonl")

    def write(self, entries, mode="a"):
        with open(self.path, mode) as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_index_prompts_tools_files_interrupts(self):
        self.write([
            _user("P1", "<local-command-caveat>x", isMeta=True),
            _user("P1", "fix the bug", turnOrigin="human", promptSource="typed", origin={"kind": "human"}),
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit"},
                                                           {"type": "tool_use", "id": "t2", "name": "ExitPlanMode"}]}},
            {"type": "user", "promptId": "P1", "toolUseResult": {"filePath": "/x/a.py"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
            {"type": "user", "promptId": "P1", "toolUseResult": {"filePath": "/plans/p.md"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t2"}]}},
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "also this",
                                                  "origin": {"kind": "human"}, "humanTurn": True}},
            _user("P2", "[Request interrupted by user for tool use]"),
        ])
        idx = TranscriptIndex(self.path)
        idx.refresh()
        self.assertEqual(idx.prompts["P1"]["turn_origin"], "human")
        self.assertEqual(idx.prompts["P1"]["text"], "fix the bug")
        self.assertEqual(idx.tool_results["P1"], 2)
        self.assertEqual(idx.files["P1"], ["/x/a.py"])  # ExitPlanMode's path is not a changed file
        self.assertIn("P2", idx.interrupted)
        self.assertEqual(idx.find_queued("also this")["origin_kind"], "human")

    def test_partial_line_and_truncation(self):
        self.write([_user("P1", "one", turnOrigin="human")])
        with open(self.path, "a") as f:
            f.write('{"type": "user", "promptId": "P2"')  # partial line
        idx = TranscriptIndex(self.path)
        idx.refresh()
        self.assertIn("P1", idx.prompts)
        self.assertNotIn("P2", idx.prompts)
        # file rewritten shorter (tombstone/compaction): index resets and re-reads
        self.write([_user("P3", "three", turnOrigin="peer")], mode="w")
        idx.refresh()
        self.assertNotIn("P1", idx.prompts)
        self.assertEqual(idx.prompts["P3"]["turn_origin"], "peer")

    def test_replaced_file_new_inode(self):
        self.write([_user("P1", "one", turnOrigin="human")])
        idx = TranscriptIndex(self.path)
        idx.refresh()
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            f.write(json.dumps(_user("P1", "one", turnOrigin="human")) + "\n" +
                    json.dumps(_user("P9", "nine", turnOrigin="human")) + "\n" + "x" * 10 + "\n")
        os.replace(tmp, self.path)
        idx.refresh()
        self.assertIn("P9", idx.prompts)

    def test_huge_line(self):
        self.write([_user("P1", "y" * 3_000_000, turnOrigin="human")])
        idx = TranscriptIndex(self.path)
        idx.refresh()
        self.assertEqual(len(idx.prompts["P1"]["text"]), 3_000_000)


class RenderTest(unittest.TestCase):
    def test_fence_neutralises_backticks(self):
        text = "here ```` and ``````` fences"
        self.assertEqual(render.fence_for(text), "`" * 8)
        self.assertEqual(render.fence_for("plain"), "````")

    def test_clean_name(self):
        self.assertEqual(render.clean_name("a/b:c*d?e#f^g[h]i|j", 100), "a b c d e f g h i j")
        self.assertEqual(render.clean_name("...hidden", 100), "hidden")
        self.assertLessEqual(len(render.clean_name("ä" * 200, 50).encode()), 50)
        self.assertEqual(render.clean_name("Café", 50), "Café")  # NFC

    def test_yaml_escapes(self):
        self.assertEqual(render._yaml("a b\"c"), '"a\\u2028b\\"c"')

    def _p(self, **kw):
        base = dict(pid="x", ts=1793450000.0, text="hi", origin_class="human", origin_kind=None, prompt_source=None,
                    status="completed", duration_s=3.0, tool_calls=2, files_changed='["/a/b.py"]',
                    likely_correction=0, redactions=0, flags="[]", capture_source="hook")
        base.update(kw)
        return base

    def test_prompt_with_markdown_is_fenced(self):
        out = render.render_prompt(self._p(text="# heading\n[[link]] #tag\n---\n```py\nx\n```"), 1)
        self.assertIn("````text\n# heading", out)
        self.assertTrue(out.rstrip().endswith("^p1"))

    def test_long_prompt_collapsed(self):
        out = render.render_prompt(self._p(text="z" * 5000), 2)
        self.assertIn("> [!note]- Long prompt (5,000 characters)", out)

    def test_agent_prompt_callout(self):
        out = render.render_prompt(self._p(origin_class="agent", origin_kind="peer"), 3)
        self.assertTrue(out.startswith("> [!quote]-"))
        self.assertIn("#agent-written", out)


class InstallTest(unittest.TestCase):
    def test_add_remove_roundtrip_preserves_others(self):
        theirs = {"type": "command", "command": "/usr/local/bin/other-hook"}
        s = {"model": "opus", "hooks": {"Stop": [{"hooks": [theirs]}], "PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}]}}
        added = install.add_hooks(s)
        self.assertTrue(install.has_our_hooks(added))
        self.assertEqual(install.add_hooks(added), added)  # idempotent
        self.assertEqual(added["hooks"]["Stop"][0], {"hooks": [theirs]})
        self.assertEqual(install.remove_hooks(added), s)
        self.assertEqual(install.remove_hooks({"a": 1}), {"a": 1})

    def test_remove_drops_empty_hooks_key(self):
        self.assertEqual(install.remove_hooks(install.add_hooks({"x": 1})), {"x": 1})

    def test_hook_entry_shape(self):
        e = install.our_entry()
        self.assertTrue(e["async"])
        self.assertEqual(e["timeout"], 2)
        self.assertTrue(e["command"].endswith(">/dev/null 2>&1 || true"))
        self.assertIn("-I -S", e["command"])

    def test_edit_settings_file(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "settings.json")
        original = {"permissions": {"allow": ["Bash(x *)"]}, "env_note": "… unicode"}
        with open(path, "w") as f:
            f.write(json.dumps(original, indent=2, ensure_ascii=False) + "\n")
        before, after, bak = install.edit_settings(
            install.add_hooks, lambda b, n: install.has_our_hooks(n) and install.remove_hooks(n) == b, path=path)
        self.assertTrue(os.path.exists(bak))
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("… unicode", raw)  # not \u-escaped, like Claude Code writes it
        self.assertTrue(raw.endswith("}\n"))
        install.edit_settings(install.remove_hooks, lambda b, n: not install.has_our_hooks(n), path=path)
        with open(path) as f:
            self.assertEqual(json.load(f), original)

    def test_refuses_symlink(self):
        d = tempfile.mkdtemp()
        real = os.path.join(d, "real.json")
        with open(real, "w") as f:
            f.write("{}")
        link = os.path.join(d, "settings.json")
        os.symlink(real, link)
        with self.assertRaises(RuntimeError):
            install.edit_settings(install.add_hooks, lambda b, n: True, path=link)


if __name__ == "__main__":
    unittest.main()
