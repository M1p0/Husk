# macOS M1：两个下载 APK 的实测记录

日期：2026-11-06。这是 **M1 初期的历史诊断**：当时动态加载、ABI 与版本持有测试通过，下面两个真实 APK 均未完成可用性验收。后续已加入 Go TLS、图形同步修复与独立 `.app`，当前实现和验收边界见[独立 App 文档](11-macos-independent-apps.md)；本页保留原失败现场。

## 1. 输入与环境

直接使用用户下载目录中的文件，测试时复制到各自的新工作目录，不修改原 APK，不复用已有应用数据。

| APK | 包名 / 体积 | SHA256 |
|---|---|---|
| `pikapika-v1.8.23-android-arm64-flutter_3.13.9.apk` | `opensource.pic2acg` / 22,562,716 字节 | `684b204ac3cdd6a3d5650debdd7dda31158b841b4d1dc13b3562e5ff4984f20a` |
| `jasmine-arm64-v1.7.22.apk` | `opensource.jmtt2mic` / 16,797,750 字节 | `d5b6eb4bbfbca5f7ab62b9f09707d51045c71566a7a9e281df1a59554c0ae0fa` |

两者均含 arm64 Flutter AOT 与 DEX；Pikapika 带 `libgojni.so`，Jasmine 带 `librust.so`。
宿主为 Apple Silicon arm64、macOS 27.0.1（26A434）。ANGLE 使用本机 VS Code Electron Framework 的 `libEGL.dylib` / `libGLESv2.dylib` 匹配库对，已检查为 macOS arm64、依赖仅系统库；没有使用 iOS ANGLE。

运行库为 `build/macos-runtime/m1-apk-validation-20261106`，profile 为 `headless-bootstrap`：

- 构建时 Git 基线：`6cbeef23724a9f324ca2dd191e6cd27a1fab19c4`，M1 实现当时在工作区，随后提交为 `1079381`。
- native 源码摘要：`7b43f96c4f228307e732792ed46e39ec65a3e0c6ea4d0bd98b57109a4350fa0f`。
- dylib SHA256：`b89bec4ea238218384f84e0a1ae0c34ac1c37b94c8a7d17a9fb19f16b76a7e2e`。

## 2. 方法与边界

[诊断器](../tools/runtime/test_apks_macos.py)先运行稳定 ABI 的包校验/配置探针，核对精确 native 源码快照，然后仅编译 `tools/javaapp-test.c`，链接上述已发布 dylib。`nm` 证明 core 函数从 dylib 导入，`otool` 和 dyld 日志证明加载的是指定版本；没有把全部 Runtime 源码再静态编进动态测试程序。

实际 APK 路径调用的是现有私有 `tl_*` 测试接口。公开 M1 ABI 仍不宣称 APK 启动能力。各例使用独立数据、临时目录、控制文件与截图目录，父进程持有共享 lease，子进程显式继承对应 FD；生命周期结束后才释放。手动 JIT 和原有线程设置没有为通过测试而缩小。

每例观察窗口约 20 秒，通过控制文件在 18 秒取截图、约 19 秒结束。退出码 0 和宿主合成帧计数不代表应用功能正常；位图均匀性检查及实际截图审阅另行进行。

另构建同源码静态单体 `javaapp-test`，使用同一资源包、ANGLE、观察设置与新数据目录对照。静态程序由 `nm` 证明 core 定义在自身，`otool`/dyld 不含 `libHuskRuntime.dylib`。

## 3. 实测结果

| APK / 运行方式 | 进程结果 | 画面与判定 |
|---|---|---|
| Pikapika 动态第 1 次 | 0.513 秒，退出 139，SIGSEGV | 无帧，无截图；失败 |
| Pikapika 动态复测 | 0.492 秒，退出 139，SIGSEGV | 无帧，无截图；失败 |
| Pikapika 静态对照 | 0.979 秒，退出 139，SIGSEGV | 同一 Go/cgo 初始化路径，无帧；失败 |
| Jasmine 动态第 1 次 | 退出 0，宿主合成 13 帧 | 全白位图，失败；不能按帧数认定成功 |
| Jasmine 动态复测 | 退出 0，宿主合成 14 帧 | 显示 Bing 地址栏、刷新及菜单，下方大片空白；仅部分界面，功能未验收 |
| Jasmine 静态对照 | 退出 0，宿主合成 11 帧 | 与动态复测截图完全相同；仍不是完整应用功能通过 |

Jasmine 动态复测与静态对照的 PNG SHA256 均为：
`19b71cc6ee9c518f0bf69b0ff8f9fe6a7c7f46b04435fd3165831b13dbc6fe6e`。

### Pikapika 的已知失败阶段

已进入 Java boot、MainActivity、Flutter JNI 和 ANGLE ES3。崩溃发生于 `mobile/Mobile.initApplication` 到 `libgojni.so` 的 Go/cgo 调用链，堆栈含 `crosscall2` 和 `Java_mobile_Mobile_initApplication`。现场指令为 `LDR X4,[X28,#0x38]`、X28 为 0，fault 地址为 `0x38`。

静态对照复现相同阶段，支持“该失败不是仅由此次动态拆分才出现”的有限结论。下一步检查 JNI→Go/cgo 入口的线程/TLS、寄存器与 loader 重写状态；尚不能确定具体根因。`res_search`、`_Unwind_*` 未解析及崩溃打印后的 syscall 警告不直接认定为致因。

### Jasmine 的已知失败阶段

已进入 Java MainActivity、Flutter Impeller（OpenGLES）、Rust 初始化、SurfaceView 和 EGL swap。帧计数来自宿主合成，不等于有效 Flutter 内容。首轮与复测画面不同，复测与静态相同；现有 harness 也记录过合成与截图竞态，当前证据不足以排除或确认特定拆分回归。

下一步分别观察 Flutter surface、Android overlay 与最终合成图，收集 Dart/平台通道错误并做可控重复测试。Bing WebView 的 JavaScript 错误是并存现象，不能据此归因白屏。

## 4. 本地证据位置

以下是本工作区的输出，未随源码提交：

按临时目录要求，下面原 `build/` 路径的证据已迁移到 `/Users/m1p0/Developer/temp/codex/Husk/macos-independent-apps-30fcdbd9/evidence/legacy-build/`；对应日志位于其 `logs/`，逐项迁移清单位于任务目录 `logs/project-temp-transfers.json`。

- 动态首轮：`build/apk-diagnostics-20261106-first/results.json`。
- 动态复测：`build/apk-diagnostics-20261106-repeat/results.json`。
- 静态对照：`build/apk-diagnostics-20261106-static/control-062545-22079/results.json`。
- 各 case 的 `run.log`、`frames/latest.bmp`、`screenshot.png`；崩溃的 Pikapika 没有帧文件。
- 动态链接证据：输出根目录的 `harness-otool-*`、`harness-nm-*` 和构建命令 JSON。
- M1 验证：`build/logs/runtime-m1-final.log`、补充的 `runtime-m1-final-probe.log`。

首轮 JSON 后补了从原 BMP 计算的均匀性字段，未改变原始退出码、帧数、日志或图片。复测由更新后的 runner 直接写出这些字段。

## 5. 复现

先按 [M1 文档](09-macos-runtime-m1.md)构建 Runtime。输出目录必须是新的目录；图形库路径需在本机实际存在且为匹配的 macOS arm64 库对：

```sh
export HUSK_TASK_TMPDIR="$(python3 -B tools/task_paths.py apk-diagnostics)"
export TMPDIR="$HUSK_TASK_TMPDIR/tmp" TMP="$HUSK_TASK_TMPDIR/tmp" TEMP="$HUSK_TASK_TMPDIR/tmp"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$HUSK_TASK_TMPDIR/tmp/pycache"
python3 tools/runtime/test_apks_macos.py \
  --runtime build/macos-runtime/m1-apk-validation-20261106 \
  --egl '/Applications/Visual Studio Code.app/Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/libEGL.dylib' \
  --gles '/Applications/Visual Studio Code.app/Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/libGLESv2.dylib' \
  --seconds 20 --out "$HUSK_TASK_TMPDIR/evidence/apk-diagnostics-new-run" \
  "$HOME/Downloads/pikapika-v1.8.23-android-arm64-flutter_3.13.9.apk" \
  "$HOME/Downloads/jasmine-arm64-v1.7.22.apk"
```

本记录保留两个 APK 的失败/未验收状态。后续兼容修复、真实独立窗口和业务交互测试应追加新证据，不用 M1 的加载成功覆盖应用层失败。
