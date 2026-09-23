# AGENTS.md

本仓库提供 macOS 与 Android 的 QQ 可见消息分析悬浮窗：分别从 QQ 的辅助功能（AX）或无障碍树读取当前聊天的可见文字，按 Jev `/v1/systemone` 契约逐条分析对方文字消息。自己的文字只作上下文；可识别的媒体只显示固定类型占位，不进入判断或上下文。界面不生成、复制、填入或发送回复。详见 [README](README.md)、[Android 使用说明](android/README.md) 与 [隐私说明](PRIVACY.md)。

## 当前入口

- `src/qq_ax.py`：QQ AX 窗口、消息行、发送者、引用解析及 `Message` 数据结构；`uv run python src/qq_ax.py` 只输出诊断摘要，不打印聊天正文。
- `src/conversation_memory.py`：进程内的已见消息与判断缓存。
- `src/decision_infra.py`：唯一启用的判断客户端；默认网关 `http://127.0.0.1:8080`，默认精确路由 `jev-latest`，单次请求，无静默重试或模型回退。
- `src/hud.py`：悬浮窗、前台边界与逐条分析；`./start.command` 启动，`./preview.command` 显示离线合成消息。
- `src/settings.py`、`src/settings_config.py`、`src/userconfig.py`：Decision Infra 设置与 env 加载。用户配置路径仍为 `~/.config/jev-jarvis/env`，app 与日志仍用 `jev-jarvis` 旧名。
- `android/app/src/main/java/info/jevqq/analyst/`：Android 设置页、无障碍服务、QQ 节点解析、进程内缓存和悬浮面板；从 `android/` 执行 `./gradlew :app:testDebugUnitTest :app:assembleDebug`。
- `shared/decision_questions.json`：macOS 与 Android 共用的 Jev 问题与行动策略契约；修改时检查两个客户端和打包脚本。
- `.github/workflows/ci.yml`：PR 和 main push 的 macOS、Android 离线回归；`.github/workflows/android-release.yml`：版本标签的签名 APK 发布。发布版本需与 `pyproject.toml` 和 Android `versionName` 一致。

macOS 源码只保留 QQ AX 读取、Decision Infra 判断、进程内缓存、悬浮窗和设置所需模块。

## 验证

```bash
uv run python -B -m unittest discover -s tests
uv run python src/qq_ax.py
./preview.command
cd android && ./gradlew :app:testDebugUnitTest :app:assembleDebug
```

单元测试使用合成输入。真实 QQ 方向识别、可见区域、引用和悬浮窗行为仍需分别在授权辅助功能的 macOS QQ 与开启无障碍服务的 Android QQ 中检查；预览不读 QQ，也不访问网关。修改 infra 请求时，对照 [Decision Infra API 文档](https://github.com/hufaei/decision-infra/blob/main/docs/api.md)、`tests/test_decision_infra.py` 和 Android `DecisionClientTest.kt`。

## 改动边界

- 保持读取当前 QQ 窗口已加载内容的边界：不截图、不 OCR、不读 QQ 数据库、不注入、不 hook、不自动滚动，不自动填入或发送。
- 收发方向不明确的消息不应触发判断；引用文字只能作为背景，不能冒充新消息的发送者或正文。
- 图片、表情和文件等媒体占位仅用于界面定位：不把文件名、图片描述或占位文字送给 Decision Infra，也不把它们放进后续消息的上下文。
- macOS Provider Key 只放在 Decision Infra；macOS HUD 只持有网关地址与精确模型 ID。Android 可按用户要求直连自定义 Jev URL 并使用本机加密保存的 API Key；网关模式不得发送该 Key。不要从旧 `TYPESAFE_*`、`OPENAI_*`、`ANTHROPIC_*` 路径接入 macOS HUD。
- 模型调用放在工作线程，界面和 QQ 前台状态变化后不得显示过期分析。修改这些边界时，补对应回归并手动检查真实 QQ。
- 用户可见行为变化要同步 README；配置和数据流向变化还要同步 `.env.example`、FAQ、PRIVACY。贡献与 issue 认领流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
