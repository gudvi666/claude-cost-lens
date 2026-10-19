# Cost and Token Accounting

[返回 README](../README.md)


- **费用合计（原生 + API 估算）** combines reconciled saved native ledgers with
  labelled API estimates for uncovered sessions. Claude's
  native `cost-state.totalCostUSD` is a usage estimate, not an invoice.
- Forked or resumed sessions can inherit the previous cumulative ledger. A saved
  baseline is subtracted only when ledger start time, copied request history,
  snapshot chronology and per-model counters support the same ancestor. The
  baseline must cover all usage-bearing requests in the inherited prefix.
  Equal amounts or equal start times alone never trigger deduplication. Parallel
  branches each retain their own incremental charges.
- Session amount hover preserves the original native cumulative amount and the
  confirmed inherited deduction. Total, project, model, daily and Advisor fees
  use the same reconciled increments; Advisor fees are not added a second time.
- **待对账** means a saved amount exists but its inherited baseline is uncertain.
  Its native cumulative amount is retained for inspection and excluded from
  reconciled totals. Multiple incomparable ancestors, missing baseline snapshots
  and ledger resets remain explicit unknowns. API estimates use attributed unique
  requests and remain separate from the unverified native ledger.
- **待写入** means an active process has no saved amount; **未记录** means the
  archive has no saved amount. A saved native zero is distinct from both.
  Active sessions with a saved amount show **已保存快照**; it may be stale.
- If a transcript lacks cost-state, native project `lastCost` may supply it only
  with an exact `lastSessionId` match. `lastStartTime` and `lastModelUsage` are
  retained for reconciliation. Reading files cannot flush Claude's memory ledger.
- Today, 7-day and 30-day windows use the actual Asia/Tokyo calendar date in both
  the web and menu bar interfaces. Daily fees allocate reconciled snapshot deltas
  near their save time; cross-midnight distribution is approximate.
- The primary Token total uses saved assistant `message.usage`, deduplicated by
  request ID across sessions. Streaming cumulative fields use the largest saved
  value; copied history is counted once. Missing request IDs fall back to message
  ID, then UUID; unidentifiable records remain separate.
- Main-model usage plus `advisor_message` iterations form the Token total. Advisor
  configuration alone does not count as a call. Missing Advisor usage is labelled.
  Input + output + cache read + cache creation form total tokens; thinking is
  already part of output and is not added again. All Token displays, including hover details, use decimal K/M/B units
  (1K = 1,000; 1M = 1,000,000; 1B = 1,000,000,000), with two decimal
  places. Values below 1,000 stay as integers. Raw API counts remain exact.
- **JSONL also contains native Token summaries** in `cost-state.modelUsage`:
  `inputTokens`, `outputTokens`, `thinkingTokens`, `cacheReadInputTokens` and
  `cacheCreationInputTokens` for each model. These saved cumulative summaries can
  include inherited history, internal calls absent from transcripts and stale
  snapshots. The web native-token reconciliation panel uses confirmed ledger
  increments and reports how many sessions have complete native counter fields.
  Each session hover also shows its raw native Token summary. Native and request
  totals are labelled separately because their coverage and save time can differ.
- The displayed amount combines native reconciled fees with labelled API estimates
  for uncovered sessions. Missing prices and unrecorded calls remain partial;
  uncertain fees are never displayed as real zero consumption.

## Privacy

The dashboard reads local transcripts, native project configuration, and session
process metadata. It checks whether the recorded process is running without
sending commands to Claude. The npm package contains the application source,
not the contents of any user's Claude data directory.

### API price estimates when native costs are unavailable

The dashboard and menu bar show native reconciled fees plus a separately labelled
API estimate for sessions with no usable native amount (pending, missing, or an
unreconciled inherited ledger). A usable native amount, including zero, always
replaces the estimate for that session on the next refresh. Saved native snapshots
are not supplemented with the entire session's transcript estimate.

The local price snapshot was checked on **2026-10-19** against
[Claude's official price list](https://platform.claude.com/docs/en/about-claude/pricing)
and [the public pricing page](https://claude.com/pricing). The app makes no pricing
network requests while scanning and needs no API key. The snapshot date and source
are shown on the dashboard and returned in `pricing` by the API. Current list
prices are used for estimates, including older log entries; this is an API
equivalent valuation, not a reconstruction of historical invoices or subscription
charges. Account discounts, taxes and unrecorded calls are not included.

Requests are deduplicated before pricing. Input, output, cache reads and writes
are priced separately. Known 5-minute/1-hour cache writes are split; unclassified
cache writes assume 5 minutes and disclose that assumption. Thinking is already
in output. Haiku 5.5's >100K prompt tier is selected separately for each request,
including cached input. Recorded Fast, US inference, batch and web search usage
are applied where the snapshot supports them. Advisor iterations use their own
model and token usage. Unknown models or missing Advisor usage produce partial
estimates rather than adopting another model's price. Code-execution runtime fees
cannot be inferred from a call count and remain a disclosed gap.

The JSON API retains `cost` as the reconciled native amount and adds
`estimated_cost` and `combined_cost` to sessions, totals, models, projects, daily
rows and date windows. Native pending/unreconciled status remains available; the
UI labels the fallback as API estimate, including partial coverage.
