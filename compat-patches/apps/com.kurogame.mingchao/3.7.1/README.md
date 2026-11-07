# 鸣潮 3.7.1：独立兼容补丁实验

状态：通过全局运行库与独立补丁渲染启动/登录画面并播放声音。账号登录、资源更新和实际游玩尚未验收。普通应用不会加载这些模块。

## 分开的组件

- `thread_probe.c`：修复 SDK 动态探针返回时留下的 16 字节栈偏移。完整模板和目标指纹匹配后才补上栈恢复，保持返回值及原寄存器记录逻辑。
- `startup_compat.c` 的 UE4 代码功能：库构造完成后，校验解密后的 Oodle 字节，显式重翻译该代码区间。初始化时才出现的 x18 指令必须按宿主 ABI 处理。使用游戏原有压缩/解压器，没有替换资源，也没有引入诊断中使用的第三方解压器。
- 同模块的 V8 功能：将 V8 11.8.172.18 配置为 16 KiB 页和解释执行，关闭仅有 4 KiB 对齐的 FlagValues 区域的冻结。通过初始化时的 flag implication 流程生效，没有跳过保护调用或伪造成功返回。
- 同模块的 Vulkan 功能：只修改两个已核对的图形后端选择函数，并配置精确匹配的 MoltenVK 1.4.2。ANGLE 的能力查询使用离屏缓冲。

通用 NativeActivity、ELF、代码视图及安全点重翻译位于核心。模块只使用 `src/runtime-api/husk-runtime-patch-v1.h` 的版本化接口，以不透明句柄访问库、请求线程入口、注册初始化回调并修改已匹配的代码；不读取 Runtime 私有对象，不依赖 Runtime 二进制哈希、构建号或线程 thunk 偏移。所有应用保存 `runtime: default`，每次冷启动使用当前全局版本。

## 显式构建与加载

`target.json` 固定 APK、SDK、探针模板、解密后字节及驱动指纹，并声明补丁 ABI 和所需能力。兼容的全局 Runtime 更新无需重编译补丁；缺少接口或能力时明确拒绝加载。

```sh
python3 -B build.py --out <任务临时目录>/thread-probe
# 可加 --runtime <Runtime目录> --test-apk <目标APK> 执行原生回归
python3 -B build.py --out <任务临时目录>/startup --module startup
```

以上命令需要 `HUSK_TASK_TMPDIR`。每条命令只构建明确选中的模块；SDK 原生回归验证栈偏移从 -16 变为 0、指纹和模板拒绝，以及模块关闭/启用时普通宿主线程的行为。启动模块通过实际 APK 的隔离运行验收。

进程必须分别选择以下环境开关，没有全目录扫描或默认全部启用：

- `HUSK_PATCH_MINGCHAO_THREAD_PROBE=1`
- `HUSK_PATCH_MINGCHAO_UE4_CODE=1`
- `HUSK_PATCH_MINGCHAO_V8=1`
- `HUSK_PATCH_MINGCHAO_VULKAN=1`，另需 `HUSK_PATCH_MINGCHAO_MOLTENVK=<驱动路径>`

`launch.py --config <JSON>` 检查 Runtime 的补丁接口要求，以及 APK、模块和驱动指纹，只将 JSON 中明确选择的功能及动态库传给子进程。配置字段为 `app_host`、`runtime`、`state_root`、`modules`、`module_sha256`、`features`、`moltenvk`；功能名为 `thread-probe`、`ue4-code`、`v8`、`vulkan`。状态、控制文件及日志限制在指定任务目录内，不改原安装数据或全局环境。

也可用 `build_pack.py --thread-probe <探针模块> --startup <启动模块> --moltenvk <驱动> --out <任务目录中的包目录>` 生成安装器兼容包。在安装器中导入输出的 `compatibility.json`，选中鸣潮并选择“鸣潮 3.7.1 兼容补丁（实验）”，自动保存 Vulkan 和补丁配置，运行库跟随全局。此后普通 `.app` 或安装器“打开应用”都读取该应用配置；产物已复制到中央版本仓库，不再依赖临时启动脚本。包内没有 Runtime，导入不改变全局版本，也不自动启用补丁。

本机旧的“鸣潮（兼容补丁）.command”现转发到普通 `.app`，两者共用已保存配置。独立 `launch.py` 仍可用于任务目录内的隔离诊断。公共接口最多支持 16 个初始化回调，Runtime 重复打开不重复注册。V8 解释执行的性能和后续游戏流程需要继续验证。
