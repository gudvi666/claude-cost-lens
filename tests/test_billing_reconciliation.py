import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import LOCAL_ZONE, UsageIndex


class BillingReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'projects' / 'fixture'
        self.project.mkdir(parents=True)

    def request(self, rid='r1', hour=12, input=10, output=5):
        return {'type': 'assistant', 'requestId': rid,
                'timestamp': f'2026-10-16T{hour:02d}:00:00Z',
                'message': {'id': rid, 'model': 'model', 'content': [],
                            'usage': {'input_tokens': input, 'output_tokens': output}}}

    def queue(self, hour):
        return {'type': 'queue-operation', 'timestamp': f'2026-10-16T{hour:02d}:00:00Z'}

    def state(self, cost=7, input=10, output=5, start=1):
        return {'type': 'cost-state', 'startTime': start, 'totalCostUSD': cost,
                'modelUsage': {'model': {'costUSD': cost, 'inputTokens': input,
                                        'outputTokens': output, 'thinkingTokens': 0,
                                        'cacheReadInputTokens': 0, 'cacheCreationInputTokens': 0}}}

    def write(self, sid, rows):
        (self.project / f'{sid}.jsonl').write_text('\n'.join(
            json.dumps(dict(row, sessionId=row.get('sessionId', sid))) for row in rows))

    def data(self):
        d = UsageIndex(self.root).summary()
        self.assertAlmostEqual(d['totals']['cost'], sum(r['cost'] for r in d['models']))
        self.assertAlmostEqual(d['totals']['cost'], sum(r['cost'] for r in d['projects']))
        self.assertAlmostEqual(d['totals']['cost'], sum(r['cost'] for r in d['daily']))
        return d, {r['id']: r for r in d['sessions']}

    def test_identical_history_and_ledger_cost_count_once(self):
        self.write('original', [self.queue(11), self.request(), self.state()])
        self.write('fork', [self.request(), self.queue(13), self.state()])
        d, s = self.data()
        self.assertEqual(d['totals']['cost'], 7)
        self.assertEqual(s['fork']['cost'], 0)
        self.assertEqual(s['fork']['native_cost'], 7)
        self.assertEqual(s['fork']['inherited_cost'], 7)
        self.assertEqual(d['totals']['native_tokens']['inputTokens'], 10)

    def test_fork_counts_new_charges_and_native_tokens(self):
        self.write('original', [self.queue(11), self.request(), self.state()])
        self.write('fork', [self.request(), self.queue(13), self.request('r2', 14, input=4),
                            self.state(cost=10, input=14, output=10)])
        d, s = self.data()
        self.assertEqual(d['totals']['cost'], 10)
        self.assertEqual(s['fork']['cost'], 3)
        self.assertEqual(s['fork']['native_tokens']['inputTokens'], 4)

    def test_parallel_branches_are_both_retained(self):
        self.write('original', [self.queue(11), self.request(), self.state()])
        self.write('a', [self.request(), self.queue(13), self.request('r2', 14, input=4),
                         self.state(cost=10, input=14, output=10)])
        self.write('b', [self.request(), self.queue(13), self.request('r3', 15, input=8),
                         self.state(cost=12, input=18, output=10)])
        d, s = self.data()
        self.assertEqual(d['totals']['cost'], 15)
        self.assertEqual(s['a']['cost'], 3)
        self.assertEqual(s['b']['cost'], 5)

    def test_parent_later_activity_does_not_inflate_fork_baseline(self):
        self.write('original', [self.queue(11), self.request(), self.state(),
                                self.request('later', 16), self.state(cost=15, input=20, output=10)])
        self.write('fork', [self.request(), self.queue(13), self.request('r2', 14, input=4),
                            self.state(cost=10, input=14, output=10)])
        d, s = self.data()
        self.assertEqual(s['fork']['inherited_cost'], 7)
        self.assertEqual(d['totals']['cost'], 18)

    def test_old_snapshot_before_additional_inherited_requests_is_not_a_baseline(self):
        self.write('original', [self.queue(11), self.request(), self.state(),
                                self.request('unsaved', 13), self.request('later', 16),
                                self.state(cost=15, input=30, output=15)])
        self.write('fork', [self.request(), self.request('unsaved', 13), self.queue(14),
                            self.request('new', 15), self.state(cost=20, input=40, output=20)])
        _, s = self.data()
        self.assertEqual(s['fork']['cost_status'], 'unreconciled')
        self.assertIsNone(s['fork']['inherited_cost'])

    def test_incomparable_ancestors_are_not_resolved_by_largest_price(self):
        self.write('a', [self.queue(10), self.request(), self.state(cost=5, input=10, output=1)])
        self.write('b', [self.queue(11), self.request(), self.state(cost=6, input=1, output=10)])
        self.write('fork', [self.request(), self.queue(13), self.request('new', 14),
                            self.state(cost=20, input=20, output=20)])
        _, s = self.data()
        self.assertEqual(s['fork']['cost_status'], 'unreconciled')
        self.assertIn('多条', s['fork']['cost_reconciliation_reason'])

    def test_advisor_fee_uses_incremental_model_ledger(self):
        def advisor_request(rid, hour):
            row = self.request(rid, hour)
            row['message']['usage']['iterations'] = [
                {'type': 'advisor_message', 'model': 'advisor', 'input_tokens': 10, 'output_tokens': 5}]
            return row
        def ledger(main_cost, advisor_cost, input):
            row = self.state(cost=main_cost, input=input, output=input//2)
            row['modelUsage']['advisor'] = dict(row['modelUsage']['model'], costUSD=advisor_cost)
            row['totalCostUSD'] = main_cost + advisor_cost
            return row
        self.write('original', [self.queue(11), advisor_request('r1', 12), ledger(7, 2, 10)])
        self.write('fork', [advisor_request('r1', 12), self.queue(13),
                            advisor_request('r2', 14), ledger(10, 3, 20)])
        d, s = self.data()
        self.assertEqual(s['fork']['cost'], 4)
        self.assertEqual(s['fork']['advisor_cost'], 1)
        self.assertEqual(d['totals']['cost'], 13)

    def test_missing_baseline_is_not_zero_or_full_cumulative_charge(self):
        self.write('original', [self.queue(11), self.request(), self.request('not-in-fork'),
                                self.state(cost=15, input=20, output=10)])
        self.write('fork', [self.request(), self.queue(13), self.request('r2', 14),
                            self.state(cost=20, input=30, output=15)])
        d, s = self.data()
        self.assertEqual(s['fork']['cost_status'], 'unreconciled')
        self.assertIsNone(s['fork']['cost'])
        self.assertEqual(s['fork']['native_cost'], 20)
        self.assertEqual(d['cost_unreconciled_native_sum'], 20)
        self.assertEqual(d['totals']['cost'], 15)

    def test_equal_amount_in_independent_ledgers_is_not_deduplicated(self):
        self.write('a', [self.queue(11), self.request('a'), self.state(start=1)])
        self.write('b', [self.queue(11), self.request('b'), self.state(start=2)])
        d, _ = self.data()
        self.assertEqual(d['totals']['cost'], 14)

    def test_same_start_without_request_lineage_is_not_assumed_identical(self):
        self.write('a', [self.queue(11), self.request('a'), self.state()])
        self.write('b', [self.queue(13), self.request('b', 14), self.state()])
        _, s = self.data()
        self.assertEqual(s['b']['cost_status'], 'unreconciled')
        self.assertIsNone(s['b']['inherited_cost'])

    def test_copied_lines_with_original_session_id_use_physical_history(self):
        self.write('original', [self.queue(11), self.request(), self.state()])
        copied = dict(self.request(), sessionId='original')
        self.write('fork', [copied, self.queue(13), self.request('r2', 14),
                            self.state(cost=10, input=20, output=10)])
        d, s = self.data()
        self.assertEqual(s['fork']['cost'], 3)
        self.assertEqual(d['totals']['cost'], 10)

    def test_native_snapshot_includes_calls_absent_from_transcript(self):
        self.write('original', [self.queue(11), self.request(), self.state(input=25)])
        d, s = self.data()
        self.assertEqual(d['totals']['tokens']['inputTokens'], 10)
        self.assertEqual(d['totals']['native_tokens']['inputTokens'], 25)
        self.assertEqual(s['original']['native_snapshot_tokens']['inputTokens'], 25)

    def test_missing_native_token_fields_are_not_claimed_zero_usage(self):
        state = self.state()
        del state['modelUsage']['model']['inputTokens']
        self.write('original', [self.request(), state])
        d, s = self.data()
        self.assertIsNone(s['original']['native_tokens'])
        self.assertEqual(d['native_token_session_count'], 0)

    def test_missing_baseline_token_field_does_not_become_zero(self):
        before = self.state()
        del before['modelUsage']['model']['cacheReadInputTokens']
        self.write('original', [self.queue(11), self.request(), before])
        self.write('fork', [self.request(), self.queue(13), self.request('new', 14),
                            self.state(cost=10, input=20, output=10)])
        _, s = self.data()
        self.assertEqual(s['fork']['cost'], 3)
        self.assertIsNone(s['fork']['native_tokens'])

    def test_repeated_rescans_do_not_compound_inherited_deduction(self):
        self.write('original', [self.queue(11), self.request(), self.state()])
        self.write('fork', [self.request(), self.queue(13), self.request('r2', 14),
                            self.state(cost=10, input=20, output=10)])
        index = UsageIndex(self.root)
        self.assertEqual(index.summary()['totals']['cost'], 10)
        self.assertEqual(index.snapshot()['totals']['cost'], 10)
        self.assertEqual(index.snapshot()['totals']['cost'], 10)

    def test_today_uses_real_calendar_day(self):
        self.write('original', [self.request(), self.state()])
        d, _ = self.data()
        self.assertEqual(d['periods']['day']['reference'], datetime.now(LOCAL_ZONE).date().isoformat())
        if datetime.now(LOCAL_ZONE).date().isoformat() != '2026-10-16':
            self.assertEqual(d['periods']['day']['cost'], 0)


if __name__ == '__main__':
    unittest.main()
