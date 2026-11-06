# macOS APK 安装器与应用管理

日期：2026-11-06。安装器位于 `/Users/m1p0/Applications/Husk APK Installer.app`，显示名称为“Husk APK 安装器”。

## 使用

1. 双击 `.apk`，安装器显示应用名称、包名、版本和文件位置。点击“安装”完成安装，也可以取消。多个 APK 会依次处理。
2. 新应用默认安装到 `~/Applications/Husk Apps/`。在安装器里选中应用，点击“打开应用”；也可以在 Finder 双击 `.app`。
3. 列表支持按名称、包名搜索，以及在 Finder 中显示应用、打开数据目录和卸载。退出安装器不会结束已启动的独立应用。
4. 已安装的 APK 再次打开时，会选中原有应用，不重复安装、不覆盖旧安装。

列表自动扫描个人/系统应用目录、`~/Applications/Husk Apps`、本仓库原来的 `build/macos-apps` 和已登记的安装目录。因此原有 Jasmine、Pikapika 无需重新安装即可显示。命令行安装器会自动登记自定义安装目录；手动移动应用到其他位置后，可点击“添加应用目录…”将其加入。列表枚举这些目录直接包含的有效 Husk `.app`，不会递归扫描整块磁盘或把普通 Android 模拟器里的应用当作已安装项。

卸载前必须完全退出该应用。默认仅删除 `.app`，保留其 `~/Library/Application Support/Husk/Apps/<Bundle ID>/` 数据。重新安装同包 APK 使用相同身份和数据目录。只有在卸载对话框勾选“同时删除应用数据（不可恢复）”时才删除数据；原 Downloads APK 不会删除。当前不支持直接覆盖更新 APK，仍需先卸载再安装；这不等于已经实现 Android 签名连续性或数据版本回滚验证。

APK 默认打开方式已经设置。若以后被其他程序改变，可以在安装器中点击“设为 APK 默认安装器”。文件关联只针对 APK，不接管 ZIP 文件。

## 构建与依赖

```sh
python3 -B tools/macos/build_installer.py
```

默认构建到当前用户 `~/Applications/Husk APK Installer.app`；已有路径不会被覆盖。可用 `--output` 指定新目录，`--applications` 设置此安装器安装应用的默认位置，`--runtime-store` 设置共享运行库位置。构建时可用 `--register-apk /absolute/path/sample.apk` 完成系统注册和默认关联，完成后会核对系统实际返回的默认程序。

安装器自带 AppKit 程序、安装管理后端、通用 APK 图标和已编译的 `HuskAppHost`。日常安装不再编译启动器，也不需要源代码目录；仍需要配置中的本机 Python、macOS Command Line Tools 中的平台检查工具，以及完整的共享 Runtime store。当前配置使用 Homebrew 的稳定 `opt` Python 路径，避免固定到单个 Cellar 小版本。运行库位置仍为 `/Users/m1p0/Developer/Projects/Husk/build/macos-runtime`，必须保留；其默认版本选择规则与[独立 App 实现](11-macos-independent-apps.md)相同。

管理目录登记保存在 `~/Library/Application Support/Husk/Installer/registry.json`。安装器不承载 APK 执行，也没有常驻运行库服务；查看列表不启动 APK。

## 实现

| 文件 | 职责 |
|---|---|
| `tools/macos/HuskInstaller.m` | 原生窗口、文件打开事件、多文件队列、列表/搜索、启动/卸载确认、默认关联 |
| `tools/macos/build_installer.py` | 打包后端和预编译宿主、签名、同卷暂存与排他发布 |
| `tools/macos/install_apk.py` | APK 元数据、安装、目录登记、跨目录枚举、受锁保护的卸载 |
| `tests/macos/test_installer.py` | 两个 Downloads 原始 APK 的实际安装与管理回归 |

安装通过原有签名/清单验证、输入快照和排他发布流程完成。注册表使用独立文件锁与原子 JSON 更新；不能把旧 `status.json` 的 PID 当成当前进程直接结束。卸载与安装共用目录锁，另持有宿主使用的 `instance.lock`；运行中的正常应用会令卸载失败。删除先在同卷任务目录内原子移出应用，再只清理本次暂存对象；若移出数据失败会恢复应用路径。不会删除 Runtime 或其他 App 的目录。

文件打开使用 `CFBundleDocumentTypes` 和 APK 专用 UTI，通过 `application:openFiles:` 接收；只对首选扩展名为 `apk` 的类型设置默认处理器，避免误把通用 ZIP 类型交给安装器。依据：[Apple 文件类型声明](https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundledocumenttypes)、[Launch Services](https://developer.apple.com/documentation/coreservices/launch_services)。

## 实测

用下载目录原始 Pikapika v1.8.23、Jasmine v1.7.22，分别在本任务私有目录实际安装、签名验证并卸载：

- 双 APK 元数据、原 APK 内容不变、捆绑宿主代码一致、旧应用与新目录应用同时发现：通过。
- 重复安装、非 Husk 应用卸载、symlink、另一个真实进程持锁期间卸载：拒绝且保留原内容。
- 保留数据卸载、同身份重装、仅显式请求时删除本 App 数据：通过。
- Launch Services 投递多个 APK，点击真实原生安装按钮，依次处理队列、列表刷新与搜索：通过。
- 重复打开已安装 APK 选中原应用；损坏 APK 显示错误且不创建应用：通过。
- 原生“打开应用”实际启动 Pikapika，达到 Runtime Running 状态；拒绝运行中卸载，安装器退出后 APK 进程继续存活：通过。
- 真实卸载确认按钮及数据复选框两条路径：通过。
- 正式安装器的系统默认查询指向 `~/Applications/Husk APK Installer.app`；直接执行 `open <原始APK>`，两个 APK 都正确送到正式安装器并选中原有应用；普通 ZIP 保持原处理器：通过。

界面布局通过安装器自身 AppKit view 生成 PNG 并人工检查。这是原生视图渲染，未称为锁屏桌面的 WindowServer 截图。测试控制仅在显式传入任务目录内的测试路径后启用，测试模式不能卸载任务目录之外的应用。

证据目录：`/Users/m1p0/Developer/temp/codex/Husk/apk-manager-6de9a421/`。

- `evidence/installer-acceptance-final/results.json`：完整管理回归。
- `evidence/default-routing/results.json`：正式安装器、系统默认关联及真实文件打开事件。
- 两处的 `*.png`：界面渲染；`logs/build-installer-release.log`：正式打包和关联结果。

这些验证覆盖安装与管理流程，不表示 APK 的账户认证、远端内容或所有 Android API 已兼容。现有 Runtime 的长期内存、系统沙箱与硬性资源限制边界没有因安装器完成而改变。
