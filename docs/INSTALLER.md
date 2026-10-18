# Windows 安装版

安装版面向 Windows 10 1809 及以上的 x64 系统，使用 .NET Framework 4.8 和 WebView2。Python、PDF 阅读依赖、BabelDOC 的隔离运行时及公开版面资源随包提供，不需要安装 Python 或 Codex。系统缺少 WebView2 时，安装向导通过微软官方引导程序联网安装；不会修改系统 PATH。

在 [GitHub Releases](https://github.com/Laity-nun0/EzRead/releases) 下载 `EzRead-<版本>-Setup-x64.exe` 并运行。默认安装到当前用户的 `%LOCALAPPDATA%\Programs\EzRead`，创建新版书本图标的桌面和开始菜单快捷方式，不要求管理员权限。

## 文献、设置与升级

正常通过 `EzRead.exe` 启动时，文献库、API 私有配置、日志和 WebView2 profile 默认保存在 `%LOCALAPPDATA%\EzRead\data`。已有 `EZREAD_DATA_DIR` 环境变量时继续使用指定目录。源码运行方式仍默认使用源码根目录 `data/`，安装不会自动复制、清空或迁移源码库。

升级前在设置中导出备份并正常关闭 EzRead，再运行新版安装程序。卸载只移除安装器登记的软件文件和快捷方式，保留上述用户数据目录；不会卸载共享 WebView2 Runtime。安装目录下未登记的运行缓存可能保留，不自动递归删除用户选定的文件夹。

若需要沿用已有源码库，可在关闭两份应用后通过 `EZREAD_DATA_DIR` 显式指定原库。不要同时启动两个进程写入同一库。API 密钥不属于文献导出备份，不能将个人数据目录上传 GitHub。

## 更改文献存储位置（3.5.3 起）

在桌面版打开“设置 → 文献与备份 → 选择目录并迁移”，选择新的空文件夹并核对确认窗口中的源、目标路径。软件先保存笔记和草稿、停止后台，待旧窗口和浏览器数据进程退出后再复制；迁移期间软件关闭，完成后自动重新打开。

迁移包含数据库、原 PDF、译文、笔记、对话、阅读状态、浏览器 profile 和 API 私有配置。数据库使用 SQLite 备份接口处理 WAL，其余文件逐个校验 SHA-256；目标使用当前用户的受限权限。目标必须支持 Windows 文件权限，不能是非空目录、链接目录或程序目录。已有两份库不能自动合并。

核验成功后才保存新位置。**原目录始终保留**，不会自动删除；确认新库内容完整后可自行归档。失败保持旧位置，可能留下目标旁的 `.ezread-migration-*` 副本以供检查。迁移保留 API 私有配置，因此不要将旧库或迁移副本公开。

已选位置独立保存在当前用户的 `%LOCALAPPDATA%\EzRead\locations\`，按程序所在目录区分，正常覆盖升级继续使用。安装到另一个程序目录不会自动继承这份选择。`EZREAD_DATA_DIR`（及旧变量兼容）优先于界面配置；已有环境变量覆盖时，需先解除该覆盖并重启，再从设置迁移。界面不会修改系统环境变量。已选择的磁盘离线或数据库缺失时会明确报错，不自动创建空库冒充原库。

## 从源码构建

构建脚本只在项目内写入 `.deps/packaging/`、`work/release/` 和 `output/releases/`，不更改全局 Python 或 PATH。先按贡献指南准备 `.venv`，安装 `requirements-release.txt`；按 `docs/PDF_LAYOUT.md` 准备固定版本 BabelDOC。然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-release.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1 -OutputDirectory work/desktop-build
.\.venv\Scripts\python.exe scripts/build-release.py --output work/release/payload
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1
```

`prepare-release.ps1` 下载固定版本官方打包工具、CPython、WebView2 引导程序及对应上游源码，并校验摘要或数字签名。构建 payload 时按发行包元数据复制必要文件，BabelDOC 模型和字体只接受已校验的公开清单；不直接复制虚拟环境、HOME 或真实数据。输出目录必须是工作区内不存在的新目录，已有目录不会被覆盖。

发布前将当前提交的 `git archive` 源码归档放入 `work/release/sources/`，再构建 payload。`release-manifest.json` 记录包版本和逐文件 SHA-256，`output/releases/` 保存安装器及最终摘要。源码发布与二进制发布分别审计：文献数据、私有配置、截图和凭据不进入提交或安装包；保留第三方源码与许可，详见根目录 `THIRD-PARTY-NOTICES.md`。

安装验收使用独立目录、临时库与 `--verification-session <新 UUID>`，检查安装、主入口启动、本地健康接口身份、PDF 资源、关闭保存、升级与卸载后数据保留。普通回归不发送真实模型请求。

数据目录功能另以 `.\.venv\Scripts\python.exe scripts/verify-migration.py --payload work/release/payload --executable work/desktop-build/EzRead.Desktop.exe` 验证真实 helper、独立 WebView2、并发重开、浏览器草稿、自动切换后的健康接口与正常退出。该入口创建独立应用副本和合成库，证据保存在 `work/migration-verification/`，不迁移个人库。
