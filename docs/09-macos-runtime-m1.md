# macOS M1：动态 Runtime 与版本加载探针

日期：2026-11-06。状态：**headless-bootstrap 初版已实现并通过本地验证**。它包含真实 native Runtime 动态库和本 profile 的完整资源，但公开 ABI 目前只支持版本查询与进程上下文配置，不启动 Java、JIT 或 APK。

独立 Mac `.app`、稳定 ABI 下的 APK 启动/窗口接口、沙箱及资源控制仍属于后续工作。本文不构成 iOS 27 能力证明。对应[架构](07-independent-app-runtime-design.md)与[执行方案](08-independent-app-runtime-execution-plan.md)的 M1。

## 1. 实现范围

| 部分 | 实现 |
|---|---|
| 真实动态库 | 复用 native-runtime 源码集合，排除 iOS 应用 glue，编入 87 个源文件，不是纯元数据假库 |
| 稳定入口 | `husk_runtime_get_api(abi, table_size)` 返回版本化函数表，客户端不静态编入 Runtime |
| 上下文 | 复制身份与路径，规范化 Runtime/包/数据/缓存/配置目录，拒绝可写根与只读根重叠 |
| 生命周期 | 每进程一个活动配置，失败无残留，关闭后可重新配置同一版本；关闭等待日志回调并保护取消临界段 |
| 配置保留 | 手动 JIT 值原样记录，含显式零值；工作线程栈保留 32 MiB，此阶段不申请这些运行资源 |
| 版本包 | macOS arm64 dylib、Java/Android 基础资源、CA、许可证、公开头和逐文件 SHA256 清单 |
| 发布 | 暂存、签名/平台与来源检查、只读化、排他发布；不覆盖已有版本或替换稳定 lease |
| 探针 | 有界共享锁、完整性与目标检查、动态加载、上下文验证；每个新进程只加载一个版本 |

`APK_LAUNCH` 不可用，`hard_memory_limit` 为 `unsupported`。没有将策略模拟或路径检查当作系统强制执行能力。

## 2. 源码入口

- [公开 C ABI](../src/runtime-api/husk-runtime-v1.h)
- [上下文与日志桥接](../src/runtime/husk-runtime-v1.c)
- [构建入口](../scripts/build_macos_runtime.sh)与[包发布器](../tools/runtime/package_macos.py)
- [独立加载探针](../tools/runtime-probe.m)
- [动态 ABI 测试](../tests/runtime/context_api_test.c)与[本地集成测试](../tests/runtime/run_macos.sh)
- [实际 APK 的动态库诊断器](../tools/runtime/test_apks_macos.py)与[两个下载 APK 的实测记录](10-macos-runtime-apk-validation.md)

没有改动 iOS application target 的源码集合。这些模块由新的 Mac 构建入口使用，旧单体 harness 只作为对照。

## 3. 本地构建

要求 Apple Silicon macOS、Xcode 工具、Python 3，以及已有的 `build/java-runtime`、`build/android-system`。从仓库根目录执行：

```sh
./scripts/build_macos_runtime.sh
```

默认使用唯一 build ID，打印实际发布目录。也可指定新版本：

```sh
./scripts/build_macos_runtime.sh --store build/macos-runtime --build-id my-m1-version
```

同名版本或 lease 已存在时失败，不覆盖或自动清理。资源可用 `--java-runtime`、`--android-system` 指定；软链、缺失 boot 文件和非法 build ID 被拒绝。

```text
<store>/<build-id>/
  lib/libHuskRuntime.dylib
  include/husk-runtime-v1.h
  resources/java-runtime/
  resources/android-system/
  resources/cacert.pem
  resources/LICENSE-Husk
  manifest.json
<store>/.leases/<build-id>.lock
```

清单记录 ABI、目标平台、profile、Git 基线、实际构建源码摘要和 artifact 哈希。编译前后重新扫描源文件、头文件和构建脚本，发现修改或新增输入时拒绝发布。未提交工作区实现的来源需要结合源码摘要判断，不能仅用 Git 基线代替。

此 profile 没有 macOS ANGLE，不复制 iOS ANGLE。dylib 仅依赖已检查的系统宿主库，不需要启动器提供未解析的私有日志符号。

## 4. 加载探针

```sh
mkdir -p build/runtime-m1-tools
clang -fobjc-arc -Wall -Wextra -Wno-deprecated-declarations \
  tools/runtime-probe.m -framework Foundation \
  -o build/runtime-m1-tools/runtime-probe
mkdir -p build/runtime-demo/package build/runtime-demo/data \
  build/runtime-demo/cache build/runtime-demo/config
```

用上一步发布的 `my-m1-version`，显式传入所有根目录：

```sh
build/runtime-m1-tools/runtime-probe \
  --runtime "$PWD/build/macos-runtime/my-m1-version" \
  --package "$PWD/build/runtime-demo/package" \
  --data "$PWD/build/runtime-demo/data" \
  --cache "$PWD/build/runtime-demo/cache" \
  --config "$PWD/build/runtime-demo/config" \
  --app-id dev.husk.example --generation example-1 --jit-mib 256
```

输出 JSON 含 PID、build ID、规范化路径、配置及能力状态。`--hold-seconds` 有界保留进程与共享锁，`--lease-timeout-ms` 控制取锁超时。ABI 或配置错误后结束本次进程；不通过 `dlclose` 热切换 Runtime。

## 5. 本地验收

```sh
sh tests/runtime/run_macos.sh
```

默认在自有临时目录构建两个版本，编译只通过动态库入口调用的客户端，再做真实子进程验收。复用已有版本时，先复制到隔离测试仓库：

```sh
sh tests/runtime/run_macos.sh --versions \
  build/macos-runtime/m1-v1 build/macos-runtime/m1-v2
```

测试只清理自己的目录、精确结束自己的子进程，不使用 `pkill` 或公共 `/tmp/husk-*` 清理。

2026-11-06 最终源码重建与测试日志为本工作区 `build/logs/runtime-m1-final.log`，通过：

- 真实动态入口、ABI/表长拒绝；probe 未静态编入 Runtime。
- 字符串复制、路径规范化、失败无残留及错误缓冲区边界。
- 单活动配置、8 线程并发 configure、关闭后重开。
- 两个新进程加载不同版本，强杀 A 后 B 及其版本持有保持。
- 同版本多个读者：最后一个退出前排他锁始终不可得，正常退出后释放。
- 锁超时、缺失/FIFO/软链拒绝，以及取锁期间对象替换检测。
- artifact 损坏、缺失、软链、空清单和路径越界在加载前拒绝。
- 超大 manifest 在有界读取前检查，显式零 JIT CLI 配置原样返回。
- JIT 值、32 MiB 栈和能力状态不被隐式改变。

构建器另验证同名版本拒绝覆盖，以及编译期间修改/新增源码或头文件时拒绝发布。这是 `macos-functional` 证据；`build/` 包和原始日志为本地输出。

## 6. 限制与后续

1. `flock` 协调遵守协议的发布/清理，不阻止有权限的进程 `unlink`。本阶段没有自动 GC，稳定锁对象不因版本清理而重建。
2. inode 检查只覆盖取得与校验窗口，不追溯先前已完成的替换。同用户未沙箱化进程仍可改权限，只读位不是 OS 沙箱。
3. 哈希校验一致性，不认证发布者；ad-hoc 签名不是正式分发信任链。
4. 本 ABI 不启动 Java/APK、不提供窗口或硬内存限制；core 编入不等于全部功能已经通过稳定 ABI 开放。
5. 实际 APK 可先通过明确标为内部测试接口的兼容 harness 诊断，不能据此声称稳定 APK ABI 或 M2 外壳已交付。
6. iOS 安装、JIT/TXM、沙箱、Jetsam、辅助资源、锁屏后台和设备 GPU 均需单独验证。

后续以此版本包和 ABI 进入 M2 独立 Mac 外壳与 APK 启动接口。

## 7. 实际 APK 的诊断入口

`tools/runtime/test_apks_macos.py --help` 给出完整参数。传入 Runtime 版本、匹配的 macOS arm64 EGL/GLES 库和 APK 文件后，脚本先校验包与精确 native 源码快照，再仅编译 `tools/javaapp-test.c`，链接所选 dylib，保存 `nm/otool` 证据。这个兼容性 harness 使用私有 `tl_*` 接口，不扩充稳定 ABI 的能力声明。

各 APK 使用独立数据、控制与帧目录，有进程级超时和精确回收。显式继承的共享锁 FD 保持到 harness 退出；不会清理其他测试程序。原有 harness 只增加 `TL_HTTP_LEGACY` 与 `TL_FRAMEDIR` 路径覆盖，生产 Runtime、手动 JIT 与线程栈未改。

报告分别记录退出码、崩溃、帧计数、主位图是否均匀以及截图路径。白帧和正常退出不能认定为正确 UI；实际界面仍须检查。首次用户 APK 实测的结果与静态对照见[实测记录](10-macos-runtime-apk-validation.md)。
