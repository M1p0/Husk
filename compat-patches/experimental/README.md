# 通用机制实验

`original-code-views.patch` 保存原始代码读取视图与执行视图分离的实验及配套用例。它不依赖游戏包名、库哈希或固定偏移，属于尚未完成的通用机制实验。

该实验已经从默认源码移出。需要在独立 checkout 中显式检查、应用并构建：

```sh
git apply --check compat-patches/experimental/original-code-views.patch
git apply compat-patches/experimental/original-code-views.patch
```

应用补丁并构建后，只有设置 `TL_ORIGINAL_CODE_VIEW=1` 的目标进程才尝试启用代码视图。不得把实验 Runtime 选为全局默认。配套用例入口是 `tests/translation-layer/run_guest_tls.py --code-views`；测试产物应通过 `HUSK_TASK_TMPDIR` 和 `--out` 指向用户指定的任务临时目录。

已通过小型用例验证原始指令读取、ADR/ADRP 读取代码中的数据、延迟索引读取、同一指令既作为字面量读取又执行、函数地址保持、从数据视图形成的代码指针执行、可变数据共享及部分代码写入。延迟索引读取在 v7 失败、v8 通过；同一指令的读取和执行在 v8 失败、v9 通过。

在显式安全点可调用实验接口 `tl_ld_retranslate_range(lib, elf_vaddr, bytes)`，对初始化期间解密后的代码重新翻译。调用方必须保证该范围没有线程正在执行或修改。接口限制在单个代码区间、校验边界，并刷新新生成的 stub 和入口的指令缓存；它不会自动监测、翻译任意运行期代码写入。

`test_code_refresh.py --runtime <实验Runtime> --linker <ELF lld> --out <任务临时目录>` 使用独立 Android ELF 验证后写入的 x18 指令、跨 TLS 陷阱、连续八次重翻译和越界拒绝。首次实现漏刷新增 stub 的缓存时该用例失败，修复后连续八个独立进程通过。已有代码视图和 TLS 回归也通过。

隔离运行已接通 NativeActivity；对目标库解密后的 Oodle 区间显式重翻译后，游戏原生压缩/解压均能运行，已观察到启动/登录画面和音频。此结果依赖独立的游戏补丁，不能视作所有 APK 的通用验收。

当前限制：只覆盖部分 macOS JIT 镜像；不覆盖 iOS、AOT、Go 镜像及代码/可变数据混页。原始字节读取尚未覆盖所有寻址方式；没有自动处理并发自修改代码，远距离回退尚未覆盖独立用例。该 Runtime 保持实验状态，不应设置为全局默认。
