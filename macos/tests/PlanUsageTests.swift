import Foundation

@main
enum PlanUsageTests {
    static func main() throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        func sample(_ used: Any = 24, weekly: Any = 35, age: Double = 0, org: String = "fixture-org") -> [String: Any] {
            ["t": now.addingTimeInterval(-age).timeIntervalSince1970 * 1000,
             "org": org, "u": ["fh": used, "sd": weekly]]
        }
        func decode(_ samples: [Any], version: Any = 2) throws -> PlanUsage {
            PlanUsage.decode(try JSONSerialization.data(withJSONObject: ["version": version, "samples": samples]))
        }
        let fresh = try decode([sample()])
        precondition(fresh.issue(now: now) == nil)
        precondition(fresh.fiveHour(now: now).remaining == 76 && fresh.sevenDay(now: now).remaining == 65)
        precondition(fresh.fiveHour(now: now).usedText == "24%")
        precondition(fresh.fiveHour(now: now).ringFraction == 0.76)
        precondition(fresh.fiveHour(now: now).number == "76")

        for age: TimeInterval in [1801, 86400, 30 * 86400] {
            let old = try decode([sample(age: age)])
            precondition(old.issue(now: now) == nil)
            precondition(old.fiveHour(now: now).number == "76" && old.sevenDay(now: now).number == "65")
            precondition(old.fiveHour(now: now).ringFraction == 0.76,
                         "Idle accounts retain the recorded percentage regardless of sample age")
        }
        precondition(fresh.fiveHour(now: now.addingTimeInterval(86400)).remaining == 76)
        let future = try decode([sample(age: -61)])
        precondition(future.issue(now: now) == .future)

        let empty = try decode([sample(100, weekly: 0)])
        precondition(empty.fiveHour(now: now).number == "0" && empty.fiveHour(now: now).ringFraction == 0)
        precondition(empty.sevenDay(now: now).number == "100" && empty.sevenDay(now: now).ringFraction == 1)
        let decimal = try decode([sample(24.5)])
        precondition(decimal.fiveHour(now: now).remaining == 75.5 && decimal.fiveHour(now: now).number == "76")

        // Newest timestamp wins regardless of array order. Never combine orgs.
        let latest = try decode([sample(50, weekly: NSNull(), org: "new-org"), sample(10, weekly: 20, age: 60)])
        precondition(latest.fiveHour(now: now).remaining == 50)
        precondition(latest.sevenDay(now: now).remaining == nil)
        let invalid = try decode([sample(true, weekly: 101)])
        precondition(invalid.issue(now: now) == .invalid, "Booleans and out-of-range usage are not real percentages")
        let noSamples = try decode([])
        let boolVersion = try decode([sample()], version: true)
        let unknownVersion = try decode([sample()], version: 3)
        precondition(noSamples.issue(now: now) == .invalid)
        precondition(boolVersion.issue(now: now) == .invalid)
        precondition(unknownVersion.issue(now: now) == .invalid)
        precondition(PlanUsage.decode(Data("bad JSON".utf8)).issue(now: now) == .invalid)

        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("plan-usage-history.json")
        precondition(PlanUsage.read(from: file).issue(now: now) == .missing)
        let bytes = try JSONSerialization.data(withJSONObject: ["version": 2, "samples": [sample(age: 2000)]])
        try bytes.write(to: file)
        let before = try FileManager.default.attributesOfItem(atPath: file.path)[.modificationDate] as! Date
        precondition(PlanUsage.read(from: file).fiveHour(now: now).remaining == 76,
                     "Old samples remain usable without changing the recorded timestamp")
        let afterBytes = try Data(contentsOf: file)
        precondition(afterBytes == bytes)
        let after = try FileManager.default.attributesOfItem(atPath: file.path)[.modificationDate] as! Date
        precondition(before == after, "Reader must leave the source file unchanged")
        precondition(PlanUsage.read(from: folder).issue(now: now) == .unreadable)
        print("PASS: quota percentages, idle snapshot retention, unknown/zero rings, sample ordering, organizations, malformed data and read-only file access")
    }
}
