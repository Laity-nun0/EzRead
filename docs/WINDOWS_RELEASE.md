Windows 10/11 x64 安装版，对应本仓库当前选定的 UI 版本。基于 [原项目 EzRead](https://github.com/zhuangcao136-glitch/EzRead)，保留其源码、许可和提交历史。

- 新版书本与 AI 星芒图标、最近阅读、单个日月图标切换主题。
- 修复筛选项右侧竖线和夜间原生标题栏颜色。
- 自带 Python、PDF 阅读与 BabelDOC 全文翻译组件，提供中文安装向导、桌面和开始菜单入口。
- 安装版数据默认保存在 `%LOCALAPPDATA%\EzRead\data`；升级和卸载保留文献、笔记与设置，不迁移或清理源码目录的原库。

下载安装文件 `EzRead-3.5.2-Setup-x64.exe`。缺少 WebView2 的电脑首次安装需要联网；AI 功能需配置自己的 API Key，本地阅读无需密钥。安装程序未进行商业代码签名。

本版本按用户选定的当前 UI 单独发布，未整合上游另外开发的 PDF 校订和对话扩展。

`SHA256SUMS.txt` 为下载校验清单，`release-manifest.json` 为安装内容清单。安装目录提供第三方许可和 BabelDOC/PyMuPDF/MuPDF 对应源码；`EzRead-3.5.2-source.zip` 包含本版本源码与重建脚本。
