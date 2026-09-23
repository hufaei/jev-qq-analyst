## Windows 支持首发

Windows 版与 macOS / Android 同款体验：在 QQ 前台聊天页，逐条分析屏幕上可见的对方文字。

**下载（Windows 10/11，免装 Python）：** [`jev-qq-analyst-windows.exe`](https://github.com/hufaei/jev-qq-analyst/releases/download/v0.7.0/jev-qq-analyst-windows.exe) · 所有文件与 SHA-256 校验值见附件

### Windows 快速开始

1. 双击 exe，首次启动在设置页选择连接模式——**官方 Jev**（默认 `https://api.typesafe.ai/v1/systemone`，需 API Key）或 **Infra 网关**（`http://127.0.0.1:8080/v1/systemone`，本机 Decision Infra，免 Key），点“测试连接”、“保存设置”
2. 保持 QQ（Electron 内核 NTQQ）在前台并打开一个聊天；悬浮面板只在 QQ 前台时出现，停靠在 QQ 旁，可拖动、收起、暂停、重新分析
3. API Key 经 Windows DPAPI 按用户加密保存在 `%APPDATA%\jev-qq-analyst\`

### 技术要点

- 读取走 Windows UI Automation 无障碍树（与 macOS AX 同构的只读契约）：不截图、不 OCR、不读数据库、不注入；只分析当前屏幕可见消息，图片/表情/文件仅显示占位
- 面板策略对齐 macOS：前台切换即时显隐、读取失败宽限、自适应轮询、分析结果进程内缓存、UIA 会话自愈
- 已知特性：QQ 空闲较久后 NTQQ 可能收起无障碍树，程序通过常驻 UIA 监听维持激活；若启动前树已被收起，重启 QQ 再启动本程序即可
- 从源码构建参见 `packaging/windows-build.md`

### 其他

- Android `versionName` 同步升至 0.7.0（`versionCode` 9）
- macOS 行为不变
