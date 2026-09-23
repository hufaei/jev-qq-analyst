# 常见问题

当前版本分析 macOS QQ 窗口中可见的对方消息。完整启动步骤见 [README](../README.md#快速开始)，数据流向见 [隐私说明](../PRIVACY.md)。

## 配置文件和日志在哪里？

用户配置仍位于 `~/.config/jev-jarvis/env`，日志仍位于 `~/Library/Logs/jev-jarvis.log`。这两个旧路径会继续被当前启动器使用；仓库更名不会自动迁移它们。开发时还可使用项目根目录的 `.env`，格式见 [示例](../.env.example)。配置保存后重启应用。

正常诊断日志不主动记录聊天标题、昵称或消息正文。反馈问题前仍应检查错误信息，避免附带个人信息；不要公开自己的配置文件。

## 网关在线，但没有分析结果？

先确认 QQ 位于前台、当前聊天有可见的对方文字消息，并已给终端或 `jev-jarvis.app` 授予 macOS“辅助功能”权限。`uv run python src/qq_ax.py` 可只检查读取链路；它不调用模型，也不打印消息正文。

再检查 Decision Infra 的 `GET /healthz`。健康检查只证明网关在线，不证明 `jev-latest` 已注册或 Provider 可用。QQ 应用默认请求 `http://127.0.0.1:8080/v1/systemone`，显式指定 `jev-latest`；网关未注册该模型时会返回 404，Provider 不可用时可返回 503。`TYPESAFE_API_KEY` 应配置在 infra 进程，而不是 QQ 应用。具体路由契约见 [Decision Infra API 文档](https://github.com/hufaei/decision-infra/blob/main/docs/api.md)。

## 为什么某条消息没有分析？

只有当前窗口已加载、可见、能识别为对方发送的文字才是分析目标。自己的消息只作上下文；没有可读正文的图片、语音，以及方向不明确的消息不会分析。QQ 改版也可能改变 AX 层级。先用上述诊断命令检查数量和收发方向，再记录 QQ 版本与复现步骤。

## 为什么引用内容没有单独显示？

只有 QQ 的 AX 树明确标注引用容器时，应用才能将引用与新写正文分开。若没有标注，引用可能混入正文，此时不应把结果当作正确识别了引用。详见 [已知限制](../README.md#已知限制)。

## 应用会下载本地模型或生成回复吗？

当前应用只通过 Decision Infra 做结构化判断，不下载本地判断模型，也没有生成、复制、填入或发送回复功能。

## 还有问题？

可在 [仓库 Issues](https://github.com/hufaei/jev-qq-analyst/issues) 中附上 QQ 版本、复现步骤、去掉个人信息的诊断和日志摘要。
