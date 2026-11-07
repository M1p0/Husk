# 鸣潮 3.7.1：独立兼容补丁实验

状态：隔离运行已渲染启动/登录画面并播放声音。固定的 `mingchao-nativeactivity-v5` Runtime 与正式模块完成了 180 秒重启验证，未捕获到原生崩溃；账号登录、资源更新和实际游玩尚未验收。默认 Runtime 不会引入这些模块。

## 分开的组件

- `thread_probe.c`：修复 SDK 动态探针返回时留下的 16 字节栈偏移。完整模板和目标指纹匹配后才补上栈恢复，保持返回值及原寄存器记录逻辑。
- `startup_compat.c` 的 UE4 代码功能：库构造完成后，校验解密后的 Oodle 字节，显式重翻译该代码区间。初始化时才出现的 x18 指令必须按宿主 ABI 处理。使用游戏原有压缩/解压器，没有替换资源，也没有引入诊断中使用的第三方解压器。
- 同模块的 V8 功能：将 V8 11.8.172.18 配置为 16 KiB 页和解释执行，关闭仅有 4 KiB 对齐的 FlagValues 区域的冻结。通过初始化时的 flag implication 流程生效，没有跳过保护调用或伪造成功返回。
- 同模块的 Vulkan 功能：只修改两个已核对的图形后端选择函数，并配置精确匹配的 MoltenVK 1.4.2。ANGLE 的能力查询使用离屏缓冲。

通用 NativeActivity 生命周期与 ELF 弱符号绑定修复位于核心。原始代码视图和安全点重翻译仍在 `../../..` 下的 `experimental/original-code-views.patch` 中，需单独应用、构建并设置 `TL_ORIGINAL_CODE_VIEW=1`。该实验 Runtime 不能设为全局默认。

## 显式构建与加载

`target.json` 固定 APK、SDK、Runtime、探针模板、解密后字节及驱动指纹。条件不匹配时拒绝应用，重新构建 Runtime 后需要重新核对和固定版本。

```sh
python3 -B build.py --runtime <固定Runtime目录> --out <任务临时目录>/thread-probe --test-apk <目标APK>
python3 -B build.py --runtime <固定Runtime目录> --out <任务临时目录>/startup --module startup
```

以上命令需要 `HUSK_TASK_TMPDIR`。每条命令只构建明确选中的模块；SDK 原生回归验证栈偏移从 -16 变为 0、指纹和模板拒绝，以及模块关闭/启用时普通宿主线程的行为。启动模块通过实际 APK 的隔离运行验收。

进程必须分别选择以下环境开关，没有全目录扫描或默认全部启用：

- `HUSK_PATCH_MINGCHAO_THREAD_PROBE=1`
- `HUSK_PATCH_MINGCHAO_UE4_CODE=1`
- `HUSK_PATCH_MINGCHAO_V8=1`
- `HUSK_PATCH_MINGCHAO_VULKAN=1`，另需 `HUSK_PATCH_MINGCHAO_MOLTENVK=<驱动路径>`

`launch.py --config <JSON>` 检查 Runtime、APK、模块和驱动指纹，只将 JSON 中明确选择的功能及动态库传给子进程。配置字段为 `app_host`、`runtime`、`state_root`、`modules`、`module_sha256`、`features`、`moltenvk`；功能名为 `thread-probe`、`ue4-code`、`v8`、`vulkan`。状态、控制文件及日志限制在指定任务目录内，不改原安装数据或全局环境。

当前本机入口为“鸣潮（兼容补丁）.command”，保留隔离的 v5 Runtime、驱动和测试状态作为依赖。删除这些产物后需重新构建并更新配置。当前加载器只有一个初始化回调槽，与其他使用该槽的模块不能组合；尚未提供稳定的 mod 管理界面。V8 解释执行的性能和后续游戏流程需要继续验证。
