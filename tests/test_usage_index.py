import json
import os
import tempfile
import unittest
from pathlib import Path

from app import UsageIndex, TOKEN_FIELDS


class NativeCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".claude"
        self.project = self.root / "projects" / "fixture"
        self.project.mkdir(parents=True)

    def transcript(self, sid="session-1", cost=None):
        records = [{"type": "assistant", "sessionId": sid,
                    "timestamp": "2026-10-16T12:00:00Z", "requestId": "request-1",
                    "message": {"model": "claude-opus-5-5", "content": [],
                                "usage": {"input_tokens": 256, "output_tokens": 84276,
                                          "output_tokens_details": {"thinking_tokens": 24123},
                                          "cache_read_input_tokens": 20827092,
                                          "cache_creation_input_tokens": 217262}}}]
        if cost is not None:
            records.append({"type": "cost-state", "sessionId": sid, "totalCostUSD": cost})
        (self.project / (sid + ".jsonl")).write_text("\n".join(map(json.dumps, records)))

    def config(self, sid, cost):
        (self.root / ".claude.json").write_text(json.dumps({"projects": {
            "/fixture": {"lastSessionId": sid, "lastCost": cost,
                         "lastModelUsage": {"claude-opus-5-5": {"costUSD": cost}}}}}))

    def active(self, sid="session-1", pid=None):
        directory = self.root / "sessions"
        directory.mkdir()
        (directory / "fixture.json").write_text(json.dumps({
            "sessionId": sid, "pid": os.getpid() if pid is None else pid, "status": "idle"}))

    def session(self):
        return UsageIndex(self.root).summary()["sessions"][0]

    def test_native_amount_is_preserved_and_takes_priority(self):
        self.transcript(cost=7.590058400000003)
        self.config("session-1", 99)
        row = self.session()
        self.assertEqual(row["cost"], 7.590058400000003)
        self.assertEqual(row["cost_source"], "cost-state")

    def test_genuine_zero_and_missing_amount_remain_distinct(self):
        self.transcript(cost=0)
        row = self.session()
        self.assertTrue(row["cost_available"])
        self.assertEqual(row["cost"], 0)
        self.transcript()
        row = self.session()
        self.assertFalse(row["cost_available"])
        self.assertIsNone(row["cost"])
        self.assertEqual(row["cost_status"], "missing")

    def test_open_session_without_amount_is_pending(self):
        self.transcript()
        self.active()
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["sessions"][0]["cost_status"], "pending")
        self.assertEqual(data["cost_pending_session_count"], 1)
        self.assertEqual(data["totals"]["cost"], 0)

    def test_open_session_with_amount_is_a_saved_snapshot(self):
        self.transcript(cost=2.5)
        self.active()
        self.assertEqual(self.session()["cost_status"], "snapshot")

    def test_stale_process_metadata_is_not_treated_as_open(self):
        self.transcript()
        self.active(pid=2147483647)
        self.assertEqual(self.session()["cost_status"], "missing")

    def test_project_config_requires_an_exact_session_match(self):
        self.transcript()
        self.config("other-session", 8.25)
        self.assertIsNone(self.session()["cost"])
        self.config("session-1", 8.25)
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["sessions"][0]["cost"], 8.25)
        self.assertEqual(data["sessions"][0]["cost_source"], "project-config")
        self.assertEqual(data["daily"][0]["cost"], 8.25)


class TokenAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".claude"
        self.project = self.root / "projects" / "fixture"
        self.project.mkdir(parents=True)

    def write(self, sid, records):
        rows = [dict(record, sessionId=sid) for record in records]
        (self.project / (sid + ".jsonl")).write_text("\n".join(map(json.dumps, rows)))

    def assistant(self, output=50, **extra):
        row = {"type": "assistant", "timestamp": "2026-10-16T12:00:00Z",
               "requestId": "request-1", "uuid": "block-1", "apiBlockIndex": 0,
               "message": {"id": "message-1", "model": "claude-opus-5-5", "content": [],
                           "usage": {"input_tokens": 2, "output_tokens": output,
                                     "cache_read_input_tokens": 1000,
                                     "cache_creation_input_tokens": 100,
                                     "output_tokens_details": {"thinking_tokens": 20}}}}
        row.update(extra)
        return row

    def assert_consistent(self, data):
        for key in TOKEN_FIELDS:
            total = data["totals"]["tokens"][key]
            self.assertEqual(total, data["totals"]["main_tokens"][key] + data["totals"]["advisor_tokens"][key])
            self.assertEqual(total, sum(x["tokens"][key] for x in data["daily"]))
            self.assertEqual(total, sum(x["tokens"][key] for x in data["projects"]))
            self.assertEqual(total, sum(x["tokens"][key] for x in data["models"]))
            self.assertEqual(total, sum(x["tokens"][key] for x in data["sessions"]))
            for session in data["sessions"]:
                self.assertEqual(session["tokens"][key], session["main_tokens"][key] + session["advisor_tokens"][key])

    def test_streaming_blocks_keep_complete_cumulative_usage_once(self):
        partial = self.assistant(output=3)
        partial["message"]["usage"].pop("output_tokens_details")
        final = self.assistant(output=250, uuid="block-2", apiBlockIndex=1)
        # A copied or out-of-order partial block cannot overwrite final usage.
        self.write("session-1", [partial, final, partial, final])
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["usage_event_count"], 1)
        self.assertEqual(data["totals"]["tokens"]["outputTokens"], 250)
        self.assertEqual(data["totals"]["tokens"]["thinkingTokens"], 20)
        self.assert_consistent(data)

    def test_copied_history_belongs_to_original_session_even_if_scanned_last(self):
        original = self.assistant(cwd="/original-project")
        copy = self.assistant(cwd="/fork-project")
        queue = lambda time: {"type": "queue-operation", "timestamp": time}
        self.write("z-original", [queue("2026-10-16T11:00:00Z"), original])
        self.write("a-fork", [copy, queue("2026-10-16T13:00:00Z")])
        data = UsageIndex(self.root).summary()
        rows = {x["id"]: x for x in data["sessions"]}
        self.assertEqual(data["usage_event_count"], 1)
        self.assertEqual(data["shared_request_count"], 1)
        self.assertEqual(data["attribution_ambiguous_request_count"], 0)
        self.assertEqual(rows["z-original"]["tokens"]["cacheReadInputTokens"], 1000)
        self.assertEqual(rows["a-fork"]["tokens"]["cacheReadInputTokens"], 0)
        self.assertEqual(rows["a-fork"]["inherited_tokens"]["cacheReadInputTokens"], 1000)
        self.assertEqual(rows["a-fork"]["inherited_request_count"], 1)
        self.assert_consistent(data)

    def test_advisor_usage_on_later_block_is_counted_once_and_included_in_total(self):
        first = self.assistant(output=3, advisorModel="claude-fable-5-1")
        first["message"]["content"] = [{"type": "server_tool_use", "name": "advisor", "id": "advisor-1"}]
        final = self.assistant(output=250, uuid="block-2", apiBlockIndex=1)
        final["message"]["usage"]["iterations"] = [
            {"type": "message", "input_tokens": 2, "output_tokens": 250},
            {"type": "advisor_message", "model": "claude-fable-5-1", "input_tokens": 100, "output_tokens": 10},
            {"type": "advisor_message", "model": "claude-fable-5-1", "input_tokens": 150, "output_tokens": 20}]
        self.write("session-1", [first, final, final])
        data = UsageIndex(self.root).summary()
        row = data["sessions"][0]
        self.assertEqual(row["advisor_calls"], 2)
        self.assertEqual(row["advisor_tokens"]["inputTokens"], 250)
        self.assertEqual(row["tokens"]["outputTokens"], 280)
        self.assertEqual(row["main_tokens"]["outputTokens"], 250)
        self.assertEqual(data["totals"]["tokens"]["inputTokens"], 252)
        self.assertEqual(data["totals"]["tokens"]["outputTokens"], 280)
        self.assert_consistent(data)

    def advisor_record(self, model="claude-fable-5-1"):
        row = self.assistant(advisorModel=model)
        row["message"]["content"] = [{"type": "server_tool_use", "name": "advisor", "id": "advisor-1"}]
        row["message"]["usage"]["iterations"] = [
            {"type": "message", "input_tokens": 2, "output_tokens": 50},
            {"type": "advisor_message", "model": model, "input_tokens": 100, "output_tokens": 10}]
        return row

    def advisor_state(self, shared=False, extra_input=0):
        main = {"inputTokens": 2, "outputTokens": 50, "thinkingTokens": 20,
                "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 100, "costUSD": 5}
        advisor = {"inputTokens": 100 + extra_input, "outputTokens": 10, "costUSD": 2}
        if shared:
            main["inputTokens"] += 100
            main["outputTokens"] += 10
            main["costUSD"] = 7
        return {"type": "cost-state", "totalCostUSD": 7,
                "modelUsage": {"claude-opus-5-5": main} if shared else
                {"claude-opus-5-5": main, "claude-fable-5-1": advisor}}

    def test_session_includes_advisor_tokens_and_native_fee_exactly_once(self):
        self.write("session-1", [self.advisor_record(), self.advisor_state()])
        data = UsageIndex(self.root).summary()
        row = data["sessions"][0]
        self.assertEqual(row["tokens"]["inputTokens"], 102)
        self.assertEqual(row["tokens"]["outputTokens"], 60)
        self.assertEqual(row["main_tokens"]["inputTokens"], 2)
        self.assertEqual(row["advisor_tokens"]["inputTokens"], 100)
        self.assertEqual(row["advisor_cost"], 2)
        self.assertEqual(row["advisor_cost_status"], "recorded")
        self.assertEqual(row["cost"], 7)
        self.assertEqual(data["totals"]["cost"], 7)
        self.assertEqual(sum(x["cost"] for x in data["models"]), 7)
        self.assertEqual(sum(x["cost"] for x in data["projects"]), 7)
        self.assertEqual(sum(x["cost"] for x in data["daily"]), 7)
        self.assert_consistent(data)

    def test_shared_main_and_advisor_model_fee_is_not_falsely_attributed(self):
        self.write("session-1", [self.advisor_record("claude-opus-5-5"), self.advisor_state(shared=True)])
        data = UsageIndex(self.root).summary()
        row = data["sessions"][0]
        self.assertEqual(row["cost"], 7)
        self.assertFalse(row["advisor_cost_available"])
        self.assertIsNone(row["advisor_cost"])
        self.assertEqual(row["advisor_cost_status"], "shared_model")
        self.assert_consistent(data)

    def test_advisor_native_model_ledger_with_different_scope_is_labelled(self):
        self.write("session-1", [self.advisor_record(), self.advisor_state(extra_input=100)])
        data = UsageIndex(self.root).summary()
        row = data["sessions"][0]
        self.assertEqual(row["advisor_cost"], 2)
        self.assertEqual(row["advisor_cost_status"], "model_total")
        self.assertEqual(row["cost"], 7)
        self.assert_consistent(data)

    def test_advisor_tool_with_no_usage_reports_missing_instead_of_real_zero(self):
        row = self.advisor_record()
        row["message"]["usage"]["iterations"] = []
        self.write("session-1", [row])
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["sessions"][0]["advisor_usage_missing_calls"], 1)
        self.assertIsNone(data["sessions"][0]["advisor_cost"])
        self.assert_consistent(data)

    def test_forked_advisor_history_is_not_added_twice(self):
        queue = lambda time: {"type": "queue-operation", "timestamp": time}
        self.write("z-original", [queue("2026-10-16T11:00:00Z"), self.advisor_record()])
        self.write("a-fork", [self.advisor_record(), queue("2026-10-16T13:00:00Z")])
        data = UsageIndex(self.root).summary()
        rows = {x["id"]: x for x in data["sessions"]}
        self.assertEqual(data["totals"]["advisor_tokens"]["inputTokens"], 100)
        self.assertEqual(rows["z-original"]["tokens"]["inputTokens"], 102)
        self.assertEqual(rows["a-fork"]["tokens"]["inputTokens"], 0)
        self.assertEqual(rows["a-fork"]["inherited_tokens"]["inputTokens"], 102)
        self.assert_consistent(data)

    def test_advisor_configuration_does_not_count_as_usage(self):
        self.write("session-1", [self.assistant(advisorModel="claude-fable-5-1")])
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["sessions"][0]["advisor_calls"], 0)
        self.assertFalse(data["sessions"][0]["advisors"])
        self.assertEqual(sum(data["totals"]["advisor_tokens"].values()), 0)
        self.assert_consistent(data)

    def test_fallback_message_id_merges_blocks_but_keeps_other_requests(self):
        first = self.assistant(output=3)
        final = self.assistant(output=250, uuid="block-2")
        other = self.assistant(output=10, uuid="block-3")
        other["message"]["id"] = "message-2"
        for row in [first, final, other]:
            del row["requestId"]
        unknown = self.assistant(output=15)
        del unknown["requestId"], unknown["uuid"], unknown["message"]["id"]
        self.write("session-1", [first, final, other, unknown, unknown])
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["usage_event_count"], 4)
        self.assertEqual(data["totals"]["tokens"]["outputTokens"], 290)
        self.assert_consistent(data)

    def test_ambiguous_history_ownership_is_reported_without_double_counting(self):
        self.write("session-1", [self.assistant()])
        self.write("session-2", [self.assistant()])
        data = UsageIndex(self.root).summary()
        self.assertEqual(data["usage_event_count"], 1)
        self.assertEqual(data["attribution_ambiguous_request_count"], 1)
        self.assert_consistent(data)

    def test_empty_archive_has_zero_counts(self):
        data = UsageIndex(self.root / "missing").summary()
        self.assertEqual(data["usage_event_count"], 0)
        self.assertEqual(data["shared_request_count"], 0)


if __name__ == "__main__":
    unittest.main()
