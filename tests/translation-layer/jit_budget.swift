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
        assert(budget("crash").selectedMiB == 4)
        assert(reopened.recover(log: "") == nil)
        try exhaust(reopened, run, used: mib, request: 2 * mib)
        assert(reopened.recover(log: "") == nil) // Several failing allocations in one run must not double repeatedly.
        assert(budget("crash").selectedMiB == 4)

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
        assert(budget("measured").selectedMiB == 128)
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
        print("PASS: JIT budget persistence, doubling, measurements, crash attribution, OOM exclusion, lifecycle and ceiling")
    }
}
