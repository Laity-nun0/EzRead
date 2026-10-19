# 已知问题与修复

## 3.5.4 安装版 PDF 翻译在首次请求前中断（2026-10-19）

修复版本：3.5.5。源码运行时不受该安装排除规则影响。

3.5.4 安装包在独立文献库中可稳定复现：任务提示“专业 PDF 引擎未完成”，错误码 `pdf_engine`，接收单元与模型请求均为 0。底层 `native-stderr.log` 报告缺少 `babeldoc.format.pdf.new_parser.runtime.data.glyphlist`。

根因是 `installer/EzRead.iss` 使用未限定根目录的 `data\*` 排除模式，误排除了依赖内部的同名目录。安装结果相对发布清单缺少 1356 项，其中 BabelDOC 解析器缺少 glyphlist.py、fontmetrics.py、latin_enc.py 与 149 个 CMap 文件。3.5.5 将模式限定为 `\data\*`，仍排除根目录用户数据，同时保留依赖资源和相关许可。

只在独立测试进程补入与同版清单摘要一致的 152 个公共解析资源后，2 页合成 PDF 和 8 页代表性论文的只读副本均能完成模拟引擎流程；不调用真实模型。该对照用于确认缺包原因，不代表中文译文质量验收。

此前可用性接口和 `--probe` 仍能通过，因为它们没有执行完整 PDF 解析。安装验收现增加全部清单文件的大小／摘要检查，以及 `scripts/verify-installed-pdf.py` 的实际内置 Python、真实任务队列和 BabelDOC 流程，模拟供应商只允许回环连接。

遇到新的同类提示时仍应保留任务日志，不能仅凭同一个汇总错误码判断为此问题。日志位于实际文献库的 `library/<文献ID>/pdf-layout/<任务ID>/native-stderr.log`；再次运行可能覆盖当前任务日志。安装版默认数据目录为 `%LOCALAPPDATA%\EzRead\data`，迁移位置或环境变量覆盖优先。诊断材料留在本机，不把论文、私有配置或用户截图提交到源码仓库。
