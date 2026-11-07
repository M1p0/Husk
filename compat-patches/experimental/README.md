# 通用机制实验

未完成的通用机制继续放在此目录，不进入默认构建。

原始代码视图与安全点重翻译已整理为 macOS arm64 JIT 的显式可选接口，实现在核心 loader。原实验补丁从当前目录移除，其历史实现可在 Git 中查阅；无需再应用 `.patch` 或构建按游戏命名的 Runtime。

接口默认关闭，必须在加载任何 guest 库前调用 `tl_ld_enable_code_views()`。环境变量 `TL_ORIGINAL_CODE_VIEW` 本身不能启用它。普通应用保持原映射；鸣潮模块在检查补丁 ABI 与能力后请求启用。AOT、Go、构建镜像及代码/可变数据混页保持原映射，调用者须用 `tl_ld_code_writable()` 检查能力。读取覆盖字面量和局部 ADR/ADRP 派生的数据地址；任意寻址与并发自修改代码不属于此接口契约。

`tl_ld_retranslate_range()` 只接受单个可用代码区间内对齐的有限范围，调用者必须保证没有线程正在执行或修改该范围。游戏的安全点、偏移、解密字节前置条件继续由独立游戏模块提供。

独立用例为 `tests/translation-layer/run_guest_tls.py`（普通模式及 `--code-views`）、`run_code_refresh.py` 和 `run_code_view_edges.py`（默认关闭、混页拒绝、超过 128 MiB 的 TLS/CTR/SVC/ADR 回退）。接口合同和其他 APK 回归独立于鸣潮启动验收。
