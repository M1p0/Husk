// SPDX-License-Identifier: GPL-2.0-or-later
import Foundation

/// Learns the smallest power-of-two code region observed to fit each app. C commits exhaustion evidence before
/// returning a failed allocation, so recovery at the next cold launch does not depend on a Swift crash callback.
final class JITMemoryBudget {
    static let shared = JITMemoryBudget(directory: FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0])
    static let mib = 1024 * 1024
    static let minimumMiB = 2
    static let maximumMiB = 1024
    static let androidID = "system:android"

    struct Profile: Codable {
        var megabytes: Int
        var confirmed = false
        var lastFailureRunID: String?
    }
    private struct Configuration: Codable {
        var selectedAppID: String?
        var apps: [String: Profile] = [:]
    }
    struct Run: Codable {
        let id: String
        let appID: String
        let capacityBytes: Int
    }
    struct Exhaustion: Codable {
        let runID: String
        let capacityBytes: Int
        let usedBytes: Int
        let requestedBytes: Int
    }
    struct Growth {
        let appID: String
        let previousMiB: Int
        let nextMiB: Int
    }

    private let directory: URL
    private var configuration: Configuration
    private(set) var activeRun: Run?
    var configurationURL: URL { directory.appendingPathComponent("jit-memory.json") }
    var runURL: URL { directory.appendingPathComponent("jit-memory-run.json") }
    var exhaustionURL: URL { directory.appendingPathComponent("jit-memory-exhaustion.json") }

    init(directory: URL) {
        self.directory = directory
        configuration = (try? JSONDecoder().decode(Configuration.self, from: Data(contentsOf: directory.appendingPathComponent("jit-memory.json")))) ?? Configuration()
    }

    static func roundedMiB(bytes: Int) -> Int {
        var size = minimumMiB
        while size < maximumMiB && bytes > size * mib { size *= 2 }
        return size
    }

    func megabytes(for appID: String?) -> Int {
        let floor = appID == Self.androidID ? 256 : appID?.hasPrefix("ps4:") == true ? 512 : Self.minimumMiB
        let saved = appID.flatMap { configuration.apps[$0]?.megabytes } ?? floor
        guard saved >= Self.minimumMiB, saved <= Self.maximumMiB else { return floor }
        return max(floor, Self.roundedMiB(bytes: saved * Self.mib))
    }

    var selectedMiB: Int { megabytes(for: configuration.selectedAppID) }

    @discardableResult
    func select(_ appID: String) -> Int {
        if configuration.selectedAppID != appID {
            configuration.selectedAppID = appID
            save()
        }
        return megabytes(for: appID)
    }

    @discardableResult
    func begin(appID: String, capacityBytes: Int) -> Run {
        if let run = activeRun, run.appID == appID { return run }
        _ = select(appID)
        clearExhaustion()
        let run = Run(id: UUID().uuidString, appID: appID, capacityBytes: capacityBytes)
        activeRun = run
        write(run, to: runURL)
        return run
    }

    /// A run may have inherited a larger region from another app. Save its measured use for the next cold launch.
    func recordUsage(bytes: Int, madeProgress: Bool) {
        guard let run = activeRun, bytes >= 0, bytes <= run.capacityBytes else { return }
        let size = max(megabytes(for: run.appID), Self.roundedMiB(bytes: bytes))
        if configuration.apps[run.appID]?.megabytes != size || configuration.apps[run.appID]?.confirmed != true {
            configuration.apps[run.appID] = Profile(megabytes: size, confirmed: true,
                                                    lastFailureRunID: configuration.apps[run.appID]?.lastFailureRunID)
            save()
        }
        // Optional allocations can recover: subsequent frames show that this failure did not end the game.
        if madeProgress { clearExhaustion() }
    }

    /// Only after a run ended, or at cold launch. A signal, missing JIT, or heap OOM alone never increases this budget.
    func recover(log: String) -> Growth? {
        defer { clearExhaustion() }
        guard let run = activeRun ?? read(Run.self, from: runURL),
              let failure = read(Exhaustion.self, from: exhaustionURL), failure.runID == run.id,
              configuration.apps[run.appID]?.lastFailureRunID != run.id,
              failure.capacityBytes > 0, failure.capacityBytes <= Self.maximumMiB * Self.mib,
              failure.usedBytes >= 0, failure.usedBytes <= failure.capacityBytes,
              failure.requestedBytes > failure.capacityBytes - failure.usedBytes,
              failure.requestedBytes <= Int.max - failure.usedBytes,
              !Self.systemMemoryWasLow(log) else { return nil }
        let previous = max(megabytes(for: run.appID), Self.roundedMiB(bytes: failure.capacityBytes))
        guard previous < Self.maximumMiB else { return nil }
        let next = max(previous * 2, Self.roundedMiB(bytes: failure.usedBytes + failure.requestedBytes))
        configuration.apps[run.appID] = Profile(megabytes: next, lastFailureRunID: run.id)
        configuration.selectedAppID = run.appID
        save()
        return Growth(appID: run.appID, previousMiB: previous, nextMiB: next)
    }

    static func systemMemoryWasLow(_ log: String) -> Bool {
        if log.split(separator: "\n").suffix(40).contains(where: { $0.contains("main thread ended with java.lang.OutOfMemoryError") }) {
            return true
        }
        let readings = log.split(separator: "\n").suffix(40).compactMap { line -> Int? in
            guard let r = line.range(of: " MiB left before jetsam") else { return nil }
            return Int(line[..<r.lowerBound].split(separator: " ").last ?? "")
        }
        return readings.last.map { $0 < 80 } ?? false
    }

    func suspend() {
        try? FileManager.default.removeItem(at: runURL)
        clearExhaustion()
    }
    func resume() { if let run = activeRun { write(run, to: runURL) } }
    func end() { activeRun = nil; suspend() }

    private func clearExhaustion() { try? FileManager.default.removeItem(at: exhaustionURL) }
    private func save() { write(configuration, to: configurationURL) }
    private func read<T: Decodable>(_ type: T.Type, from url: URL) -> T? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
    private func write<T: Encodable>(_ value: T, to url: URL) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? encoder.encode(value) { try? data.write(to: url, options: .atomic) }
    }
}
