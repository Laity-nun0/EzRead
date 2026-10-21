# 实际界面演示与复现

[中文介绍](../../README.md) · [English overview](../../README.en.md) · [维护与验证](../MAINTENANCE.md)

## 演示来源与操作

2026-10-21，Windows 11 x64，源码版本 3.5.6，功能基线为提交 `8c3cfe803ad3d52dd1856fbb21a9bc9777f0e7c2`。使用 Python 3.12.0、Node.js 24.14.0、Playwright 1.62.1 和本机 Edge，以 1440 × 1000 视口拍摄实际运行的网页界面；没有原生窗口边框，不是设计稿或原生安装验收。

Actual browser-rendered UI, synthetic data, no model calls. The Chinese PDF is manually authored. These screenshots demonstrate the interface, not translation quality or an installer test.

| 顺序 | 截图 | 可看到的实际操作 |
| --- | --- | --- |
| 1 | [日间文献库](library.png) | 打开包含 16 篇合成文献的隔离库，显示最近阅读、收藏、合集和卡片 |
| 2 | [夜间文献库](library-dark.png) | 点击顶部日月按钮切换夜间主题 |
| 3 | [PDF 对照阅读](reader.png) | 切回日间，点击“继续阅读”，打开已有合成原文与手写中文 PDF；显示标签栏、常驻侧栏与同步滚动入口 |
| 4 | [期刊等级](journal-tiers.png) | 返回文献库，打开“设置 → 期刊等级” |
| 5 | [翻译设置](translation-settings.png) | 切换到“翻译”设置；密钥为空，不提交任务 |

正文、封面、曲线和中文 PDF 均由演示脚本创作，没有使用真实论文或实测数据。卡片借用部分真实期刊名称来展示名单配色，不代表这些虚构论文曾在该期刊发表。文献元数据和已有译文由脚本直接写入隔离库，**没有演示导入解析或实际模型翻译**。翻译结果和研究结论不能从这些图片推断。

拍摄核对 `/api/health` 的源码目录、独立数据目录和版本，等待全部封面及两侧 PDF 画布／文字层加载，检查 JS、HTTP 和控制台错误；浏览器拦截外部请求和模型功能提交，服务端阻止模型调用并屏蔽私有 API 配置读取。本次五张均拍摄成功，错误、外部请求和模型调用均为 0，并逐张检查公开内容。

## 在自己的工作区重拍

先按[贡献指南](../../CONTRIBUTING.md)准备项目虚拟环境，并按 [PDF 运行时说明](../PDF_LAYOUT.md#独立运行时)准备 `.deps/babeldoc/runtime/`。需要 Windows、已安装的 Microsoft Edge、Node.js 与 Playwright；已有 Playwright 可直接通过 `EZREAD_PLAYWRIGHT` 指定模块目录。首次获取依赖需要联网，拍摄时只允许回环请求。

没有现成 Playwright 时，可只在忽略的 `work/` 中安装演示依赖，不改变产品依赖或全局 npm：

```powershell
npm install --prefix work/showcase-tools --no-save playwright@1.62.1
$env:EZREAD_PLAYWRIGHT = Join-Path (Get-Location) 'work/showcase-tools/node_modules/playwright'
.\.venv\Scripts\python.exe scripts/capture-showcase.py
```

脚本优先使用 `EZREAD_NODE`，否则从 PATH 找 Node；也可传 `--node <可执行文件路径>`。脚本创建随机端口和全新合成库，不需要运行真实 EzRead；不要把真实 `data/` 复制进演示目录。它会更新此目录的五张固定 PNG，模型配置保持模拟且密钥为空。

本机诊断输出位于 `output/playwright/showcase-<随机ID>/`，包含合成库和带本机路径的报告，**不用于直接公开**。先核对报告、逐张检查截图，并确认脚本与浏览器进程退出；验证成功后仅保留五张交付图片，删除本次不再需要的合成库、报告和截图副本。依赖目录如需再次拍摄可保留。
