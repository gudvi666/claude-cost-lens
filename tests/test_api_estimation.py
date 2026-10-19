import json
import tempfile
import unittest
from pathlib import Path

from app import UsageIndex, empty_tokens, estimate_api_cost


class APIPricingTests(unittest.TestCase):
    def tokens(self, **values):
        return dict(empty_tokens(), **values)

    def test_cache_ttls_and_thinking_are_not_charged_twice(self):
        result = estimate_api_cost('claude-opus-5-5', self.tokens(
            inputTokens=100, outputTokens=20, thinkingTokens=19,
            cacheReadInputTokens=1000, cacheCreationInputTokens=300),
            {'ephemeral_1h_input_tokens': 200, 'ephemeral_5m_input_tokens': 100})
        self.assertAlmostEqual(result['cost'], .0031)
        self.assertTrue(result['complete'])

    def test_haiku_tier_counts_cache_and_prices_each_request(self):
        short = self.tokens(cacheReadInputTokens=100_000, outputTokens=100)
        long = self.tokens(inputTokens=1, cacheReadInputTokens=100_000, outputTokens=100)
        self.assertAlmostEqual(estimate_api_cost('claude-haiku-5-5', short, {})['cost'], .00105)
        self.assertAlmostEqual(estimate_api_cost('claude-haiku-5-5', long, {})['cost'], .0052505)

    def test_fast_geography_and_search_fee(self):
        result = estimate_api_cost('claude-opus-5-5', self.tokens(inputTokens=1_000_000),
                                   {'speed': 'fast', 'inference_geo': 'us', 'web_search_requests': 2})
        self.assertAlmostEqual(result['cost'], 8.82)

    def test_unknown_model_is_not_free(self):
        result = estimate_api_cost('claude-opus-future', self.tokens(inputTokens=50), {})
        self.assertIsNone(result['cost'])
        self.assertFalse(result['complete'])

    def test_dated_model_id_and_default_cache_ttl(self):
        result = estimate_api_cost('claude-opus-5-5-20261001', self.tokens(cacheCreationInputTokens=1_000_000), {})
        self.assertEqual(result['cost'], 5)
        self.assertTrue(result['notes'])

    def test_fable_advisor_cache_rate(self):
        result = estimate_api_cost('claude-fable-5-1', self.tokens(inputTokens=1_000_000,
            outputTokens=1_000_000, cacheReadInputTokens=1_000_000), {})
        self.assertEqual(result['cost'], 60.25)

    def test_execution_time_missing_is_partial(self):
        result = estimate_api_cost('claude-opus-5-5', self.tokens(inputTokens=1_000_000),
                                   {'code_execution_requests': 1})
        self.assertEqual(result['cost'], 4)
        self.assertFalse(result['complete'])


class EstimateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'projects' / 'fixture'
        self.project.mkdir(parents=True)

    def request(self, rid='r1', model='claude-opus-5-5', amount=1_000_000):
        return {'type': 'assistant', 'requestId': rid, 'timestamp': '2026-10-19T01:00:00Z',
                'message': {'id': rid, 'model': model, 'content': [], 'usage': {
                    'input_tokens': amount, 'output_tokens': 0, 'speed': 'standard',
                    'inference_geo': 'global', 'service_tier': 'standard'}}}

    def write(self, sid, rows):
        (self.project / (sid + '.jsonl')).write_text('\n'.join(json.dumps(dict(row, sessionId=sid)) for row in rows))

    def data(self):
        data = UsageIndex(self.root).summary()
        for key in ('cost', 'estimated_cost', 'combined_cost'):
            for category in ('sessions', 'models', 'projects', 'daily'):
                self.assertAlmostEqual(data['totals'][key], sum(row.get(key) or 0 for row in data[category]))
        return data

    def test_copied_history_streaming_and_advisor_are_counted_once(self):
        row = self.request()
        row['message']['usage']['iterations'] = [{'type': 'advisor_message', 'model': 'claude-fable-5-1',
                                                  'input_tokens': 1_000_000, 'output_tokens': 0}]
        self.write('a', [row, row])
        self.write('b', [row])
        data = self.data()
        self.assertEqual(data['totals']['cost'], 0)
        self.assertEqual(data['totals']['estimated_cost'], 14)
        self.assertEqual(data['cost_estimated_session_count'], 1)
        self.assertEqual(sum(s['estimated_advisor_cost'] or 0 for s in data['sessions']), 10)

    def test_native_zero_and_native_positive_both_override_estimate(self):
        for native in (0, 9.25):
            self.write('a', [self.request(), {'type': 'cost-state', 'totalCostUSD': native}])
            data = self.data()
            self.assertEqual(data['totals']['combined_cost'], native)
            self.assertEqual(data['totals']['estimated_cost'], 0)
            self.assertIsNone(data['sessions'][0]['estimated_cost'])

    def test_rescan_replaces_estimate_when_native_arrives(self):
        self.write('a', [self.request()])
        index = UsageIndex(self.root)
        self.assertEqual(index.summary()['totals']['combined_cost'], 4)
        self.write('a', [self.request(), {'type': 'cost-state', 'totalCostUSD': 3.5}])
        after = index.snapshot()
        self.assertEqual(after['totals']['combined_cost'], 3.5)
        self.assertEqual(after['totals']['estimated_cost'], 0)

    def test_unknown_advisor_keeps_known_part_marked_partial(self):
        row = self.request()
        row['message']['usage']['iterations'] = [{'type': 'advisor_message', 'model': 'unknown',
                                                  'input_tokens': 100, 'output_tokens': 20}]
        self.write('a', [row])
        data = self.data()
        self.assertEqual(data['totals']['estimated_cost'], 4)
        self.assertFalse(data['sessions'][0]['estimate_complete'])
        self.assertEqual(data['cost_estimate_partial_session_count'], 1)

    def test_absent_usage_is_not_a_zero_dollar_estimate(self):
        row = self.request()
        del row['message']['usage']
        self.write('a', [row])
        session = self.data()['sessions'][0]
        self.assertIsNone(session['estimated_cost'])
        self.assertFalse(session['estimate_complete'])

    def test_tier_not_applied_to_session_aggregate(self):
        self.write('a', [self.request('r1', 'claude-haiku-5-5', 60_000),
                         self.request('r2', 'claude-haiku-5-5', 60_000)])
        data = self.data()
        self.assertAlmostEqual(data['totals']['combined_cost'], .012)

    def test_streaming_cache_metadata_is_cumulative(self):
        first = self.request(amount=0)
        first['message']['usage'].update(cache_creation_input_tokens=100,
                                        cache_creation={'ephemeral_1h_input_tokens': 100})
        final = self.request(amount=0)
        final['message']['usage'].update(cache_creation_input_tokens=200,
                                        cache_creation={'ephemeral_1h_input_tokens': 200})
        self.write('a', [first, final])
        self.assertAlmostEqual(self.data()['totals']['estimated_cost'], .0016)


if __name__ == '__main__':
    unittest.main()
