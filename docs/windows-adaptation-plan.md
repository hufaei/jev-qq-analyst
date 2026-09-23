# Windows QQ 适配方案（基于 v0.6.2）

> 状态：**已实现**（feat/windows 分支）。核心模块：`src/qq_ax_win.py`（UIA 适配器）、
> `src/jev_client.py`（双模式直连客户端：官方 Jev / Infra 网关，DPAPI 加密 Key）、
> `src/hud_win.py`（tkinter 置顶面板 + 设置界面）、`start.bat` 与
> `packaging/windows-build.md`（PyInstaller 单文件 exe）。本文其余部分为实施前的
> 探测结论，保留作依据。
>
> 实施中发现的关键补充：NTQQ 的 Chromium 会**收起**（deactivate）无障碍树——在无
> UIA 客户端"在监听"或 QQ 闲置一段时间后，树缩成几个匿名节点，此时外部无法用遍历、
> WM_GETOBJECT、MSAA、事件订阅或模拟点击唤醒。已验证的对策是**常驻事件监听**
> （`qq_ax_win._ensure_accessibility` 注册后保持进程生命周期存活的 handler），使本
> 程序持续被计为监听客户端。若 QQ 在本程序启动前就已收起树，重启 QQ 后再启动本程
> 序即可恢复。

## 一、探测结论：可行

Windows 版 QQ（NTQQ）与 macOS 版同为 Chromium/Electron 内核，原生暴露 **UI Automation（UIA）**
无障碍树，只读遍历即可拿到本方案所需的全部信息，无需截图、OCR、读数据库、注入或 hook，
与项目现有隐私立场（PRIVACY.md）完全一致。Windows 上 UIA 访问同完整性级别的进程**不需要**
macOS 式的"辅助功能"授权，也不需要管理员权限。

### 实测拿到的关键节点

| 需求信息 | Windows UIA 中的位置 | 实测结果 |
| --- | --- | --- |
| QQ 主窗口 | 顶层窗口 `ClassName='Chrome_WidgetWin_1'`，进程 `QQ.exe` | ✅ |
| 消息列表 | `WindowControl Name='消息列表'`，根容器 `AutomationId='ml-root'` | ✅ 与 macOS 的 `MESSAGE_LIST_DESCRIPTION` 同名 |
| 单条消息 | 消息列表下 `GroupControl`，`AutomationId` = QQ 消息 id | ✅ 每条独立节点，含完整内容 |
| 发送人昵称 | 消息行内头像 `GroupControl` 的 `Name` | ✅ 群昵称/好友昵称 |
| 消息文本 | 行内 `TextControl.Name` | ✅ |
| 消息方向（我/对方） | 头像与气泡 x 坐标：左=对方，右=自己 | ✅ 与 macOS `_avatar_side`/`_bubble_side` 同思路 |
| 时间分隔 | 行内 `TextControl.Name`（如 `HH:MM`，横向居中） | ✅ |
| 图片等媒体 | `ImageControl Name='图片'` 等类型占位 | ✅ 可映射为 macOS 端同款占位符 |
| 会话标题 | 聊天区头部、消息列表正上方的标题控件（x 与列表左缘对齐） | ✅ 单聊为对方昵称，群聊应为群名（待采样） |
| QQ 是否前台 | `GetForegroundWindow()` → pid 是否属于 QQ.exe | ✅ 对应 macOS `frontmost_app_is_qq` |

### 一个重要差异（对我们有利）

Chromium 对滚出视口的消息做虚拟化，但这些节点在 UIA 树中仍存在、文本可读，只是矩形为
`0x0`。**过滤掉 0x0 矩形的节点即可天然实现"只分析当前屏幕上可见的消息"**，与 macOS 端的
可见性约束一一对应。

另注意：Chromium 渲染进程的无障碍树是**懒激活**的——首个 UIA 客户端开始遍历后才完整展开
（实测首次遍历即触发，约亚秒级）。适配器应在读取前做一次预热遍历并短暂等待。

## 二、总体架构

沿用"适配器 + 共享分析/展示层"的现有结构，新增 Windows 适配器，对接约定与
`src/qq_ax.py` 完全一致（返回同样的 `Message` 列表与 JSON 契约）：

```
src/
  qq_ax.py          # macOS AXUIElement 适配器（现有）
  qq_ax_win.py      # 新增：Windows UIA 适配器，输出同构 Message
  hud_win.py        # 新增：Windows 悬浮面板（对应 hud.py 的 NSPanel 实现）
  ...               # settings / decision_infra / conversation_memory 复用
probe/
  win_uia_probe.py  # 新增：窗口枚举 + 全树遍历探测（已验证）
  win_msg_dump.py   # 新增：消息列表/头部聚焦转储（已验证）
start.bat           # 对应 start.command
```

- 按平台在启动入口选择适配器（`sys.platform == 'win32'`）。
- 依赖：`uiautomation`（纯 Python + ctypes，免编译，MIT）、`psutil`（进程识别，可选）。
  Python 3.12+ 与现有工程一致。

### qq_ax_win.py 读取流程（对应 macOS 端逐条实现）

1. **前台判定**：`GetForegroundWindow` 的 pid 属于 `QQ.exe` 才继续（对应 `frontmost_app_is_qq`）。
2. **定位窗口**：枚举顶层窗口，`ClassName='Chrome_WidgetWin_1'` 且 pid 属于 QQ；
   支持多窗口（主面板 + 独立聊天窗口，见待验证清单）。
3. **预热**：浅遍历一次触发 Chromium 展开无障碍树，等待其稳定。
4. **定位消息列表**：优先 `Name='消息列表'` 的 `WindowControl`，以 `AutomationId='ml-root'` 加固。
5. **解析消息行**（对应 macOS 的 `_parse_row` 族）：
   - 行 id：`AutomationId`（消息 id，可用于增量去重：只分析新出现的行）；
   - 方向：头像 `GroupControl.Name` 的 x 坐标相对列表中线判定，左=them、右=me；
     文本节点坐标兜底（对应 `_bubble_side`）；
   - 文本：行内可见 `TextControl`；时间戳（居中、`HH:MM` 形态）识别后单独归类；
   - 媒体：`ImageControl`（`图片` 等）映射为 `kind=image/sticker/file` 占位；
   - 引用/回复：行内嵌套的"昵称 + 小图标 + 预览"块映射为 `quoted_text`（结构已见到，需采样细化）；
   - 可见性：丢弃矩形为 0x0 或超出消息列表视口的节点。
6. **会话标题**：消息列表上方 header 区域、与列表左缘对齐的标题控件；作为分析上下文传入。
7. **输出**：与 macOS 相同的 `Message{ text, side, y, conf, sender, quoted_text, kind, ... }`。

### hud_win.py 悬浮面板（唯一需要新写的 UI）

macOS 的 `hud.py` 基于 NSPanel；Windows 需要等价物：**置顶 + 半透明 + 可拖动/缩放/折叠 +
QQ 不在前台时隐藏**。推荐用 `pywebview`（无边框透明置顶窗，可直接复用现有分析卡 HTML/CSS
风格），备选 tkinter `transparentcolor` + `topmost`（零新增重依赖，视觉略糙）。鼠标点击穿透
区域（纯展示部分）用 `WS_EX_TRANSPARENT | WS_EX_LAYERED` 实现。

### 打包与分发（后续可选）

- 开发期：`start.bat` + 全局/venv Python，与 macOS 的 `start.command` 对等。
- 发布期：PyInstaller 单目录打包（与 Android 端"开箱即用"的定位对齐）。

## 三、待验证清单（需要真机配合采样）

1. **群聊**：群昵称标签、@消息、成员多行气泡的结构（当前仅实测了单聊）。
2. **引用/回复**：嵌套引用块的完整结构 → `quoted_text` 映射规则。
3. **独立聊天窗口**：从主面板拖出的独立窗口，确认窗口标题与内部结构是否同构。
4. **表情/贴纸、文件、转发卡片**的控件类型与命名，补全 `kind` 映射表。
5. **"经典模式"**（实测发现头部有"切换为经典模式"按钮）：两种布局的消息列表结构差异。
6. **QQ 版本耦合**：`ml-root`、`md-tips__<msgid>` 等是 QQ 自己的 DOM id，跨版本稳定性未知；
   适配器应以"区域名 + 结构推断"为主、id 为辅，并为识别失败输出脱敏节点诊断
   （对齐 v0.6.1 的诊断导出思路）。
7. 提权运行的 QQ（以管理员运行）需要分析进程同样提权——提示即可，非常见场景。

## 四、落地阶段建议

- **P0 单聊全链路**：`qq_ax_win.py`（标题 + 可见消息 + 方向 + 图片占位 + 增量去重），
  CLI 输出 JSON，接现有 `decision_infra.py` 跑通分析。
- **P1 群聊与引用**：补采样 → 完善 sender/quoted/kind。
- **P2 悬浮面板**：`hud_win.py`，复用分析卡样式；QQ 前台联动显隐。
- **P3 设置与打包**：设置界面（Jev URL/Key，Windows Keystore 等价物用 DPAPI）、
  `start.bat`、PyInstaller 发布。

## 五、隐私与合规对照

UIA 是 Windows 官方无障碍 API，本项目用法为**只读遍历**：不截屏、不 OCR、不读 QQ 数据库、
不注入、不 hook、不代发消息；仅读取前台聊天页已渲染的可见节点。API Key 在 Windows 端可用
DPAPI（`CryptProtectData`）按用户加密保存，等价于 Android Keystore 的本地保护。现有 PRIVACY.md
的承诺在 Windows 适配下全部可保持。
