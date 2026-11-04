// SPDX-License-Identifier: GPL-2.0-or-later
import SwiftUI

/// Keep the library alive while the Java runtime drains and drops an APK's session.
@MainActor final class APKSession: ObservableObject {
    static let shared = APKSession()
    @Published private(set) var closing = false
    @Published private(set) var revision = 0

    func close(_ app: TLApp) async -> Bool {
        guard !closing, let apk = app.apks.first,
              husk_native_loaded_apk().map({ String(cString: $0) }) == apk else { return false }
        closing = true
        defer { closing = false }
        let stopped = await Task.detached(priority: .userInitiated) { husk_native_close() }.value
        guard stopped else { return false }
        TLUnityScreen.discard(apk: apk)
        AppWindows.shared.close(app)
        NotificationCenter.default.post(name: TLUnityUIView.huskStopPlaying, object: nil)
        CrashReport.gameEnded()
        revision += 1
        HuskLog.log("ui", "APK fully closed; next launch creates a new session: \(app.label)")
        return true
    }
}

struct CloseAPKButton: View {
    let app: TLApp
    var compact = false
    @ObservedObject private var session = APKSession.shared
    @State private var confirm = false
    @State private var failed = false

    var body: some View {
        Button(role: .destructive) { confirm = true } label: {
            if session.closing { ProgressView() }
            else { Label("Fully Close APK", systemImage: "stop.circle.fill").labelStyle(.titleAndIcon) }
        }
        .font(compact ? .system(size: 12, weight: .semibold) : .body)
        .disabled(session.closing)
        .confirmationDialog("Fully close this APK?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Fully Close APK", role: .destructive) {
                Task { if !(await session.close(app)) { failed = true } }
            }
        } message: {
            Text("Husk stays open. The next launch starts the app from the beginning. Saved data is kept; unsaved changes may be lost.")
        }
        .alert("The APK is still stopping", isPresented: $failed) {
            Button("OK", role: .cancel) { }
        } message: {
            Text("A task in the app has not finished yet. Wait a moment and try Fully Close APK again. Husk has kept the session to avoid losing or corrupting its data.")
        }
    }
}
