Windows 10/11 x64 安装版。由 Laity-nun0 基于 [原项目 EzRead](https://github.com/zhuangcao136-glitch/EzRead) 深度二次开发、独立维护，保留原项目版权声明、许可和提交历史。翻译主路径已经由 Codex CLI 文本块改为 API 驱动的本地 BabelDOC 版式 PDF 流程，当前版本不是原作者的官方发行版。

- 新版书本与 AI 星芒图标、最近阅读、单个日月图标切换主题。
- 修复筛选项右侧竖线和夜间原生标题栏颜色。
- 自带 Python、PDF 阅读与 BabelDOC 全文翻译组件，提供中文安装向导、桌面和开始菜单入口。
- 安装版数据默认保存在 `%LOCALAPPDATA%\EzRead\data`；升级和卸载保留文献、笔记与设置，不迁移或清理源码目录的原库。
- 新增“设置 → 文献与备份 → 选择目录并迁移”：保存并关闭旧库后复制文献、译文、笔记、对话、草稿和私有配置，SQLite 备份及文件校验完成后切换并重启。目标需为空且支持 Windows 权限；原库保留，不自动删除。环境变量覆盖时需先解除覆盖。
- 替换 README 的旧界面图片，使用当前界面和完全合成的演示文献，补充[二次开发说明](https://github.com/Laity-nun0/EzRead/blob/main/docs/DERIVATION.md)。

下载安装文件 `EzRead-3.5.3-Setup-x64.exe`。缺少 WebView2 的电脑首次安装需要联网；AI 功能需配置自己的 API Key，本地阅读无需密钥。安装程序未进行商业代码签名。

本版本按用户选定的当前 UI 单独发布，未整合上游另外开发的 PDF 校订和对话扩展。

`SHA256SUMS.txt` 为下载校验清单，`release-manifest.json` 为安装内容清单。安装目录提供第三方许可和 BabelDOC/PyMuPDF/MuPDF 对应源码；`EzRead-3.5.3-source.zip` 包含本版本源码与重建脚本。
