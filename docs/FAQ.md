# 常见问题

项目有 macOS、Windows 和 Android 三个入口。macOS 启动步骤见 [README](../README.md#macos-快速开始)，Android 可直接从 [Release 下载 APK](https://github.com/hufaei/jev-qq-analyst/releases/latest/download/jev-qq-analyst-android.apk)，设置与 USB 调试见 [Android 使用说明](../android/README.md)，数据流向见 [隐私说明](../PRIVACY.md)。以下网关、配置文件和日志条目适用于 macOS 端。

## Android 手机怎样连接 Mac 测试？

Android 直接连接用户填写的 Jev HTTPS 接口时，不需要 Mac 或 USB。若要用 Mac 构建、调试或连接 Mac 上的 Decision Infra，打开手机 USB 调试并允许这台 Mac，运行 `adb devices -l` 确认设备状态为 `device`。连接本机网关还需运行 `adb reverse tcp:8080 tcp:8080`；只填 `127.0.0.1` 而不反向映射会连接到手机本机。

## Android QQ 聊天页没有悬浮面板怎么办？

1. 进入具体的 QQ 聊天页，确认 **Jev · QQ 可见消息分析** 的系统辅助功能仍已开启。打开 Jev 设置页，查看底部“最近 QQ 识别状态”。若提示没有可分析文字，先找一条当前屏幕可见的对方文字消息。
2. 如果状态停在“已收到 QQ 页面事件，正在读取活动窗口”，或进入聊天页后长时间不更新，检查 Jev 的后台运行限制。在手机上长按 Jev 图标 → **应用信息** → **省电策略／电池用量**，选择 **无限制**；若有“后台自启动”开关，也允许 Jev 自启动。不同品牌的入口名称可能不同。
3. 改完省电设置后，**重新打开 Jev，确认辅助功能仍开启**，再返回 QQ。系统可能在修改应用策略时结束 Jev 进程并关闭辅助功能；本次 Xiaomi 17 Pro、Android 16、HyperOS 3 实测就是这样恢复的。无需重启手机，也不要从最近任务中强行清除 Jev。

后台省电策略是手机系统设置，不依赖特定 QQ 版本。本次日志确认 QQ 已在前台，但 Jev 进程被系统冻结；本地调试包中增加的持续通知也未单独解除该手机上的冻结。[小米的电池策略说明](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1575)说明“无限制”允许应用后台工作，[自启动说明](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1624)说明该权限由用户管理。其他手机若仍无面板，请记录手机型号、Android 版本、QQ 版本和 Jev 页面上的最新识别状态。

## 为什么签名 APK 安装时仍提示风险？

GitHub Release 的 APK 已使用固定的发布密钥签名，可验证文件未被改动，并确保以后由同一密钥签名的版本能升级。Android 对来自应用商店外的安装来源仍会要求用户授权，Google Play Protect 也会检查外部来源的应用；签名不会自动消除这类提示。[Android 的侧载说明](https://developer.android.com/distribute/marketing-tools/alternative-distribution)和 [Google Play Protect 说明](https://support.google.com/googleplay/answer/2812853?hl=zh-Hans)分别解释了这两层检查。下载时请使用本仓库的 Release 链接，并对照随版本提供的 `SHA256SUMS`。

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

macOS 端通过 Decision Infra 做结构化判断；Android 端可直连用户填写的 Jev 兼容接口，也可用 Decision Infra。两端均不下载本地判断模型，也没有生成、复制、填入或发送回复功能。

## 还有问题？

可在 [仓库 Issues](https://github.com/hufaei/jev-qq-analyst/issues) 中附上 QQ 版本、复现步骤、去掉个人信息的诊断和日志摘要。
