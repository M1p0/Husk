# macOS 独立 App：发布与验收记录

日期：2026-11-06。此记录区分已发布实现、已完成回归与尚未完成的真实窗口验收，不代表 iOS 或任意 APK 兼容性通过。

## 当前产物

- `build/macos-apps/Pikapika.app`，原 APK SHA256 为 `684b204ac3cdd6a3d5650debdd7dda31158b841b4d1dc13b3562e5ff4984f20a`。
- `build/macos-apps/Jasmine.app`，原 APK SHA256 为 `d5b6eb4bbfbca5f7ab62b9f09707d51045c71566a7a9e281df1a59554c0ae0fa`。
- `build/macos-runtime/macos-app-release-20261106-v1`、`macos-app-release-20261106-v2`。

两套 Runtime 都是 `macos-app` profile，各包含 407 个带 SHA256 的 artifact，目录为 `0555`，完整源码基线为 `924cde0be434d6e65258804a74a899ece459d1f9`，native 源码摘要为 `714e7b251be965b986e3cb8d4f057d827c7915d3bab376283533665668dd824d`。两套 `.app` 用同一版轻量宿主构建，通过严格签名验证；安装器解析并复制原 APK，没有修改其内容。具体二进制摘要保存在验收目录的 `release-inventory.json`。

设计、运行方法与升级流程见[实现文档](11-macos-independent-apps.md)，整体架构与后续执行顺序见[设计](07-independent-app-runtime-design.md)和[执行方案](08-independent-app-runtime-execution-plan.md)。

## 已完成检查

| 检查 | 结果与边界 |
|---|---|
| Bootstrap 与 app ABI | 通过版本/表长协商、配置复制、并发 configure、单次 app 启动及错误参数拒绝；不等于执行 APK 的界面验收 |
| 进程与版本持有 | 两个真实探针进程分别加载 v1/v2；强杀 A 后 B 继续存活并持有自己的版本锁；普通退出、多个读者、锁替换与超时检查通过 |
| 包完整性 | 缺失/篡改 artifact、路径穿越、symlink、FIFO lease、错误目标与未完成发布的可写根目录，在加载前拒绝 |
| 图像框架 | 发布版自身 dex 上的 `ImageReaderBufferTest`、`NativeSurfaceLayerTest` 通过，没有使用开发 dex 覆盖 |
| 原生运行时 | 临时文件路径、8 线程跨库 Android TLS 与小栈调用、ASan NativeWindow/EGL 引用回归通过；没有以整机 OOM 作为隔离证明 |
| 临时目录 | 正常目录与 symlink/越界输出拒绝检查通过；发布暂存和正式目标必须同卷，跨父目录发布完成只读化后才释放独占 lease |
| 仓库与 CI/CD | 仅 `main`；GitHub Actions 的仓库开关为 `enabled: false`，没有自动构建或部署 |

图像回归的第一次启动因未指定本机 JDK，使用了系统 Java 占位程序而失败；设置 `HUSK_JDK` 为已安装的 OpenJDK 21 后重跑通过，两个记录均保留，没有覆盖失败日志。

## 真实窗口验收的当前状态

首次最终验收 `real-apks-release-a` 已成功启动原始 Pikapika，GPU 帧显示其原有计算器页面。采集实际 AppKit 窗口时，`screencapture -l` 返回 `could not create image from window`，因此尚未执行该轮的正常激活、退出和重开流程，也未开始 Jasmine 的该轮验证。

同一测试环境的诊断结果为：屏幕采集权限 `true`，当前会话 `CGSSessionScreenIsLocked=true`。这是实际桌面被锁定，不能把 GPU 截图代替包含原生 WebView 的窗口验收。所有自有测试进程已退出，两版 lease 均已释放。需要正常解锁桌面后，使用新证据目录继续完整验收。

待完成场景：

1. 两个 APK 各自正常激活，Pikapika 按应用提示退出后重开，验证其退出码和持久数据。
2. 同时运行的 A/B 保持 v1，选择 v2 后，仅重新启动的 B 加载 v2，APK 内容和已安装宿主保持不变。
3. 对 A 注入崩溃，B 保持原 PID/代次，并完成真实输入、取消对话框和渲染。
4. 当前宿主上的 Java/WebKit fixture：原生点击、中文文本、WebView、后台计时、彻底关闭和同数据重开。
5. 禁用帧文件捕获和测试点击，Pikapika 正常窗口连续冷启动三次，检查真实呈现没有白屏。

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
  evidence/release-inventory.json
  evidence/surface-images-release-jdk/results.json
  evidence/real-apks-release-a/results.json
  evidence/bionic-temp-paths-final/results.log
  evidence/guest-tls-temp-policy/results.log
  evidence/native-window-temp-policy/results.log
```

已核实归属的 20 项历史 `/private/tmp` 文件和目录，按原任务迁移到 `/Users/m1p0/Developer/temp/codex/Husk/recovered-husk-*/private-tmp/`，逐项来源与内容摘要见迁移清单。旧的项目内调试证据迁入本任务 `evidence/legacy-build/`，正式 App、Runtime 和源码留在项目。

通过[实现文档](11-macos-independent-apps.md)设置统一任务环境后，可复现无需桌面的回归：

```sh
sh tests/runtime/run_macos.sh --versions \
  build/macos-runtime/macos-app-release-20261106-v1 \
  build/macos-runtime/macos-app-release-20261106-v2

HUSK_JDK=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
python3 -B tests/translation-layer/run_surface_images.py \
  --runtime build/macos-runtime/macos-app-release-20261106-v2
```

桌面解锁后，真实双 APK 测试使用新的输出目录，不复用失败轮次的应用数据：

```sh
python3 -B tests/macos/test_real_apks.py \
  --pika build/macos-apps/Pikapika.app \
  --jasmine build/macos-apps/Jasmine.app \
  --runtime-v1 build/macos-runtime/macos-app-release-20261106-v1 \
  --runtime-v2 build/macos-runtime/macos-app-release-20261106-v2 \
  --out "$HUSK_TASK_TMPDIR/evidence/real-apks-new-run"
```
