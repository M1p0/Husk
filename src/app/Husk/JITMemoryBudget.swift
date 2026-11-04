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
    static let fallbackKey = "husk.jit.fallbackMegabytes"
    static let defaultFallbackMiB = 512
    static let page = 16384

    /// Only the application's native images count here, never APK assets, Java heap or textures.
    struct Library {
        let name: String
        let abi: String
        let pages: Int?
        let codePages: Int?
        let systemCalls: Int
        let imports: Int
    }

    static func validFallback(_ text: String) -> Int? {
        guard let value = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              (minimumMiB...maximumMiB).contains(value) else { return nil }
        return value
    }

    static func fallbackMiB(defaults: UserDefaults = .standard) -> Int {
        validFallback(String(defaults.integer(forKey: fallbackKey))) ?? defaultFallbackMiB
    }

    /// ELF image pages include BSS and segment gaps. The instruction allowance covers rewriting (x18, ADR and literal
    /// stubs); the final 12.5% reserve allows for JNI stubs and code produced later. This is an estimate, not a guarantee.
    static func estimateBytes(libraries: [Library], complete: Bool) -> Int? {
        guard complete else { return nil }
        if libraries.isEmpty { return minimumMiB * mib }
        let arm64 = libraries.filter { $0.abi == "arm64-v8a" }
        guard !arm64.isEmpty else { return nil } // The ARM32 translator has a different memory model.
        let limit = maximumMiB * mib
        var images: [String: Int] = [:]
        for lib in arm64 {
            guard let pages = lib.pages, pages > 0, pages <= limit / page else { return nil }
            let image = pages * page
            let code = max(0, min(pages, lib.codePages ?? pages)) * page
            let calls = min(max(0, lib.systemCalls), limit / 32) * 32
            let imports = min(max(0, lib.imports), limit / 16) * 16
            let size = min(limit, image + code / 8 + calls + imports + 3 * page)
            // The loader resolves one library by name even when APK splits duplicate it.
            images[lib.name] = max(images[lib.name] ?? 0, size)
        }
        let total = images.values.reduce(0) { min(limit, $0 + $1) }
        return withHeadroom(total)
    }

    static func withHeadroom(_ bytes: Int) -> Int {
        let bounded = max(0, min(bytes, maximumMiB * mib))
        return min(maximumMiB * mib, max(minimumMiB * mib, bounded + max(mib, bounded / 8)))
    }

    struct Profile: Codable {
        var megabytes: Int
        var confirmed = false
        var lastFailureRunID: String?
        var estimatedBytes: Int?
        var estimateIdentity: String?
        var observedPeakBytes: Int?
        var failedRequiredBytes: Int?
    }
    private struct Configuration: Codable {
        var apps: [String: Profile] = [:]
    }
    struct Run: Codable {
        let id: String
        let appID: String
        let capacityBytes: Int
        var initialUsedBytes: Int? = nil
        var peakUsedBytes: Int? = nil
        var reachedRunning: Bool? = nil
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
    // A saved profile is history, not knowledge of which game the user will choose after opening Husk.
    private(set) var selectedAppID: String?
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

    var selectedMiB: Int { megabytes(for: selectedAppID) }

    func requiredMiB(for appID: String?) -> Int? {
        guard let appID else { return nil }
        if appID == Self.androidID || appID.hasPrefix("ps4:") { return megabytes(for: appID) }
        guard let p = configuration.apps[appID],
              (Self.minimumMiB...Self.maximumMiB).contains(p.megabytes),
              p.confirmed || p.estimatedBytes != nil || p.lastFailureRunID != nil else { return nil }
        return megabytes(for: appID)
    }

    func allocationMiB(fallbackMiB: Int, androidAtLaunch: Bool = false) -> Int {
        let target = selectedAppID ?? (androidAtLaunch ? Self.androidID : nil)
        let requested = requiredMiB(for: target) ?? Self.validFallback(String(fallbackMiB)) ?? Self.defaultFallbackMiB
        return androidAtLaunch ? max(256, requested) : requested
    }

    /// A cold measured budget can include the Java runtime's process-lifetime images. Those remain resident after close.
    /// Still reserve the app's own estimated images (bounded by its calibrated budget), and give unknown apps no credit.
    func additionalBytes(for appID: String, coldBytes: Int, sharedBytes: Int) -> Int {
        guard let profile = configuration.apps[appID], let estimate = profile.estimatedBytes else { return coldBytes }
        let shared = max(0, sharedBytes)
        var additional = max(0, coldBytes - shared)
        // A rounded cold capacity includes unused reserve. Use the measured peak when available, keeping fresh headroom
        // and the app-image estimate as a floor; app-specific pages retained from earlier runs still occupy the region.
        if shared > 0, profile.confirmed, let peak = profile.observedPeakBytes {
            additional = min(additional, Self.withHeadroom(max(0, peak - shared)))
        }
        let appImages = min(coldBytes, max(0, estimate))
        let failedRequest = min(coldBytes, max(0, profile.failedRequiredBytes ?? 0))
        return max(additional, appImages, failedRequest)
    }

    /// New APK contents invalidate old measurements. A rescan of unchanged APKs can refine the static estimate without
    /// throwing away measurements, or the larger budget learned from an explicit capacity failure.
    func updateEstimate(appID: String, bytes: Int?, identity: String) {
        var p = configuration.apps[appID] ?? Profile(megabytes: Self.minimumMiB)
        let changed = p.estimateIdentity != nil && p.estimateIdentity != identity
        if changed { p = Profile(megabytes: Self.minimumMiB) }
        guard p.estimateIdentity != identity || p.estimatedBytes != bytes else { return }
        p.estimateIdentity = identity
        p.estimatedBytes = bytes.map { max(0, min($0, Self.maximumMiB * Self.mib)) }
        // Legacy profiles only saved a rounded allocation, not an attributable observed peak. Unknown images must
        // not turn that old value (including a different translator's zero usage) into a known app requirement.
        if bytes == nil && p.observedPeakBytes == nil && p.lastFailureRunID == nil { p.confirmed = false }
        if !p.confirmed && p.lastFailureRunID == nil {
            p.megabytes = Self.roundedMiB(bytes: p.estimatedBytes ?? 0)
        }
        configuration.apps[appID] = p
        save()
    }

    @discardableResult
    func select(_ appID: String) -> Int {
        selectedAppID = appID
        return megabytes(for: appID)
    }

    @discardableResult
    func begin(appID: String, capacityBytes: Int, initialUsedBytes: Int = 0) -> Run {
        if let run = activeRun, run.appID == appID { return run }
        _ = select(appID)
        clearExhaustion()
        let run = Run(id: UUID().uuidString, appID: appID, capacityBytes: capacityBytes,
                      initialUsedBytes: max(0, initialUsedBytes), peakUsedBytes: 0, reachedRunning: false)
        activeRun = run
        write(run, to: runURL)
        return run
    }

    /// A run may have inherited a larger region from another app. Save its measured use for the next cold launch.
    func recordUsage(bytes: Int, madeProgress: Bool) {
        guard var run = activeRun, bytes >= 0, bytes <= run.capacityBytes else { return }
        let used = max(0, bytes - (run.initialUsedBytes ?? 0))
        run.peakUsedBytes = max(run.peakUsedBytes ?? 0, used)
        run.reachedRunning = true
        activeRun = run
        let size = max(megabytes(for: run.appID), Self.roundedMiB(bytes: used))
        if configuration.apps[run.appID]?.megabytes != size || configuration.apps[run.appID]?.confirmed != true {
            var p = configuration.apps[run.appID] ?? Profile(megabytes: size)
            p.megabytes = size
            p.confirmed = true
            configuration.apps[run.appID] = p
            save()
        }
        // Optional allocations can recover: subsequent frames show that this failure did not end the game.
        if madeProgress { clearExhaustion() }
    }

    /// A successful exit is a better estimate than a large speculative allocation. Include a final sample (late DLL/JIT
    /// allocations may arrive between polls), retain the measured high-water mark and add headroom for the next run.
    @discardableResult
    func calibrateOnExit(bytes: Int, successful: Bool) -> Int? {
        guard successful, let run = activeRun, run.reachedRunning == true,
              bytes >= 0, bytes <= run.capacityBytes,
              !FileManager.default.fileExists(atPath: exhaustionURL.path),
              configuration.apps[run.appID]?.lastFailureRunID != run.id else { return nil }
        var p = configuration.apps[run.appID] ?? Profile(megabytes: Self.minimumMiB)
        let peak = max(run.peakUsedBytes ?? 0, max(0, bytes - (run.initialUsedBytes ?? 0)))
        p.observedPeakBytes = max(p.observedPeakBytes ?? 0, peak)
        let suggested = Self.roundedMiB(bytes: max(Self.withHeadroom(p.observedPeakBytes ?? 0), p.failedRequiredBytes ?? 0))
        p.megabytes = suggested
        p.confirmed = true
        configuration.apps[run.appID] = p
        save()
        return suggested
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
        let baseline = run.initialUsedBytes ?? 0
        guard baseline >= 0, baseline <= failure.usedBytes else { return nil }
        let previous = max(megabytes(for: run.appID), Self.roundedMiB(bytes: failure.capacityBytes))
        guard previous < Self.maximumMiB else { return nil }
        let next = max(previous * 2, Self.roundedMiB(bytes: failure.usedBytes + failure.requestedBytes))
        var p = configuration.apps[run.appID] ?? Profile(megabytes: next)
        p.megabytes = next
        p.confirmed = false
        p.lastFailureRunID = run.id
        let neededForApp = failure.usedBytes - baseline + failure.requestedBytes
        p.failedRequiredBytes = max(p.failedRequiredBytes ?? 0, neededForApp)
        configuration.apps[run.appID] = p
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
    func end() {
        if selectedAppID == activeRun?.appID { selectedAppID = nil }
        activeRun = nil
        suspend()
    }

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
