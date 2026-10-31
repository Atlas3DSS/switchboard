"""The built-in agent and API accounts against a fake OpenAI-compatible endpoint (tests/fake_openai.py)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="sbagent-")
os.environ["SB_STATE"] = os.path.join(TMP, "state")
os.environ["SB_CONFIG"] = os.path.join(TMP, "accounts.json")
os.environ["SB_WORKSPACE"] = os.path.join(TMP, "ws")

from switchboard import config, discovery, transcript  # noqa: E402
sys.path.insert(0, HERE)
import fake_openai  # noqa: E402

SRV, URL = fake_openai.serve()


def run_turn(text, *extra, sid="s-1"):
    env = {**os.environ, "SB_API_BASE": URL, "SB_API_KEY": "test-key", "SB_API_MODEL": "fake-small",
           "PYTHONPATH": ROOT, "SB_STATE": os.path.dirname(discovery.API_SESSIONS)}
    p = subprocess.run([sys.executable, "-m", "switchboard.agent", "-p", *extra, "--session-id", sid], input=text,
                       capture_output=True, text=True, env=env, cwd=TMP, timeout=60)
    events = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    return events, p.stderr


class AgentTests(unittest.TestCase):
    def test_headless_turn_streams_claude_style_events(self):
        events, err = run_turn("hello there", sid="s-hello")
        self.assertEqual(events[0]["type"], "system", err)
        self.assertEqual(events[0]["session_id"], "s-hello")
        res = events[-1]
        self.assertEqual((res["type"], res["is_error"]), ("result", False), res)
        self.assertEqual(res["result"], "echo: hello there (n=2)")

    def test_tool_call_runs_and_result_goes_back(self):
        events, err = run_turn("run: echo tool-output-42", sid="s-tool")
        tools = [c for e in events if e["type"] == "assistant" for c in e["message"]["content"] if c["type"] == "tool_use"]
        self.assertEqual(tools[0]["name"], "bash", err)
        self.assertEqual(events[-1]["result"], "tool said: tool-output-42")

    def test_transcript_is_readable_and_resume_keeps_history(self):
        run_turn("first ask", sid="s-res")
        path = discovery.transcript_path("api", "s-res")
        self.assertTrue(path and path.endswith("s-res.jsonl"))
        self.assertTrue(discovery.transcript_has_user_text(path, "api", "first ask", 0))
        events, _ = run_turn("second ask", "--resume", "s-res", sid="s-res")
        self.assertIn("(n=4)", events[-1]["result"])  # system + 2 earlier messages + this one
        text, _ = transcript.last_reply(path, "api")
        self.assertEqual(text, "echo: second ask (n=4)")
        self.assertEqual(discovery.last_model(path, "api"), "fake-small")

    def test_sb_only_tools_refuse_bash(self):
        events, _ = run_turn("run: echo nope", "--tools", "sb", sid="s-sb")
        self.assertIn("no tool named bash", json.dumps(events))

    def test_bad_key_is_a_failed_result(self):
        env_key = os.environ.get("SB_API_KEY")
        p = subprocess.run([sys.executable, "-m", "switchboard.agent", "-p"], input="hi", capture_output=True,
                           text=True, cwd=TMP, timeout=60,
                           env={**os.environ, "SB_API_BASE": URL, "SB_API_KEY": "wrong", "SB_API_MODEL": "fake-small",
                                "PYTHONPATH": ROOT})
        res = json.loads(p.stdout.splitlines()[-1])
        self.assertTrue(res["is_error"])
        self.assertIn("401", res["result"])
        self.assertIsNone(env_key)


class SetupTests(unittest.TestCase):
    def test_test_api_lists_models_then_chats(self):
        r = config.test_api({"preset": "custom", "base_url": URL, "api_key": "test-key"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["models"], ["fake-large", "fake-small"])
        r = config.test_api({"preset": "custom", "base_url": URL, "api_key": "test-key", "model": "fake-small"})
        self.assertTrue(r["ok"], r)
        self.assertIn("echo:", r["reply"])
        bad = config.test_api({"preset": "custom", "base_url": URL, "api_key": "nope", "model": "fake-small"})
        self.assertFalse(bad["ok"])
        self.assertIn("401", bad["error"])

    def test_save_keeps_keys_private_and_blank_key_keeps_old(self):
        config.set_accounts([{"name": "Local LLM", "kind": "api", "preset": "custom", "base_url": URL,
                              "api_key": "test-key", "model": "fake-small"},
                             {"name": "claude", "kind": "claude"}])
        self.assertEqual(oct(os.stat(config.PATH).st_mode & 0o777), "0o600")
        a = config.account("local-llm")
        self.assertEqual(a["api_key"], "test-key")
        self.assertNotIn("api_key", config.public(a))
        config.set_accounts([{"name": "local-llm", "kind": "api", "preset": "custom", "base_url": URL,
                              "api_key": "", "model": "fake-large"}])
        self.assertEqual(config.account("local-llm")["api_key"], "test-key")
        env = discovery.account_env("local-llm", "api")
        self.assertEqual((env["SB_API_BASE"], env["SB_API_MODEL"]), (URL, "fake-large"))

    def test_api_account_needs_a_model_and_a_url(self):
        with self.assertRaises(ValueError):
            config.normalize({"name": "x", "kind": "api", "preset": "custom", "base_url": URL})
        with self.assertRaises(ValueError):
            config.normalize({"name": "x", "kind": "api", "preset": "custom", "base_url": "ftp://x", "model": "m"})
        self.assertEqual(config.normalize({"kind": "api", "preset": "openrouter", "model": "m"})["base_url"],
                         "https://openrouter.ai/api/v1")


if __name__ == "__main__":
    unittest.main()
