"""Hermes Agent as a terminal agent: its SQLite sessions read as transcripts, session discovery by folder, the launch
command and note, and an empty composer with a greyed suggestion read as empty. Run: python3 -m unittest tests.test_hermes
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="sbhermes-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["HERMES_HOME"] = os.path.join(TMP, "hermes")
os.environ["SB_HERMES_BIN"] = "/bin/true"  # "installed"
os.environ["SB_CONFIG"] = os.path.join(TMP, "accounts.json")
os.makedirs(os.environ["HERMES_HOME"])
json.dump({"accounts": [{"name": "hermes", "kind": "hermes", "config_dir": os.environ["HERMES_HOME"]}]},
          open(os.environ["SB_CONFIG"], "w"))

from switchboard import discovery, hermes, shared, terminals, transcript  # noqa: E402

DB = os.path.join(os.environ["HERMES_HOME"], "state.db")
CWD = os.path.realpath(TMP)


def make_db(t0):
    c = sqlite3.connect(DB)
    c.executescript("""
      CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, started_at REAL, title TEXT);
      CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
        tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, token_count INTEGER);""")
    c.execute("INSERT INTO sessions VALUES ('local-old', 'tui', 'm1', ?, ?, 'old')", (CWD, t0 - 3600))
    c.execute("INSERT INTO sessions VALUES ('local-abc', 'tui', 'qwen3-coder:30b', ?, ?, 'probe')", (CWD, t0))
    rows = [("user", "What is the codeword?", None, t0 + 1, 10),
            ("assistant", "", json.dumps([{"function": {"name": "terminal", "arguments": json.dumps({"command": "ls -la"})}}]), t0 + 2, 5),
            ("tool", "file list", None, t0 + 3, 3),
            ("assistant", "BANANA-42", None, t0 + 4, 2)]
    for role, content, calls, ts, n in rows:
        c.execute("INSERT INTO messages(session_id, role, content, tool_calls, timestamp, token_count) VALUES (?,?,?,?,?,?)",
                  ("local-abc", role, content, calls, ts, n))
    c.commit()
    c.close()


class HermesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t0 = time.time() - 60
        make_db(cls.t0)

    def test_hermes_account(self):
        acct = [a for a in discovery.accounts() if a["kind"] == "hermes"]
        self.assertEqual([(a["name"], a["config_dir"]) for a in acct], [("hermes", os.environ["HERMES_HOME"])])
        self.assertIn("hermes", terminals.PROVIDERS)
        self.assertEqual(discovery.transcript_format("hermes"), "hermes")  # replies confirmed from state.db
        self.assertEqual(discovery.account_env("hermes", "hermes")["HERMES_HOME"], os.environ["HERMES_HOME"])

    def test_session_reads_like_a_transcript(self):
        path = discovery.transcript_path("hermes", "local-abc")
        self.assertEqual(path, f"hermes:{DB}#local-abc")
        self.assertIsNone(discovery.transcript_path("hermes", "local-nope"))
        chat = transcript.read_chat(path, "hermes", 10)
        self.assertEqual([(e["role"], e["text"]) for e in chat["entries"]],
                         [("user", "What is the codeword?"), ("tool", "terminal: ls -la"), ("assistant", "BANANA-42")])
        newer = transcript.read_chat(path, "hermes", 10, after=chat["entries"][0]["id"])
        self.assertEqual([e["text"] for e in newer["entries"]], ["terminal: ls -la", "BANANA-42"])
        older = transcript.read_chat(path, "hermes", 1)
        self.assertEqual(([e["text"] for e in older["entries"]], older["before"] is not None), (["BANANA-42"], True))
        self.assertEqual(transcript.last_reply(path, "hermes")[0], "BANANA-42")
        self.assertEqual(discovery.last_model(path, "hermes"), "qwen3-coder:30b")
        self.assertTrue(discovery.transcript_has_user_text(path, "hermes", "What is the codeword?", self.t0))
        self.assertFalse(discovery.transcript_has_user_text(path, "hermes", "never said", self.t0))
        self.assertEqual(transcript.usage_stats(path, "hermes", self.t0)["tokens_24h"], 20)
        self.assertEqual([h["text"] for h in shared.search([("x", path, "hermes")], "codeword")], ["What is the codeword?"])
        self.assertEqual(shared.last_exchange(path, "hermes"), ("What is the codeword?", "BANANA-42"))

    def test_new_session_found_by_folder_and_start_time(self):
        self.assertEqual(hermes.new_session(CWD, self.t0), "local-abc")
        self.assertIsNone(hermes.new_session(CWD, self.t0, taken={"local-abc"}))
        self.assertIsNone(hermes.new_session("/elsewhere", self.t0))

    def test_launch_command_and_note(self):
        a = {"id": "h1", "provider": "hermes", "account": "hermes", "session_id": None, "model": "qwen3-coder:30b",
             "effort": "high", "project": "p", "cwd": CWD, "permission_mode": None}
        self.assertEqual(terminals.launch_argv(a), ["/bin/true", "chat", "-m", "qwen3-coder:30b", "--reasoning", "high"])
        self.assertEqual(terminals.launch_argv({**a, "session_id": "local-abc", "model": None, "effort": None})[1:],
                         ["chat", "--resume", "local-abc"])
        self.assertNotIn("--yolo", terminals.launch_argv(a))  # tools ask first unless the owner chose otherwise
        self.assertIn("--yolo", terminals.launch_argv({**a, "permission_mode": "bypassPermissions"}))
        env = terminals.launch_env(a)
        self.assertIn("sb context", env["HERMES_EPHEMERAL_SYSTEM_PROMPT"])
        self.assertEqual(env["SB_AGENT"], "h1")

    def test_empty_composer_with_a_suggestion(self):
        def term(composer, col, footer="  ☤ qwen3-coder:30b · ctx -- · 5s  / commands · @ file"):
            t = terminals.Term("h", {})
            t.provider = "hermes"
            t.screen.feed(("☤ Hermes\r\nBANANA-42\r\n" + "─" * 40 + "\r\n" + composer + "\r\n" + "─" * 40 + "\r\n" +
                           footer + f"\x1b[4;{col + 1}H").encode())
            return t.view()
        self.assertEqual(term("❯ Find and fix a failing test", 2), (True, False, ""))
        self.assertEqual(term("❯ What is the codeword?", 23), (True, False, "What is the codeword?"))
        self.assertTrue(term("❯ Reply PONG", 2, "  ☤ qwen3-coder:30b · 27s · ⏱ 2s  enter interrupt · tab queue")[1])


if __name__ == "__main__":
    unittest.main()
