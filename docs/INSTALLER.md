# Windows 安装版

安装版面向 Windows 10 1809 及以上的 x64 系统，使用 .NET Framework 4.8 和 WebView2。Python、PDF 阅读依赖、BabelDOC 的隔离运行时及公开版面资源随包提供，不需要安装 Python 或 Codex。系统缺少 WebView2 时，安装向导通过微软官方引导程序联网安装；不会修改系统 PATH。

在 [GitHub Releases](https://github.com/Laity-nun0/EzRead/releases) 下载 `EzRead-<版本>-Setup-x64.exe` 并运行。默认安装到当前用户的 `%LOCALAPPDATA%\Programs\EzRead`，创建新版书本图标的桌面和开始菜单快捷方式，不要求管理员权限。

## 文献、设置与升级

正常通过 `EzRead.exe` 启动时，文献库、API 私有配置、日志和 WebView2 profile 默认保存在 `%LOCALAPPDATA%\EzRead\data`。已有 `EZREAD_DATA_DIR` 环境变量时继续使用指定目录。源码运行方式仍默认使用源码根目录 `data/`，安装不会自动复制、清空或迁移源码库。

升级前在设置中导出备份并正常关闭 EzRead，再运行新版安装程序。卸载只移除安装器登记的软件文件和快捷方式，保留上述用户数据目录；不会卸载共享 WebView2 Runtime。安装目录下未登记的运行缓存可能保留，不自动递归删除用户选定的文件夹。

若需要沿用已有源码库，可在关闭两份应用后通过 `EZREAD_DATA_DIR` 显式指定原库。不要同时启动两个进程写入同一库。API 密钥不属于文献导出备份，不能将个人数据目录上传 GitHub。

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
