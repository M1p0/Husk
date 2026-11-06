# macOS 独立 App：发布与验收记录

日期：2026-11-06。**有限双 APK 的真实窗口、进程终止隔离、版本切换、后台及冷启动验收已通过。** 这不代表账户业务、长期内存、iOS 或任意 APK 兼容性通过。

## 当前产物

- `build/macos-apps/Pikapika.app`，原 APK SHA256 为 `684b204ac3cdd6a3d5650debdd7dda31158b841b4d1dc13b3562e5ff4984f20a`。
- `build/macos-apps/Jasmine.app`，原 APK SHA256 为 `d5b6eb4bbfbca5f7ab62b9f09707d51045c71566a7a9e281df1a59554c0ae0fa`。
- 当前运行库：`build/macos-runtime/macos-app-close-20261106-v1`、`macos-app-close-20261106-v2`，默认选中后者。

两套当前 Runtime 都是 `macos-app` profile，各包含 407 个带 SHA256 的 artifact，目录为 `0555`，完整源码基线为 `fa155f28bcc973cde4968114ff1313ea512bf8d6`，native 源码摘要为 `8bce2bb0470f084f2e1e3da118c8dd765ceeb92894870549b730a90b5df5c101`。两套 `.app` 仍使用先前构建的轻量宿主，通过严格签名验证；宿主与 APK 的摘要保持不变，新进程仅加载新运行库，未重新安装或打包 APK。旧的 `macos-app-release-20261106-v1/v2` 保留为历史版本，没有覆盖。

设计、运行方法与升级流程见[实现文档](11-macos-independent-apps.md)，整体架构与后续执行顺序见[设计](07-independent-app-runtime-design.md)和[执行方案](08-independent-app-runtime-execution-plan.md)。

## 已完成检查

| 检查 | 结果与边界 |
|---|---|
| Bootstrap 与 app ABI | 通过版本/表长协商、配置复制、并发 configure、单次 app 启动及错误参数拒绝；不等于执行 APK 的界面验收 |
| 进程与版本持有 | 两个真实探针进程分别加载 v1/v2；强杀 A 后 B 继续存活并持有自己的版本锁；普通退出、多个读者、锁替换与超时检查通过 |
| 包完整性 | 缺失/篡改 artifact、路径穿越、symlink、FIFO lease、错误目标与未完成发布的可写根目录，在加载前拒绝 |
| 图像框架 | 初次发布版自身 dex 上的 `ImageReaderBufferTest`、`NativeSurfaceLayerTest` 通过；当前版本另外通过下述真实 Java/WebKit 窗口测试，没有使用开发 dex 覆盖 |
| 原生运行时 | 临时文件路径、8 线程跨库 Android TLS 与小栈调用、ASan NativeWindow/EGL 引用回归通过；没有以整机 OOM 作为隔离证明 |
| 临时目录 | 正常目录与 symlink/越界输出拒绝检查通过；发布暂存和正式目标必须同卷，跨父目录发布完成只读化后才释放独占 lease |
| 仓库与 CI/CD | 仅 `main`；GitHub Actions 的仓库开关为 `enabled: false`，没有自动构建或部署 |

图像回归的第一次启动因未指定本机 JDK，使用了系统 Java 占位程序而失败；设置 `HUSK_JDK` 为已安装的 OpenJDK 21 后重跑通过，两个记录均保留，没有覆盖失败日志。

## 已完成的真实窗口验收

桌面正常解锁后，使用真实 WindowServer 截图继续测试。最终有效双 APK 轮次为 `real-apks-closefix-b`，截图均已人工复核：

| 场景 | 实际结果 |
|---|---|
| Pikapika 正常首次流程 | 计算器输入原有激活内容，应用显示 Tips；点击应用自己的关闭按钮退出码为 0。同数据新进程进入 Account 页 |
| Jasmine 正常首次流程 | 原生 WKWebView 显示 Bing；在地址栏输入 `jm://start` 并提交，进入中文登录页 |
| 运行时升级 | 两应用同时运行 v1；选择 v2 后旧 PID 仍为 v1。仅关闭/重开 Jasmine 后，它加载 v2，Pikapika 保持 v1；没有 `--runtime` 启动覆盖 |
| 非合作终止隔离 | 强制 `SIGKILL` 结束 Pikapika PID 72440，退出码 -9，v1 lease 立即可独占。Jasmine PID 72798、代次不变，帧数 24→88，并能打开账号对话框、输入文字、正确取消，账号仍为空 |
| 普通关闭后的生命周期 | Jasmine 关闭退出码 0，新 PID/代次使用同一数据目录；最终两个 Runtime 的 lease 均释放，所有自有进程已退出 |
| 无诊断帧捕获冷启动 | `window-no-capture-closefix-srgb` 三次均未传 `--frame-dir`、`--runtime`，没有输入或 GPU 截图请求；实际窗口均显示 Account，退出码均 0，instance lock 释放 |
| Java/WebKit 集成 | `fixture-closefix` 的原生 Java 点击、Latin→中文全选替换、真实 WebView 点击变色并显示 `WEB clicks=1`、同数据重开与再次输入通过 |
| 后台 | fixture 窗口隐藏且未激活时，tick 从 8→12；恢复后界面正常。两次 finish 都完成 `onDestroy`、Runtime STOPPED（5）与清理，退出码 0 |

上述故障注入验证的是**无法被客体阻止的进程死亡**，没有声称测试了 CPU 真正非法访存或 GPU/内核故障。三次无捕获窗口的粉色头部，在 ColorSync 转到 sRGB 后均为 `f48fb1`、匹配率 100%；没有降低原 70% 门槛或放宽每通道容差 12。

两个大型 APK 的部分原生后台线程不会在三秒内自行收拢，因此 stop 返回“保留资源”状态，宿主随后退出整个进程，由内核回收映射并释放锁。这证明完整进程边界下的关闭/重开；不能称为同一进程内全部 Runtime 清理成功。Java fixture 则实际完成了 Runtime 内部收拢。手动 JIT 仍为每进程 512 MiB，Java 启动 worker 仍为 32 MiB，没有通过缩减这些设置迁就测试。

### 发现并修正的问题

- `real-apks-release-a` 被真实锁屏阻断，原始采集失败与锁屏诊断保留。
- `real-apks-release-b` 重开时，脚本误把上代 Running 状态和旧帧当作新进程就绪；现在先核对当前 PID，不改写应用数据。
- `real-apks-release-c` 的用户发送 SIGSEGV 没有令进程退出，不能把发出信号等同于真实崩溃；最终改为明确记录 SIGKILL 的非合作进程终止。
- 关闭窗口原先取消 Go/SQLite 文件 I/O，导致后台 panic 并以 2 退出。新版本在本 profile 的终止阶段保留正常文件 I/O，仍停止 Java 入口；未收拢原生工作随进程结束，不尝试重启或卸载运行库。最终日志没有这个 panic，三次正常关闭均为 0。
- `real-apks-closefix-a` 虽通过机械检查，视觉复核发现取消坐标点中本地 Confirm，测试文字留在本地账号栏，未提交登录认证。该轮视觉验收明确失败；修正左侧 Cancel 坐标后，以全新数据重新执行，最终 `closefix-b` 账号保持为空。
- 首次无捕获检测把显示器 ICC 中的 RGB 直接与 Flutter sRGB 比较，误判粉色头部。保留原图，新增系统 ColorSync 转换后的独立分析图；实测 `e38ba7` 转为 `f48fb1`，最终三轮均通过。

首次初始化入口和不提交账户的测试边界见实现文档。没有可用账户，因此认证、远端内容和付费/授权业务没有通过记录。

## 尚未解决的交付限制

独立进程具有私有堆、线程、可写运行时状态与 JIT，A 的崩溃不会沿同一进程堆栈直接终止 B。当前宿主没有可用的系统强制 footprint limit，也尚未建立操作系统沙箱，不能承诺 A 任意分配内存后 B 永不受系统总内存或 GPU 压力影响。

现有 DVM 缺少可达性 GC 和 Java 终结调度，派生 Bitmap 的长期内存回收尚未实现，不能通过单次渲染或有限帧队列证明长期稳定。完整根集合、线程停顿和原生像素所有权的实施依赖见[DVM 内存回收方案](12-dvm-memory-reclamation-plan.md)。运行库版本自动回收、APK 自身安全更新、iOS 系统注册/签名/JIT/后台与权限适配也尚未交付。

## 证据与复现

本机证据统一保存在：

```text
/Users/m1p0/Developer/temp/codex/Husk/macos-independent-apps-30fcdbd9/
  logs/private-tmp-transfers.json
  logs/project-temp-transfers.json
  logs/runtime-release-regression.log
  logs/window-capture-diagnostic.json
  logs/window-capture-unlocked.json
  logs/runtime-close-regression.log
  evidence/release-inventory.json
  evidence/close-release-inventory.json
  evidence/surface-images-release-jdk/results.json
  evidence/real-apks-release-a/results.json
  evidence/real-apks-closefix-b/results.json
  evidence/real-apks-closefix-b/visual-review.json
  evidence/window-no-capture-closefix-srgb/results.json
  evidence/fixture-closefix/results.json
  evidence/bionic-temp-paths-final/results.log
  evidence/guest-tls-temp-policy/results.log
  evidence/native-window-temp-policy/results.log
```

已核实归属的 20 项历史 `/private/tmp` 文件和目录，按原任务迁移到 `/Users/m1p0/Developer/temp/codex/Husk/recovered-husk-*/private-tmp/`，逐项来源与内容摘要见迁移清单。旧的项目内调试证据迁入本任务 `evidence/legacy-build/`，正式 App、Runtime 和源码留在项目。

通过[实现文档](11-macos-independent-apps.md)设置统一任务环境后，可复现无需桌面的回归：

```sh
sh tests/runtime/run_macos.sh --versions \
  build/macos-runtime/macos-app-close-20261106-v1 \
  build/macos-runtime/macos-app-close-20261106-v2

HUSK_JDK=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
python3 -B tests/translation-layer/run_surface_images.py \
  --runtime build/macos-runtime/macos-app-close-20261106-v2
```

真实双 APK 测试需要已解锁桌面、新的输出目录，不复用失败轮次的应用数据：

```sh
python3 -B tests/macos/test_real_apks.py \
  --pika build/macos-apps/Pikapika.app \
  --jasmine build/macos-apps/Jasmine.app \
  --runtime-v1 build/macos-runtime/macos-app-close-20261106-v1 \
  --runtime-v2 build/macos-runtime/macos-app-close-20261106-v2 \
  --out "$HUSK_TASK_TMPDIR/evidence/real-apks-new-run"

python3 -B tests/macos/test_window_launch.py \
  --app build/macos-apps/Pikapika.app \
  --state "$HUSK_TASK_TMPDIR/evidence/real-apks-new-run/pika-state" \
  --runtime build/macos-runtime/macos-app-close-20261106-v2 \
  --out "$HUSK_TASK_TMPDIR/evidence/window-new-run" \
  --runs 3 --expect-top-rgb f48fb1 --no-activate
```
