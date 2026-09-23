# Android QQ 可见消息分析

Android 端使用无障碍服务读取**前台 QQ 当前聊天中屏幕上可见**的节点。它逐条分析可确定为对方发送的文字；自己的文字只作为上下文；可识别的媒体只显示类型占位。结果显示在 QQ 之上的小面板中，切出 QQ 后隐藏。它不截图、滚动、复制、填入或发送消息。

## 下载并安装

从 [GitHub Release 下载签名 APK](https://github.com/hufaei/jev-qq-analyst/releases/latest/download/jev-qq-analyst-android.apk)，在手机上打开文件安装。APK 的最低系统版本是 Android 8.0（API 26）。**实测组合**：Xiaomi 17 Pro（型号 `25098PN5AC`）、Android 16（API 36）、HyperOS `OS3.0.319.0.WBLCNXM`、QQ 9.3.55；普通与长文本对方消息均已完成识别和 Jev 判断。其他设备及 QQ 版本仍需验证。

正常使用无需 Mac 或 USB。若以前安装过自行构建的 debug APK，由于签名不同，需先卸载 debug 版，再安装 Release APK；卸载会清除原设置，安装后需要重新填写 URL 和 API Key、重新开启无障碍服务。以后由同一发布密钥签名的 Release APK 可以直接升级。

## 在 Mac 上从源码构建

需要 JDK 17、Android SDK（API 35）和已启用 USB 调试的 Android 手机。首次构建需要下载 Gradle 和 Android 依赖。在仓库的 `android/` 目录运行：

```bash
./gradlew :app:assembleDebug
adb devices -l
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

`adb devices -l` 应列出状态为 `device` 的手机。若显示 `unauthorized`，在手机上允许这台 Mac 的 USB 调试授权。模拟器能验证安装和设置页，但真实 QQ 的节点结构、发送者、引用和媒体识别需要在手机上检查。

## 配置判断服务

打开手机上的 **Jev · QQ 可见消息分析**：

1. 选择 **Jev API**，填写你使用的完整 HTTPS `/v1/systemone` URL 和对应 API Key；默认 URL 是 `https://api.typesafe.ai/v1/systemone`。模型路由默认 `jev-latest`。保存后可用“测试连接”发送一条合成问候，确认 URL、Key 和协议可用。测试不会读取 QQ。
2. 如果使用本机 [Decision Infra](https://github.com/hufaei/decision-infra)，选择 **Decision Infra 网关**。先在 Mac 上启动网关，再运行：

   ```bash
   adb reverse tcp:8080 tcp:8080
   ```

   网关 URL 保持 `http://127.0.0.1:8080/v1/systemone`。这里的 `127.0.0.1` 是**手机**；`adb reverse` 将手机的 8080 端口接到 Mac。网关模式不把 API Key 发给网关，Provider Key 应配置在 Decision Infra。断开或重连 USB 后可能需要重做 `adb reverse`。
3. 点“打开辅助功能设置”，找到 **Jev · QQ 可见消息分析** 并开启。回到 QQ 聊天页。服务只在 QQ 位于前台时读取当前可见节点。

外部 HTTP URL 会被拒绝；远程服务须用 HTTPS。Key 在本机由 Android Keystore 加密，设置页不会回显 Key。清除 Key 后保存即可删除；切换网关模式不会发送已保存的 Jev Key。具体数据流向见 [隐私说明](../PRIVACY.md)。

## 手机上检查什么

在自己的 QQ 账号中打开一个聊天，放入两条对方文字、一条自己的文字，以及可识别的图片或文件。预期对方文字各有分析卡，自己的文字只显示“我 · 上下文”，媒体显示固定占位且不触发模型。分析卡依次显示原话、意图与情绪、回复风险、是否值得回复、意图候选、行为与需要、信号和带分数的下一步。按住面板标题可拖动位置，拖动底部“调整大小”可改变宽高；位置和大小会保存在手机上。离开 QQ，面板应隐藏；返回聊天后可再次出现。用 QQ 的引用功能检查引用是否与新写正文分开。

如果没有出现面板，返回 Jev · QQ 设置页，查看底部“最近 QQ 识别状态”。这个状态只记录识别阶段和条数，不保存聊天标题或正文；它可区分尚未进入聊天页、会话标题未识别和当前没有可分析文字。

当前解析规则参考 [QQAdapter](https://github.com/jev-chat/jev-chat-jarvis/blob/main/app/src/main/java/com/jev/probe/capture/ChatAppAdapter.kt#L170-L231) 的 QQ 资源 ID，并在上述实测组合中支持左侧“资料卡”头像定位长文本的发送者。QQ 版本、设备和消息类型会改变无障碍树；无法确定方向的消息不会被分析。若出现漏读或误判，请记录手机型号、Android 与 QQ 版本，并提供**去掉私人内容**的页面结构或复现说明。

## 本地测试

```bash
./gradlew :app:testDebugUnitTest :app:assembleDebug
```

JVM 测试使用合成的 QQ 节点和本地 HTTP 测试服务；它们不能证明真机 QQ 的节点布局与无障碍权限行为。
