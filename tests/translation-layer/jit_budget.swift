// SPDX-License-Identifier: GPL-2.0-or-later
import Foundation

@main enum JITBudgetTests {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let mib = JITMemoryBudget.mib
        func budget(_ name: String) -> JITMemoryBudget { JITMemoryBudget(directory: root.appendingPathComponent(name)) }
        func exhaust(_ b: JITMemoryBudget, _ run: JITMemoryBudget.Run, used: Int = 0, request: Int,
                     runID: String? = nil, capacity: Int? = nil) throws {
            let failure = JITMemoryBudget.Exhaustion(runID: runID ?? run.id, capacityBytes: capacity ?? run.capacityBytes,
                                                    usedBytes: used, requestedBytes: request)
            try JSONEncoder().encode(failure).write(to: b.exhaustionURL, options: .atomic)
        }

        let fresh = budget("fresh")
        assert(fresh.selectedMiB == 2)
        assert(fresh.select("small") == 2)
        assert(fresh.megabytes(for: JITMemoryBudget.androidID) == 256)
        assert(fresh.megabytes(for: "ps4:game") == 512)
        assert(JITMemoryBudget.roundedMiB(bytes: 2 * mib) == 2)
        assert(JITMemoryBudget.roundedMiB(bytes: 2 * mib + 1) == 4)
        assert(JITMemoryBudget.roundedMiB(bytes: Int.max) == 1024)

        // A crash immediately after NULL: evidence is read by a new instance, once, before prewarming.
        let first = budget("crash")
        let run = first.begin(appID: "small", capacityBytes: 2 * mib)
        try exhaust(first, run, used: mib, request: 2 * mib)
        let reopened = budget("crash")
        let grown = reopened.recover(log: "*** FATAL SIGNAL 11")
        assert(grown?.previousMiB == 2 && grown?.nextMiB == 4)
        assert(budget("crash").megabytes(for: "small") == 4)
        assert(budget("crash").allocationMiB(fallbackMiB: 192) == 192) // A cold launch cannot predict the next game.
        assert(reopened.recover(log: "") == nil)
        try exhaust(reopened, run, used: mib, request: 2 * mib)
        assert(reopened.recover(log: "") == nil) // Several failing allocations in one run must not double repeatedly.
        assert(budget("crash").megabytes(for: "small") == 4)

        // A known large request advances through doubling steps without requiring one crash per step.
        let large = budget("large")
        let largeRun = large.begin(appID: "minecraft", capacityBytes: 2 * mib)
        try exhaust(large, largeRun, used: mib, request: 354 * mib)
        assert(large.recover(log: "")?.nextMiB == 512)
        assert(large.select("other") == 2)
        assert(large.select("minecraft") == 512)

        let unknown = budget("unknown")
        _ = unknown.begin(appID: "game", capacityBytes: 2 * mib)
        assert(unknown.recover(log: "SIGSEGV OutOfMemoryError MAP_JIT refused") == nil)
        assert(unknown.selectedMiB == 2)

        let oom = budget("oom")
        let oomRun = oom.begin(appID: "game", capacityBytes: 2 * mib)
        try exhaust(oom, oomRun, request: 3 * mib)
        assert(oom.recover(log: "memory: 42 MiB left before jetsam\n*** FATAL SIGNAL 6") == nil)
        assert(oom.selectedMiB == 2)
        try exhaust(oom, oomRun, request: 3 * mib)
        assert(oom.recover(log: "main thread ended with java.lang.OutOfMemoryError: Java heap space") == nil)

        let recovered = budget("optional")
        let optionalRun = recovered.begin(appID: "game", capacityBytes: 2 * mib)
        try exhaust(recovered, optionalRun, request: 3 * mib)
        recovered.recordUsage(bytes: mib, madeProgress: true)
        assert(recovered.recover(log: "an unrelated later crash") == nil)
        assert(recovered.selectedMiB == 2)

        let wrong = budget("wrong-run")
        let wrongRun = wrong.begin(appID: "game", capacityBytes: 2 * mib)
        try exhaust(wrong, wrongRun, request: 3 * mib, runID: UUID().uuidString)
        assert(wrong.recover(log: "") == nil)
        try exhaust(wrong, wrongRun, used: -1, request: Int.max)
        assert(wrong.recover(log: "") == nil)
        try exhaust(wrong, wrongRun, request: mib) // Fits: not exhaustion.
        assert(wrong.recover(log: "") == nil)

        let measured = budget("measured")
        _ = measured.begin(appID: "game", capacityBytes: 512 * mib)
        measured.recordUsage(bytes: 96 * mib, madeProgress: true)
        assert(budget("measured").megabytes(for: "game") == 128)
        measured.recordUsage(bytes: 1, madeProgress: true)
        assert(measured.selectedMiB == 128)

        let capped = budget("capped")
        let cappedRun = capped.begin(appID: "game", capacityBytes: 512 * mib)
        try exhaust(capped, cappedRun, request: 2048 * mib)
        assert(capped.recover(log: "")?.nextMiB == 1024)
        capped.end()
        let limitRun = capped.begin(appID: "game", capacityBytes: 1024 * mib)
        try exhaust(capped, limitRun, request: 2048 * mib)
        assert(capped.recover(log: "") == nil)

        let background = budget("background")
        let bgRun = background.begin(appID: "game", capacityBytes: 2 * mib)
        try exhaust(background, bgRun, request: 3 * mib)
        background.suspend()
        assert(budget("background").recover(log: "") == nil)
        background.resume()
        assert(FileManager.default.fileExists(atPath: background.runURL.path))
        background.end()
        assert(!FileManager.default.fileExists(atPath: background.runURL.path))

        // Missing estimates use the exact manually entered MB value. A known app always wins, in both directions.
        assert(JITMemoryBudget.validFallback(" 192 ") == 192)
        for invalid in ["", "0", "1", "-32", "1025", "3.5", "512MB", String(Int.max)] {
            assert(JITMemoryBudget.validFallback(invalid) == nil)
        }
        let fallback = budget("fallback")
        _ = fallback.select("unknown")
        assert(fallback.allocationMiB(fallbackMiB: 192) == 192)
        assert(fallback.allocationMiB(fallbackMiB: -1) == 512)
        fallback.updateEstimate(appID: "known", bytes: 12 * mib, identity: "v1")
        _ = fallback.select("known")
        assert(fallback.allocationMiB(fallbackMiB: 1024) == 16)
        assert(fallback.allocationMiB(fallbackMiB: 2) == 16)
        assert(fallback.allocationMiB(fallbackMiB: 1024, androidAtLaunch: true) == 256)
        assert(budget("new-launch").allocationMiB(fallbackMiB: 1024, androidAtLaunch: true) == 256)
        _ = fallback.select(JITMemoryBudget.androidID)
        assert(fallback.allocationMiB(fallbackMiB: 1024) == 256)
        let suiteName = "husk-budget-tests-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        assert(JITMemoryBudget.fallbackMiB(defaults: defaults) == 512)
        defaults.set(192, forKey: JITMemoryBudget.fallbackKey)
        assert(JITMemoryBudget.fallbackMiB(defaults: defaults) == 192)
        defaults.set(0, forKey: JITMemoryBudget.fallbackKey)
        assert(JITMemoryBudget.fallbackMiB(defaults: defaults) == 512)

        typealias Library = JITMemoryBudget.Library
        let big = Library(name: "libminecraftpe.so", abi: "arm64-v8a", pages: 354 * mib / 16384,
                          codePages: 200 * mib / 16384, systemCalls: 100, imports: 500)
        let estimate = JITMemoryBudget.estimateBytes(libraries: [big], complete: true)!
        assert(estimate > 354 * mib && JITMemoryBudget.roundedMiB(bytes: estimate) == 512)
        assert(JITMemoryBudget.estimateBytes(libraries: [big, big], complete: true) == estimate)
        assert(JITMemoryBudget.estimateBytes(libraries: [big], complete: false) == nil)
        let unreadable = Library(name: "bad.so", abi: "arm64-v8a", pages: nil, codePages: nil, systemCalls: 0, imports: 0)
        assert(JITMemoryBudget.estimateBytes(libraries: [unreadable], complete: true) == nil)
        let otherABI = Library(name: "libminecraftpe.so", abi: "x86_64", pages: 65000, codePages: 65000, systemCalls: 0, imports: 0)
        assert(JITMemoryBudget.estimateBytes(libraries: [big, otherABI], complete: true) == estimate)
        assert(JITMemoryBudget.estimateBytes(libraries: [], complete: true) == 2 * mib)

        // Exit replaces a speculative 512 MiB estimate with this app's measured peak plus reserve, not the whole region.
        // Earlier APKs in the same process must not be charged to the new app's next cold launch.
        let exit = budget("exit")
        exit.updateEstimate(appID: "game", bytes: 500 * mib, identity: "v1")
        _ = exit.begin(appID: "game", capacityBytes: 512 * mib, initialUsedBytes: 256 * mib)
        exit.recordUsage(bytes: 296 * mib, madeProgress: true)
        assert(exit.calibrateOnExit(bytes: 304 * mib, successful: false) == nil)
        assert(exit.calibrateOnExit(bytes: 304 * mib, successful: true) == 64)
        exit.end()
        exit.updateEstimate(appID: "game", bytes: 500 * mib, identity: "v1")
        assert(exit.megabytes(for: "game") == 64) // A report reload must not undo the measurement.
        exit.updateEstimate(appID: "game", bytes: 200 * mib, identity: "v2")
        assert(exit.megabytes(for: "game") == 256) // Updated APKs are estimated again.

        let peak = budget("peak")
        _ = peak.begin(appID: "game", capacityBytes: 256 * mib, initialUsedBytes: 100 * mib)
        peak.recordUsage(bytes: 180 * mib, madeProgress: true)
        assert(peak.calibrateOnExit(bytes: 140 * mib, successful: true) == 128)
        peak.suspend()
        peak.resume()
        let resumed = peak.begin(appID: "game", capacityBytes: 256 * mib, initialUsedBytes: 180 * mib)
        assert(resumed.initialUsedBytes == 100 * mib) // Pausing/resuming does not reset the session's baseline.
        peak.end()

        let protected = budget("failure-floor")
        let failed = protected.begin(appID: "game", capacityBytes: 2 * mib)
        try exhaust(protected, failed, request: 40 * mib)
        assert(protected.recover(log: "")?.nextMiB == 64)
        protected.end()
        _ = protected.begin(appID: "game", capacityBytes: 64 * mib)
        protected.recordUsage(bytes: 6 * mib, madeProgress: true)
        assert(protected.calibrateOnExit(bytes: 6 * mib, successful: true) == 64)
        // The phone held 16 MiB, with 10,895,360 bytes of reusable Java runtime code after fully closing PicACG.
        // Its cold measured 16 MiB budget must not be charged again to the remaining 5.6 MiB.
        let reopen = budget("reopen-java")
        let usedAfterClose = 10_895_360
        let resident = 10_747_904
        reopen.updateEstimate(appID: "pica", bytes: 2 * mib, identity: "pica-v1")
        _ = reopen.begin(appID: "pica", capacityBytes: 16 * mib)
        reopen.recordUsage(bytes: usedAfterClose, madeProgress: true)
        assert(reopen.calibrateOnExit(bytes: usedAfterClose, successful: true) == 16)
        reopen.end()
        for _ in 0..<3 {
            let cold = reopen.megabytes(for: "pica") * mib
            assert(cold > 16 * mib - usedAfterClose) // The old gate wrongly demanded a Husk restart.
            assert(reopen.additionalBytes(for: "pica", coldBytes: cold, sharedBytes: resident) <= 16 * mib - usedAfterClose)
            _ = reopen.begin(appID: "pica", capacityBytes: 16 * mib, initialUsedBytes: usedAfterClose)
            reopen.recordUsage(bytes: usedAfterClose + 147456, madeProgress: true)
            assert(reopen.calibrateOnExit(bytes: usedAfterClose + 147456, successful: true) == 16)
            reopen.end()
        }
        reopen.updateEstimate(appID: "native", bytes: 8 * mib, identity: "native-v1")
        assert(reopen.additionalBytes(for: "native", coldBytes: 8 * mib, sharedBytes: resident) == 8 * mib)
        assert(reopen.additionalBytes(for: "unknown", coldBytes: 16 * mib, sharedBytes: resident) == 16 * mib)
        assert(reopen.additionalBytes(for: "pica", coldBytes: 16 * mib, sharedBytes: 0) == 16 * mib)
        print("PASS: JIT estimates, fallback precedence, exit calibration, repeated Java reopen and native/unknown app limits")
    }
}
