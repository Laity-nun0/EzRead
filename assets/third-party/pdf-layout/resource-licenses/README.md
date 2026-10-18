# BabelDOC 公共资源的许可与来源

这些原文覆盖随 EzRead 安装包提供的 BabelDOC 固定公共资源，下载来源与 SHA-256 记录在 `sources.json`。实际资源逐文件清单位于 `.deps/babeldoc/runtime-manifest.json`，包含 34 个字体、146 个 CMap、1 个 DocLayout ONNX 模型和 1 个 tiktoken 缓存。

- GoNotoKurrent：`GoNoto-UNLICENSE.txt` 同时保留构建脚本的 Unlicense 与所用 Noto 字形的 OFL 版权和许可。
- Source Han Sans/Serif：`SourceHan-OFL.txt`。
- LXGW WenKai GB/TC 与 Klee One：`LXGW-OFL.txt`、`Klee-OFL.txt`；字体文件内的原有版权及保留名称一并保留。
- Noto Sans/Serif：`Noto-OFL.txt`。
- MaruBuri：`MaruBuri-README.md` 保留原字体项目的 Naver 字体许可全文。
- DocLayout-YOLO-DocStructBench ONNX：模型来源和 Apache-2.0 声明见 `DocLayout-model-card.md`，完整原文见 `Apache-2.0.txt`。
- OpenAI o200k_base tokenizer：`tiktoken-MIT.txt`，缓存内容来自 `https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken`。
- CMap：BabelDOC 的 Adobe CMap 适配使用压缩 JSON 映射；保留上游 Adobe BSD 声明 `Adobe-CMap-BSD.txt`。字体与其他资源的上游映射说明另见 `BabelDOC-Assets-README.md`。这里不另行授予这些第三方资源 MIT 许可。

下载时保留上游许可原文，不执行其中的命令或链接内容。EzRead 未修改这些字体、模型及映射数据。
