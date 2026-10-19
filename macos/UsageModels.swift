import Foundation

struct TokenCounts: Decodable {
    var inputTokens: Int64 = 0
    var outputTokens: Int64 = 0
    var thinkingTokens: Int64 = 0
    var cacheReadInputTokens: Int64 = 0
    var cacheCreationInputTokens: Int64 = 0

    // Thinking is already part of output; it must not be counted twice.
    var total: Int64 { inputTokens + outputTokens + cacheReadInputTokens + cacheCreationInputTokens }
    static let zero = TokenCounts()

    mutating func add(_ other: TokenCounts) {
        inputTokens += other.inputTokens
        outputTokens += other.outputTokens
        thinkingTokens += other.thinkingTokens
        cacheReadInputTokens += other.cacheReadInputTokens
        cacheCreationInputTokens += other.cacheCreationInputTokens
    }
}

struct UsageSnapshot: Decodable {
    struct Totals: Decodable {
        let cost: Double
        let estimated_cost: Double?
        let combined_cost: Double?
        let tokens: TokenCounts
        let main_tokens: TokenCounts
        let advisor_tokens: TokenCounts
        let native_tokens: TokenCounts?
    }
    struct Day: Decodable {
        let date: String
        let cost: Double
        let estimated_cost: Double?
        let combined_cost: Double?
        let tokens: TokenCounts
    }
    struct Model: Decodable, Identifiable {
        let model: String
        let cost: Double
        let estimated_cost: Double?
        let combined_cost: Double?
        let tokens: TokenCounts
        var id: String { model }
    }
    struct Session: Decodable {
        let last: String?
        let cost_status: String
        let estimated_cost: Double?
        let estimate_complete: Bool?
    }

    let generated_at: String
    let session_count: Int
    let usage_event_count: Int
    let cost_available_session_count: Int
    let cost_missing_session_count: Int
    let cost_pending_session_count: Int
    let cost_unreconciled_session_count: Int?
    let native_token_session_count: Int?
    let cost_estimated_session_count: Int?
    let cost_estimate_partial_session_count: Int?
    let totals: Totals
    let daily: [Day]
    let models: [Model]
    let sessions: [Session]

    struct Metrics {
        var cost: Double = 0
        var nativeCost: Double = 0
        var estimatedCost: Double = 0
        var estimatedSessions = 0
        var tokens = TokenCounts.zero
        var pendingSessions = 0
        var unreconciledSessions = 0
        var activeDays = 0
        var hasEstimates: Bool { estimatedCost > 0 || estimatedSessions > 0 }
        var costText: String {
            if hasEstimates { return "≈" + UsageFormat.money(cost) }
            if cost == 0 && unreconciledSessions > 0 { return "待对账" }
            return pendingSessions > 0 && cost == 0 ? "待写入" : UsageFormat.money(cost)
        }
    }

    func metrics(days: Int, now: Date = Date()) -> Metrics {
        if days == 0 {
            // All local data includes undated records and history older than
            // the date filters. Use the authoritative deduplicated totals.
            var result = Metrics()
            result.nativeCost = totals.cost
            result.estimatedCost = totals.estimated_cost ?? 0
            result.cost = totals.combined_cost ?? (result.nativeCost + result.estimatedCost)
            result.tokens = totals.tokens
            result.pendingSessions = sessions.filter {
                $0.estimated_cost == nil && ($0.cost_status == "pending" || $0.cost_status == "missing")
            }.count
            result.unreconciledSessions = sessions.filter { $0.cost_status == "unreconciled" }.count
            result.estimatedSessions = sessions.filter { $0.estimated_cost != nil }.count
            result.activeDays = daily.filter { $0.cost > 0 || $0.tokens.total > 0 }.count
            return result
        }
        // The existing archive groups daily events in Asia/Tokyo.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 9 * 3600)!
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        let today = formatter.string(from: now)
        let start = formatter.string(from: calendar.date(byAdding: .day, value: -(days - 1), to: now)!)
        var result = Metrics()
        for day in daily where day.date >= start && day.date <= today {
            result.cost += day.combined_cost ?? day.cost
            result.nativeCost += day.cost
            result.estimatedCost += day.estimated_cost ?? 0
            result.tokens.add(day.tokens)
            if day.cost > 0 || day.tokens.total > 0 { result.activeDays += 1 }
        }
        result.pendingSessions = sessions.filter {
            guard let last = $0.last else { return false }
            let day = String(last.prefix(10))
            return day >= start && day <= today && $0.estimated_cost == nil && ($0.cost_status == "pending" || $0.cost_status == "missing")
        }.count
        result.unreconciledSessions = sessions.filter {
            guard let last = $0.last else { return false }
            let day = String(last.prefix(10))
            return day >= start && day <= today && $0.cost_status == "unreconciled"
        }.count
        result.estimatedSessions = sessions.filter {
            guard let last = $0.last else { return false }
            let day = String(last.prefix(10))
            return day >= start && day <= today && $0.estimated_cost != nil
        }.count
        return result
    }
}

enum UsageFormat {
    static func tokens(_ value: Int64) -> String {
        // Promote rounded values at unit boundaries (999.995K becomes 1.00M).
        let units: [(Int64, Double, String)] = [
            (999_995_000, 1_000_000_000, "B"),
            (999_995, 1_000_000, "M"),
            (1_000, 1_000, "K")
        ]
        for (threshold, divisor, suffix) in units where value >= threshold {
            return String(format: "%.2f%@", locale: Locale(identifier: "en_US_POSIX"), Double(value) / divisor, suffix)
        }
        return exact(value)
    }

    static func exact(_ value: Int64) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.locale = Locale(identifier: "en_US")
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }

    static func money(_ value: Double) -> String { String(format: "$%.2f", value) }

    static func model(_ value: String) -> String {
        value.replacingOccurrences(of: "claude-", with: "Claude ")
            .replacingOccurrences(of: "-(\\d+)-(\\d+)$", with: " $1.$2", options: .regularExpression)
            .capitalized
    }
}
