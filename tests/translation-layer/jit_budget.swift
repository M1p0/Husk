import Foundation

@main struct JITMemorySettingsTests {
    static func main() throws {
        let suite = "husk-manual-jit-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let mib = JITMemoryBudget.mib
        assert(JITMemoryBudget.megabytes(defaults: defaults) == 512)
        assert(JITMemoryBudget.validMegabytes(" 192 ") == 192)
        for text in ["", "-1", "0", "1", "1025", "3.5", "huge", String(Int.max)] {
            assert(JITMemoryBudget.validMegabytes(text) == nil)
        }
        // Keep the original preference key and exact values, including non-power-of-two choices.
        assert(JITMemoryBudget.settingKey == "husk.jit.fallbackMegabytes")
        for value in [2, 16, 192, 384, 512, 1024] {
            defaults.set(value, forKey: JITMemoryBudget.settingKey)
            assert(JITMemoryBudget.megabytes(defaults: defaults) == value)
            assert(JITMemoryBudget.bytes(defaults: defaults) == value * mib)
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let files = ["jit-memory.json", "jit-memory-run.json", "jit-memory-exhaustion.json"]
        for file in files { try Data("{\"megabytes\":16}".utf8).write(to: root.appendingPathComponent(file)) }
        let unrelated = root.appendingPathComponent("saved-game.json")
        try Data("saved".utf8).write(to: unrelated)
        defaults.set(384, forKey: JITMemoryBudget.settingKey)
        JITMemoryBudget.removeLegacyProfiles(in: root)
        assert(files.allSatisfy { !FileManager.default.fileExists(atPath: root.appendingPathComponent($0).path) })
        assert(FileManager.default.fileExists(atPath: unrelated.path))
        assert(JITMemoryBudget.bytes(defaults: defaults) == 384 * mib)
        JITMemoryBudget.removeLegacyProfiles(in: root) // migration is safe on subsequent launches
        for value in [0, -1, 1025] {
            defaults.set(value, forKey: JITMemoryBudget.settingKey)
            assert(JITMemoryBudget.megabytes(defaults: defaults) == 512)
        }
        print("PASS: exact manual JIT sizes, saved preference compatibility, validation and obsolete-profile removal")
    }
}
