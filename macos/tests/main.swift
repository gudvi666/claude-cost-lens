import Foundation

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}

let tokens = TokenCounts(inputTokens: 256, outputTokens: 84_276, thinkingTokens: 24_123,
                         cacheReadInputTokens: 20_827_092, cacheCreationInputTokens: 217_262)
require(tokens.total == 21_128_886, "Thinking must not be counted twice")
require(UsageFormat.tokens(999) == "999", "Sub-thousand count")
require(UsageFormat.tokens(1_000) == "1.00K", "Thousands")
require(UsageFormat.tokens(999_995) == "1.00M", "Rounded K promotes to M")
require(UsageFormat.tokens(100_000_000) == "100.00M", "Millions")
require(UsageFormat.tokens(999_995_000) == "1.00B", "Rounded M promotes to B")
require(UsageFormat.tokens(2_866_121_863) == "2.87B", "Billions")
require(UsageFormat.exact(2_829_864_670) == "2,829,864,670", "Exact 64-bit count")
require(UsageFormat.model("claude-opus-5-5") == "Claude Opus 5.5", "Model name")

let t: [String: Int64] = ["inputTokens": 256, "outputTokens": 84276, "thinkingTokens": 24123,
                         "cacheReadInputTokens": 20827092, "cacheCreationInputTokens": 217262]
let fixture: [String: Any] = [
    "generated_at": "2026-10-19T12:00:00+09:00", "session_count": 1,
    "usage_event_count": 4, "cost_available_session_count": 0,
    "cost_missing_session_count": 1, "cost_pending_session_count": 1,
    "totals": ["cost": 8, "tokens": t, "main_tokens": t, "advisor_tokens": t],
    "daily": [
        ["date": "2026-10-16", "cost": 5, "tokens": t],
        ["date": "2026-10-17", "cost": 3, "tokens": t],
        ["date": "2026-10-19", "cost": 0, "tokens": t],
        ["date": "2026-10-20", "cost": 999, "tokens": t]
    ],
    "models": [],
    "sessions": [["last": "2026-10-19T12:00:00+09:00", "cost_status": "pending"]]
]
let snapshot = try JSONDecoder().decode(UsageSnapshot.self, from: JSONSerialization.data(withJSONObject: fixture))
let now = ISO8601DateFormatter().date(from: "2026-10-19T03:00:00Z")!
require(snapshot.metrics(days: 1, now: now).costText == "待写入", "Missing native fee is not zero")
require(snapshot.metrics(days: 1, now: now).tokens.total == tokens.total, "Today is the actual calendar day")
require(snapshot.metrics(days: 7, now: now).cost == 8, "Seven-day range excludes future records")
require(snapshot.metrics(days: 7, now: now).tokens.total == tokens.total * 3, "Range token aggregation")
require(snapshot.metrics(days: 30, now: now).pendingSessions == 1, "Range fee coverage")
print("PASS: native decoding, exact Token totals, Thinking exclusion, units, model names, date ranges and pending fees")

var uncertainFixture = fixture
uncertainFixture["sessions"] = [["last": "2026-10-19T12:00:00+09:00", "cost_status": "unreconciled"]]
let uncertain = try JSONDecoder().decode(UsageSnapshot.self, from: JSONSerialization.data(withJSONObject: uncertainFixture))
require(uncertain.metrics(days: 1, now: now).costText == "待对账", "Unknown inherited fees are not real zero")
require(uncertain.metrics(days: 30, now: now).unreconciledSessions == 1, "Reconciliation coverage is visible")
print("PASS: uncertain inherited ledgers display 待对账")

var estimatedFixture = fixture
estimatedFixture["daily"] = [["date": "2026-10-19", "cost": 1.5, "estimated_cost": 2.75, "combined_cost": 4.25, "tokens": t]]
estimatedFixture["sessions"] = [["last": "2026-10-19T12:00:00+09:00", "cost_status": "pending", "estimated_cost": 2.75, "estimate_complete": true]]
let estimated = try JSONDecoder().decode(UsageSnapshot.self, from: JSONSerialization.data(withJSONObject: estimatedFixture))
let pricedMetrics = estimated.metrics(days: 1, now: now)
require(pricedMetrics.costText == "≈$4.25", "Mixed fees are visibly approximate")
require(pricedMetrics.nativeCost == 1.5 && pricedMetrics.estimatedCost == 2.75, "Separate native and estimated amounts")
require(pricedMetrics.pendingSessions == 0 && pricedMetrics.estimatedSessions == 1, "Priced fallback replaces pending display")
print("PASS: mixed API estimates remain separate from native fees")

var allFixture = fixture
allFixture["totals"] = ["cost": 12.5, "estimated_cost": 2.75, "combined_cost": 15.25,
                        "tokens": t, "main_tokens": t, "advisor_tokens": t]
allFixture["sessions"] = [
    ["last": "2020-01-01T12:00:00+09:00", "cost_status": "recorded"],
    ["cost_status": "pending"],
    ["cost_status": "unreconciled", "estimated_cost": 2.75],
    ["cost_status": "missing"]
]
let allSnapshot = try JSONDecoder().decode(UsageSnapshot.self, from: JSONSerialization.data(withJSONObject: allFixture))
let allMetrics = allSnapshot.metrics(days: 0, now: now)
require(allMetrics.costText == "≈$15.25", "All-local fees come from deduplicated totals")
require(allMetrics.nativeCost == 12.5 && allMetrics.estimatedCost == 2.75, "All-local fee breakdown")
require(allMetrics.tokens.total == tokens.total, "All-local tokens use totals, not date-filtered daily sums")
require(allMetrics.pendingSessions == 2 && allMetrics.unreconciledSessions == 1 && allMetrics.estimatedSessions == 1,
        "All-local coverage includes sessions without dates")
require(allSnapshot.metrics(days: 0, now: Date(timeIntervalSince1970: 0)).cost == allMetrics.cost,
        "All-local metrics do not depend on current date")
allFixture["totals"] = ["cost": 12.5, "estimated_cost": 2.75, "tokens": t, "main_tokens": t, "advisor_tokens": t]
let legacyAll = try JSONDecoder().decode(UsageSnapshot.self, from: JSONSerialization.data(withJSONObject: allFixture))
require(legacyAll.metrics(days: 0).cost == 15.25, "All-local fallback adds native and estimates once")
print("PASS: all-local totals, missing-date coverage, fee split and date independence")
