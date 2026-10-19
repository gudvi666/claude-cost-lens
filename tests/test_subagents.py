import json
import tempfile
import unittest
from pathlib import Path

from app import UsageIndex


class SubagentModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "fixture"
        self.project.mkdir(parents=True)

    def record(self, request, model="claude-opus-5-5", **extra):
        return dict({"type": "assistant", "sessionId": "session-1",
                     "timestamp": "2026-10-19T12:00:00Z", "requestId": request,
                     "message": {"model": model, "content": [],
                                 "usage": {"input_tokens": 100, "output_tokens": 10}}}, **extra)

    def write(self, name, records):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(map(json.dumps, records)))

    def snapshot(self):
        data = UsageIndex(self.root).summary()
        for row in data["sessions"]:
            for model, count in row["models"].items():
                self.assertEqual(count, row["primary_models"].get(model, 0) +
                                 row["subagent_models"].get(model, 0))
        return data

    def test_real_multi_model_conversation_remains_primary(self):
        self.write("session-1.jsonl", [self.record("main-1"),
                   self.record("main-2", "claude-sonnet-5-5")])
        row = self.snapshot()["sessions"][0]
        self.assertEqual(row["primary_models"], {"claude-opus-5-5": 1, "claude-sonnet-5-5": 1})
        self.assertEqual(row["subagent_models"], {})

    def test_agent_metadata_in_flat_transcript_identifies_subagent(self):
        self.write("session-1.jsonl", [self.record("main"),
                   self.record("child", "claude-haiku-5-5", agentId="a123", isSidechain=True)])
        data = self.snapshot()
        row = data["sessions"][0]
        self.assertEqual(row["primary_models"], {"claude-opus-5-5": 1})
        self.assertEqual(row["subagent_models"], {"claude-haiku-5-5": 1})
        self.assertEqual(data["usage_event_count"], 2)
        self.assertEqual(data["totals"]["tokens"]["inputTokens"], 200)

    def test_subagent_directory_identifies_records_without_agent_id(self):
        self.write("session-1.jsonl", [self.record("main")])
        self.write("session-1/subagents/agent-a123.jsonl", [self.record("child", "claude-haiku-5-5")])
        row = self.snapshot()["sessions"][0]
        self.assertEqual(row["primary_models"], {"claude-opus-5-5": 1})
        self.assertEqual(row["subagent_models"], {"claude-haiku-5-5": 1})

    def test_same_model_can_have_both_roles_and_streaming_still_counts_once(self):
        self.write("session-1.jsonl", [self.record("main")])
        child = self.record("child", agentId="a123", isSidechain=True)
        final = self.record("child", agentId="a123", isSidechain=True)
        final["message"]["usage"]["output_tokens"] = 30
        self.write("session-1/subagents/agent-a123.jsonl", [child, final, final])
        data = self.snapshot()
        row = data["sessions"][0]
        self.assertEqual(row["primary_models"], {"claude-opus-5-5": 1})
        self.assertEqual(row["subagent_models"], {"claude-opus-5-5": 1})
        self.assertEqual(row["models"], {"claude-opus-5-5": 2})
        self.assertEqual(data["usage_event_count"], 2)
        self.assertEqual(data["totals"]["tokens"]["outputTokens"], 40)
        self.assertAlmostEqual(data["totals"]["estimated_cost"], 0.0016)

    def test_fork_copy_does_not_change_original_request_role(self):
        queue = {"type": "queue-operation", "sessionId": "session-1", "timestamp": "2026-10-19T11:00:00Z"}
        self.write("session-1.jsonl", [queue, self.record("main")])
        copied = self.record("main", sessionId="fork", agentId="a123", isSidechain=True)
        self.write("fork/subagents/agent-a123.jsonl", [copied,
                   dict(queue, sessionId="fork", timestamp="2026-10-19T13:00:00Z")])
        data = self.snapshot()
        rows = {row["id"]: row for row in data["sessions"]}
        self.assertEqual(rows["session-1"]["primary_models"], {"claude-opus-5-5": 1})
        self.assertEqual(rows["session-1"]["subagent_models"], {})
        self.assertEqual(rows["fork"]["models"], {})
        self.assertEqual(data["usage_event_count"], 1)

    def test_sidechain_flag_alone_does_not_prove_subagent_role(self):
        self.write("session-1.jsonl", [self.record("main", isSidechain=True, agentId="")])
        row = self.snapshot()["sessions"][0]
        self.assertEqual(row["subagent_models"], {})
        self.assertEqual(row["primary_models"], {"claude-opus-5-5": 1})


if __name__ == "__main__":
    unittest.main()
