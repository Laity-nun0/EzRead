# EzRead 安装版：第三方组件与源码

EzRead 自有源码和文档沿用根目录 MIT License。该许可不替代安装包内第三方组件的许可证。安装版完整保留 Python、PDF.js、ECDICT、WebView2 SDK 和各 Python 发行包的版权与许可文件。

## PDF 翻译组件

安装版包含 BabelDOC 0.6.4、PyMuPDF 1.26.7 和其内含的 MuPDF 1.26.12，使用其 AGPL-3.0 开源许可。涉及这些组件的分发须遵循 AGPL-3.0；EzRead 的 MIT 授权不免除这些条件。安装版中的 `.py`、JavaScript 和 CSS 保留可修改源码；`licenses/sources/` 同时提供相应上游完整源码、EzRead 构建源码归档、下载来源及 SHA-256 清单。各发行包的许可和第三方通知保存在 `licenses/` 及各自 `.dist-info/` 目录。

- [BabelDOC 0.6.4 官方发行](https://pypi.org/project/BabelDOC/0.6.4/)
- [PyMuPDF 1.26.7 官方发行](https://pypi.org/project/PyMuPDF/1.26.7/)
- [MuPDF 1.26.12 完整源码](https://mupdf.com/downloads/archive/mupdf-1.26.12-source.tar.gz)
- [GNU AGPL-3.0 许可原文](https://www.gnu.org/licenses/agpl-3.0.html)
- [EzRead 源码与构建脚本](https://github.com/Laity-nun0/EzRead)

BabelDOC 的版面模型、字体与 CMap 按上游公开资源清单分发，逐文件版本和校验值见 `.deps/babeldoc/runtime-manifest.json`。资源包内原有许可与通知一并保留。

## 其他组件

- CPython 3.12.10 x64 嵌入发行版：PSF License 及其中第三方许可，见 `licenses/PYTHON-LICENSE.txt`。
- PDF.js 6.4.299：Apache-2.0 等原有许可与通知，见 `static/vendor/pdfjs/`。
- ECDICT：公开词典及其上游授权见 `assets/dictionaries/LICENSE-ECDICT.txt`，它不包含用户文献库。
- WebView2 SDK：微软原有许可见 `desktop/bin/WebView2-LICENSE.txt`。缺少 Evergreen Runtime 时，安装向导使用具有微软签名的官方引导程序联网安装运行时。
- 其他 Python 发行包：原有许可分别保留在 `licenses/main/`、`licenses/babeldoc/` 及运行时包目录，不统一改成 MIT。

安装包不包含开发者的论文、笔记、API 密钥、浏览器配置或个人备份。AI 翻译与问答仍需用户在界面配置自己的供应商 API；本地阅读无需密钥。
