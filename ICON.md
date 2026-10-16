# EzRead 图标

采用已确认的蓝色展开书本与白色四角星芒设计。两片书页表达阅读，星芒表达 AI 辅助；平面轮廓配透明背景，与日间、夜间界面共用同一图标。

生成工具：内置 image_gen.imagegen，2026-10-16。根据用户选定的书页与 AI 星芒稿提取独立透明图标；旧图标保留在 Git 历史中。

文件：

- 源图：`assets/branding/ezread-book.png`。
- 应用 PNG：`static/ezread-icon.png`。
- Windows 多尺寸 ICO：`static/ezread.ico`。16、24、32、48、64、128、256 像素版本均由应用使用的高清原图 `static/ezread-icon.png` 直接缩放编码，保留透明背景。运行 `python assets/branding/build_windows_icon.py` 可重建；运行 `powershell -File scripts/build-desktop.ps1 -OutputDirectory work/desktop-build` 可生成独立 EXE 验证图标，不覆盖正在使用的桌面组件。
- 标题栏辅助 ICO：`static/window-icon-transparent.ico`，仅供原生小图标隐藏；网页 favicon 与任务栏仍使用真实书本图标。

`setup-desktop.ps1` 为桌面快捷方式在 `work/shortcut-icons/` 保存按图标内容摘要命名的同内容 ICO，避免 Windows 沿用旧图标缓存；该目录是本机生成文件，不提交。

提示词：

Extract and faithfully preserve only the approved blue open-book with four-point white AI sparkle symbol. Remove all text. Transparent background outside the book; flat cobalt blue pages, rounded top corners, gently concave bottom edges, narrow transparent center seam, opaque white sparkle inside the right page. No added outlines, tile, shadows, gradients or texture. Centered square canvas with padding. Preserve the approved book silhouette.
