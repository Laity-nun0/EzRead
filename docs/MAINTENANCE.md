# 维护、验证与真实反馈记录

[中文介绍](../README.md) · [English overview](../README.en.md) · [提交反馈](https://github.com/Laity-nun0/EzRead/issues/new/choose)

本记录供试用者和外部审阅者核查：解决过什么问题、证据在哪里、哪些仍未验证。核对日期为 **2026-10-21（UTC+8）**；没有将维护者自测、模拟结果或下载次数记为独立用户体验。

## 已发布基线

准备工作开始时，公开默认分支 `main` 与本地 HEAD 均为 `8c3cfe803ad3d52dd1856fbb21a9bc9777f0e7c2`；源码版本为 3.5.6。GitHub [Latest Release](https://github.com/Laity-nun0/EzRead/releases/latest) 在核对时指向 [windows-v3.5.6](https://github.com/Laity-nun0/EzRead/releases/tag/windows-v3.5.6)，发布于 2026-10-20，非草稿、非预发布。五个附件均显示已上传，含安装器、源码 ZIP、文件清单、SHA-256 清单和第三方声明。

这些是本次读取公开 API 的结果，不代表本次重新下载、安装或核对了所有附件字节。功能与限制沿用该版本；本轮文档、模板和截图通过 `main` 提供，3.5.6 Release 及其附件保持原状，不发布新安装包。

## 可核查的维护工作

| 问题与用户可见结果 | 修复与发布 | 可检查的验证入口 |
| --- | --- | --- |
| 3.5.4 安装版缺少 BabelDOC 内部 `data` 资源，首次模型请求前翻译失败；3.5.5 保留依赖资源并限制根目录数据排除范围 | [5149d70](https://github.com/Laity-nun0/EzRead/commit/5149d70f0d0d0711aa6efce25d8389213522e450)，[3.5.5 Release](https://github.com/Laity-nun0/EzRead/releases/tag/windows-v3.5.5)；[问题与根因](KNOWN_ISSUES.md) | [发布边界与清单回归](../tests/test_release.py)、[安装后真实引擎模拟流程](../scripts/verify-installed-pdf.py)；导入探测通过不能代替完整流程 |
| 多篇论文标签、关闭重开后的会话恢复，以及翻译未完成时的退出提醒 | [5149d70](https://github.com/Laity-nun0/EzRead/commit/5149d70f0d0d0711aa6efce25d8389213522e450)，[3.5.5 Release](https://github.com/Laity-nun0/EzRead/releases/tag/windows-v3.5.5) | [阅读会话验证](../scripts/verify-reader-session.py)、[关闭与后台归属验证](../scripts/verify-exit.py)；原生检查需独立文献库及 verification session |
| PDF 重绘先清空画面造成闪动；改为画布完成后一次显示，并保留选择与复制 | [8c3cfe8](https://github.com/Laity-nun0/EzRead/commit/8c3cfe803ad3d52dd1856fbb21a9bc9777f0e7c2)，[3.5.6 Release](https://github.com/Laity-nun0/EzRead/releases/tag/windows-v3.5.6) | [PDF 回归](../tests/test_reader_pdf.js)、[实际浏览器阅读验证](../scripts/verify-reader-text.py)包含延迟绘制期间保留画面及文字层检查 |
| 删除合集时保留其中论文与阅读数据；成功翻译后清理可丢弃的排版中间文件，失败与暂停材料继续保留 | [8c3cfe8](https://github.com/Laity-nun0/EzRead/commit/8c3cfe803ad3d52dd1856fbb21a9bc9777f0e7c2)，[3.5.6 Release](https://github.com/Laity-nun0/EzRead/releases/tag/windows-v3.5.6) | [合集回归](../tests/test_collections.py)、[PDF 任务回归](../tests/test_pdf_translation_tasks.py)，覆盖数据保留、哈希与路径边界 |

这些工作在本轮之前已实现并发布，本轮没有重复修改功能。历史修复没有公开 Issue 时，以上使用实际提交与 Release 关联，不补造用户报告。

公开 CI 可复核：[3.5.6 的 Windows/Python 3.10 与 3.12 离线检查及桌面构建](https://github.com/Laity-nun0/EzRead/actions/runs/39354168314)均成功；[3.5.6 安装包流水线](https://github.com/Laity-nun0/EzRead/actions/runs/39354168137)的构建、安装、启动、升级、卸载步骤均成功。入口为 [checks.yml](../.github/workflows/checks.yml) 与 [windows-installer.yml](../.github/workflows/windows-installer.yml)。这是对应提交当时的 CI 结果，不能代替新改动或真实账号的验收。

## 2026-10-21 本轮准备工作与检查

- 保留中文 README，增加[英文入口](../README.en.md)、离线首次试用步骤、API 条件及数据发送提示。
- 更新五张[实际界面截图](images/README.md)，使用独立合成库与手写模拟译文，显示 3.5.6 的标签栏和常驻侧栏。修正拍摄脚本中的本机 Node／Playwright 路径依赖；未修改产品功能。
- 新增[错误报告](../.github/ISSUE_TEMPLATE/bug_report.md)与[试用反馈](../.github/ISSUE_TEMPLATE/trial_feedback.md)模板，在[贡献指南](../CONTRIBUTING.md#试用反馈与维护追踪)中约定 Issue → 修复／PR → 回归 → Release → 复测的记录方式。

| 检查 | 本轮结果与边界 |
| --- | --- |
| `python scripts/capture-showcase.py` | 通过，五张截图；页面／控制台／HTTP 错误和外部请求均为 0，私有配置读取和真实模型调用为 0。浏览器结果，不是原生安装验收 |
| `python scripts/verify-reader-text.py` | 通过，6 项浏览器检查；覆盖宽窄视口、后台完成后双 PDF、重绘期间保留画面与文字层、复制、缩放、笔记和位置恢复。真实模型调用、真实库写入及页面错误均为 0 |
| `python scripts/check.py` | 退出码 0。Python 共运行 711 项测试，4 项按环境条件跳过，未视为已验收；其余通过。Ruff、源码内容审查、前端语法及全部 Node 回归通过；普通验证未执行真实模型推理 |
| 文档链接、模板及发布内容检查 | 7 份 Markdown 的 71 个本地链接／锚点和两份模板元数据通过；缺少 Node 时在建库前明确失败。Ruff、483 个候选路径的源码内容审查及 `git diff --check` 通过；五张截图逐张检查。没有将此检查称为 Git 历史审计或 GitHub 模板上线验收 |
| 推送前隐私与历史检查 | 远端基线为 `8c3cfe8`，开始时没有额外待推送提交；本次仅纳入 13 个文档、模板、合成截图及拍摄脚本文件。检查文本、逐张截图及 PNG 元数据，未发现私人文献、对话、账号凭据或个人路径；五张 PNG 均无附加元数据。提交使用公开 GitHub noreply 身份，不发布本机配置、库或日志 |

本轮没有以真实密钥调用供应商，也没有重新执行安装、升级、卸载或跨显示器原生验收。截图中的文献直接由 fixture 建立，不构成新用户导入成功的证据。复杂扫描件、公式、图表、特殊字体和旋转页的实际译文仍须人工对照原 PDF；模拟响应不能证明语义准确或所有可见对象完整。

## 公开数据快照与口径

数据来自 2026-10-21 读取的 [GitHub 仓库 API](https://api.github.com/repos/Laity-nun0/EzRead)、[全部 Release API](https://api.github.com/repos/Laity-nun0/EzRead/releases?per_page=100)及[全部 Issue／PR 列表 API](https://api.github.com/repos/Laity-nun0/EzRead/issues?state=all&per_page=100)。返回 5 个公开 Release、0 条 Issue／PR，均未达每页 100 条上限；以下只是当日快照。

| 指标 | 数值及统计范围 |
| --- | --- |
| Release 安装器下载计数 | 合计 **7**：3.5.2 为 0、3.5.3 为 0、3.5.4 为 4、3.5.5 为 1、3.5.6 为 2；仅累计 `*-Setup-x64.exe` 的 `download_count` |
| Release 源码 ZIP 下载计数 | 合计 **1**，来自 3.5.6 的显式源码附件；不含 GitHub 自动生成的 ZIP／tarball、clone 或其他附件 |
| Star | **0**，仓库 `stargazers_count`；与安装／使用无直接等价关系 |
| 外部 Issue | **0**，已公开 Issue 中非维护者账号提交的条目，含已关闭条目、排除 PR；没有把私下交流补记为公开 Issue |
| 外部 PR | **0**，公开列表中的非维护者 PR，含已关闭条目；没有把本地提交算作社区贡献 |
| 独立用户数、活跃用户数 | **未知**；未部署遥测或账号统计，下载中可能含维护者验证及重复下载 |
| 结构化外部试用反馈 | 尚无公开记录；模板提供后续收集方式，未虚构评价 |

不能从上述小样本下载量推出真实用户数或项目影响力。后续只在实际重新读取来源时更新日期和数值；若接口超出一页须分页，不用 `open_issues_count` 代替全部 Issue 或 PR 数。
