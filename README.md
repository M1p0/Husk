<p align="center">
  <img src="src/app/Husk/Assets.xcassets/AppIcon.appiconset/Icon-Default.png" width="120" height="120" alt="Husk">
</p>

<h1 align="center">Husk</h1>

<p align="center">Android apps and games on your iPhone.</p>

<p align="center">
  <a href="https://itslevii.dev/docs/install">Install</a>
  ·
  <a href="https://itslevii.dev/husk">Compatibility</a>
  ·
  <a href="https://itslevii.dev">Website</a>
  ·
  <a href="https://github.com/Leviidev/Husk/releases">Releases</a>
</p>

<p align="center">
  <a href="https://github.com/Leviidev/Husk/releases"><img src="https://img.shields.io/github/downloads/Leviidev/Husk/total?style=flat-square&color=5B49EB&labelColor=111111" alt="Downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Leviidev/Husk?style=flat-square&color=5B49EB&labelColor=111111" alt="GPL-2.0-or-later"></a>
</p>

Add an APK, tap it, and it opens full-screen.

This checkout adds **Fully Close APK** for Java apps such as PicACG, in the running
app's toolbar, floating window and library detail page. It ends the app session
while keeping Husk open. The next launch creates a fresh app and screen; saved
files stay in place. Closing discards all initialized Java state, including core
and framework statics, class reflection caches, main loopers and frame schedulers.
The next launch reruns class initializers, core bootstrap, Application creation
and the launcher Activity. Linked boot metadata, native bindings and the granted
JIT memory remain available. **Pause and Return** keeps the existing resume behavior.
Closing interrupts blocked socket reads, polling and host HTTP requests. If a
native task still cannot finish, Husk shows a stopping screen and retries cleanup
automatically; the retained screen cannot be resumed. Other native game engines
do not yet offer this action.
Closing wakes blocked network calls, HTTP requests, Java waits, sleeps and class
initializers through stop events. Idle waits keep their original deadlines;
there are no periodic shutdown checks or retry timers. Other native engines
retain their ordinary host I/O waits. Shared wake descriptors are created once,
before guest code runs, and reused between sessions.
WebKit completion handlers are canceled before their owners disappear, queued
UI work from the old session is discarded, and abandoned SQLite connections,
audio and decoder resources are released after the app's threads have exited.
JIT memory uses the value entered in Settings for every app. APK scans, crashes
and successful runs do not change it. Change the value, then fully reopen Husk
to allocate a different region. Java runtime code stays resident across APK sessions.

Shutdown gate regression: `sh tests/translation-layer/run_session.sh`.
This also verifies one host `poll` per idle wait, broadcast cancellation, clean
restart, unchanged descriptor errors and no drain notifications on normal calls.
Guest network/HTTP shutdown regression (macOS):
`sh tests/translation-layer/run_session_io.sh`.
Full Java bootstrap and real frame-callback reopening regression (macOS):
`sh tests/translation-layer/run_reopen_boot.sh`.
WebKit pending-completion/session isolation regression (macOS):
`sh tests/translation-layer/run_web_session.sh`.
The Mac Java-app harness also accepts `TL_CYCLES=3` to exercise repeated starts
and stops within one process (using its existing runtime and ANGLE settings).

For Xcode development signing, use
`HUSK_ENTITLEMENTS=Husk/HuskDevelopment.entitlements` and
`-allowProvisioningUpdates`. This requests Increased Memory Limit in both the
provisioning profile and app signature while preserving the interpreter's
32 MiB Java worker stacks. Read `os_proc_available_memory()` on the device to
verify the granted budget; adding a key only to the executable is insufficient.
The separate Extended Virtual Addressing capability requires a signing team
that supports it and is omitted from this development entitlement file.

Java compatibility fixes preserve resource-array entries with repeated map names,
keep Android buttons above WebViews touchable, and correct weighted dialog
measurement so long content can scroll while the footer stays on screen. Java
launches also release the unused Dart/V8 address reservation to leave room for
threads and heap allocations on iOS. Certificate loading avoids an invalid
PKCS#12 passphrase value that could crash Husk at startup.

Java string allocation failures now propagate a preallocated `OutOfMemoryError`
instead of dereferencing a null buffer. Legacy mobile-network queries return a
disconnected `NetworkInfo`, so PicACG can check both mobile and Wi-Fi when scrolling.

Framework layout/input regressions: `python3 tests/java-framework/run.py` (a host
JDK is required; set `HUSK_JDK` if needed). Address-reservation regressions:
`sh tests/translation-layer/run_compressed_heap.sh` on macOS. The native web hit
test's build command is in `tests/translation-layer/web_hit.m`.
String allocation fault injection: `sh tests/translation-layer/run_string_oom.sh`
(macOS, a host JDK/R8, and the boot jars in `build/java-runtime`).

- **Translation layer** — the game’s own 64-bit Android code runs on the iPhone. Husk stands in for Android around it (C library, Java, GL, Vulkan, audio, input). No Android boot; a game starts in seconds.
- **Emulation** — a full LineageOS guest under QEMU, for apps the translation layer cannot run yet.
- **PS4** — fake packages and game folders on iOS 17.4+.

## Independent APK apps: proposed design

The proposed jailbreak architecture gives each APK its own iOS app identity and
process, with a versioned Husk runtime loaded at process startup. This is a
design and execution plan. An initial macOS headless-bootstrap runtime package
and dynamic-loading probe are implemented; independent app shells, the stable
APK launch interface, resource enforcement and iOS validation remain future work.
Development starts with a macOS prototype of the shared core and protocols;
iOS-specific registration, sandboxing, JIT, memory and background behavior still
require separate target-device validation.

- [架构与设计：独立应用、资源隔离及运行库升级](docs/07-independent-app-runtime-design.md)
- [执行方案：能力探针、分阶段实现与验收](docs/08-independent-app-runtime-execution-plan.md)
- [macOS M1：本地构建、动态加载验证与当前限制](docs/09-macos-runtime-m1.md)

## Install

Sideload with SideStore, AltStore, or TrollStore. iOS 15.5 or later.

**Guide:** [itslevii.dev/docs/install](https://itslevii.dev/docs/install)

Add this source in SideStore or AltStore (Sources › +). It also lists AetherPS4, Utsuro, Podium and Fathom:

```
https://itslevii.dev/repo.json
```

Or download `Husk.ipa` from [Releases](https://github.com/Leviidev/Husk/releases).

Games need [JIT](https://itslevii.dev/docs/jit), or they can run from [Play without JIT](https://itslevii.dev/docs/play-without-jit) after you import the certificate that installed Husk.

## Screenshots

<table>
  <tr>
    <td align="center"><img src="Screenshots/gta-san-andreas.jpeg" alt="GTA: San Andreas"><br><sub>GTA: San Andreas</sub></td>
    <td align="center"><img src="Screenshots/minecraft-dungeons.jpeg" alt="Minecraft Dungeons"><br><sub>Minecraft Dungeons</sub></td>
  </tr>
  <tr>
    <td align="center" width="33%"><img src="Screenshots/library.png" alt="Library"><br><sub>Library</sub></td>
    <td align="center" width="33%"><img src="Screenshots/game-settings.png" alt="Game Settings"><br><sub>Game Settings</sub></td>
    <td align="center" width="33%"><img src="Screenshots/settings.png" alt="Settings"><br><sub>Settings</sub></td>
  </tr>
</table>

## Building

On a Mac with Xcode, plus `meson`, `ninja`, `pkg-config`, `python3`, `xcodegen`, `qemu-img`, and a Rust toolchain with `aarch64-apple-ios`:

```sh
./scripts/ci_build.sh
```

That produces `build/Husk.ipa`. Later app-only rebuilds: `./scripts/package_ipa.sh` → `~/Desktop/Husk.ipa`.

This fork uses manual local builds and tests. GitHub Actions is disabled;
there is no automatic build or deployment on push.

## Licence

GPL-2.0-or-later. Husk links QEMU (GPLv2), so the shipped binary is a combined GPLv2 work. It cannot go on the App Store. See [docs/01-licensing.md](docs/01-licensing.md).
