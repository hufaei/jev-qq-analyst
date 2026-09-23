# jev-qq-analyst

读取 macOS QQ 当前窗口内每一条可见的对方消息，通过 [Decision Infra](https://github.com/hufaei/decision-infra) 精确路由 `jev-latest`，逐条显示意图、情绪、风险和下一步；自己的消息只作上下文，不作为分析目标，也不生成回复。

> **接入边界已调整：** QQ 应用不再直连 TypeSafe、不保存 Provider Key，也不在失败时偷偷换模型。Key、Provider 和模型生命周期全部留在 Decision Infra。

> 当前版本不截图、不 OCR、不读取 QQ 数据库、不注入、不 hook，也不会自动发送或填入消息。它只处理本人设备、本人账号、QQ 当前窗口已经加载的文字节点。

## 快速开始

要求：Apple Silicon Mac、macOS 13+、已登录的 QQ，以及本机运行的 [Decision Infra](https://github.com/hufaei/decision-infra)。本项目使用 Python 3.12 和 `uv`；`start.command` 会在缺少 `uv` 时尝试安装。启动 infra 需要 Node.js 22+ 和 pnpm 12.5.1。

1. 在本机 Decision Infra 项目中配置 Provider 并启动网关：

```bash
cd /你的路径/decision-infra
pnpm install
# TYPESAFE_API_KEY 只配置在这个项目的 .env 或启动环境中
pnpm gateway
```

2. 另开终端确认网关在线：

```bash
curl http://127.0.0.1:8080/healthz
```

应返回 `{"status":"ok"}`。这只证明网关在线；`jev-latest` 是否可用仍由下一步的真实决策验证。

3. 打开“系统设置 → 隐私与安全性 → 辅助功能”，给运行程序的终端或 `jev-jarvis.app` 授权。`jev-jarvis.app` 是当前打包脚本仍使用的兼容名称。
4. 保持 QQ 主窗口打开并切到需要分析的聊天。
5. 在本项目启动悬浮窗：

```bash
./start.command
```

只检查 QQ 读取链路，不启动模型：

```bash
uv run python src/qq_ax.py
```

该命令只输出窗口尺寸、消息数量、收发方向和耗时，不打印聊天标题、昵称或正文。

## 在页面上验收

先双击 `preview.command`，或在终端运行 `./preview.command`。它只显示合成消息，不读取 QQ、不请求模型；红色窗口按钮退出预览。

验收真实读取与分析：

1. 打开 QQ，并让另一个 QQ 账号发来“这个事情今天能搞定吗？”。
2. 保持该聊天位于前台一两秒。
3. 灰白悬浮窗按 QQ 可视区顺序显示消息：对方的话是完整分析卡，自己的话是右侧较窄的蓝灰色上下文条，不调用 Jev。分析卡先显示原话、主要判断和风险；“现在回复”概率是前部的小色块，三个意图候选各有独立概率标记；“下一步”按适配分显示为最多三条独立的行，分数靠右对齐。
4. 状态显示 `对方 2 条 · 已分析 2 条`（数字随可视区变化）；实际模型路由可在启动日志中核实，预览模式不做模型调用。
5. 如果网关返回 `404`，说明它没有注册 `jev-latest`，通常是 Decision Infra 进程没有拿到 `TYPESAFE_API_KEY`。卡片会显示分析失败，可在日志中查看错误类型。
6. 如果网关返回 `503`，说明它背后的 Provider 不可用。应用不会改用其他模型。
7. 切到浏览器后面板应隐藏；回到 QQ 后重新读取当前可见消息。
8. 同一屏里放两条连续的对方消息和一条自己的回复：应依次出现两张分析卡、一条“我 · 上下文”条；状态只计数两条对方消息。滑到别处再滑回来，已经分析过的消息应从进程内缓存回显；新看到的对方历史消息会分析。收发方向优先取消息行内名称为“我”或对方显示名的发送者容器；缺失时才用 AX 头像或明显的左右位置回退。遇到很长、横跨窗口的消息时，要实测方向是否仍正确。
9. 用 QQ 的“引用/回复消息”功能分别让自己和对方发一条带引用的新消息，尤其可引用一条含“我”或对方名字的话：收发方向应取新消息行的发送者，不受引用中的名字影响。如果 QQ 辅助功能树标出了引用容器，卡片标题会显示“引用：…”，原话区只显示新写内容。若标题没有“引用”，不要把分析结果当成已正确区分引用；请记录 QQ 版本和页面表现供适配。
10. 看一条无需立即回复的消息时，检查前部“现在回复”的概率色块和“暂不回复，留出空间”；对方明确提问时，检查“下一步”的三条策略是否各占一行、按适配分排序。蓝灰色意图标记表示概率高低，行动分数的绿/琥珀/灰表示适配度高/中/低；颜色不代替数字。这里是策略判断，不是生成待发送的回复。

页面底部不应出现候选回复、“复制”“填入”或“发送”按钮。

## 能看到什么

QQ 辅助功能树提供以下输入：

| 数据 | 说明 |
|---|---|
| 当前聊天标题 | 从消息区域上方的 AX 标题节点读取 |
| 可见消息正文 | 读取“消息列表”容器下的 `AXStaticText` |
| 表情 | 文字中的 Unicode 表情可读；`AXImage` 有具体辅助功能名称时读作 `[名称]`。只有泛称或没有描述的贴图无法理解 |
| 收发方向 | 优先读取消息行自身的发送者容器名称：“我”为自己，其余显示名为对方；不从整条消息文字中搜名字。缺失时才回退到 AX 头像（左/右）和气泡位置；仍不明确则不分析 |
| 被引用的文字 | 仅当 QQ 的 AX 节点明确标注引用/回复容器时，与新写正文分开；引用只作背景 |
| 消息顺序和位置 | 用于上下文、去重和可选检测框 |

Jev 返回以下分析结果：

| 字段 | 含义 |
|---|---|
| `intent` | 当前话语意图；在原有八种上增加求确认、倾诉、设边界、邀约 |
| `confidence` | 最终意图的置信度 |
| `intent_probs` / `intent_ranking` | 所有意图的概率分布及排名前三的候选 |
| `risk` | 0–9 的风险期望值 |
| `risk_probs` | 十个风险等级的完整概率分布 |
| `behavior` / `emotion` / `need` | 对话行为、文字可支持的情绪、当前可能需要的回应；把握低时不强行显示肯定判断 |
| `signals` | 隐含请求、催促感、明确边界是概率信号，只显示超过阈值的提示；不是长期关系结论 |
| `reply_probability` | Jev `noul` 对“现在回复是否更合适”的 `P(true)`；低于 40% 时只建议暂不回复，高于 60% 时不推荐沉默，中间区间保持开放 |
| `action_rankings` / `actions` | Jev 对 12 种中文行动策略逐项使用 `score` 的 0–4 适配度，显示前三名；这些是策略，不是生成的回复文本，也不是概率 |
| `backend` | Decision Infra 实际返回的精确路由，如 `infra/jev-latest` |

示例：

```json
{
  "intent": "催进度",
  "confidence": 0.87,
  "risk": 4.2,
  "behavior": "催促",
  "emotion": "着急",
  "need": "时间承诺",
  "reply_probability": 0.84,
  "intent_ranking": [{"label": "催进度", "probability": 0.87}, {"label": "问进度", "probability": 0.13}],
  "action_rankings": [{"id": "schedule", "label": "给出可信的时间安排", "score": 3.8}],
  "actions": ["给出可信的时间安排"],
  "backend": "infra/jev-latest"
}
```

## 配置 Decision Infra

打开悬浮窗里的“模型设置”，界面只显示“判断 · Decision Infra”。也可以按 [`.env.example`](.env.example) 编辑 `~/.config/jev-jarvis/env`。这个路径与日志 `~/Library/Logs/jev-jarvis.log` 沿用旧应用名，重命名仓库不会迁移它们：

```bash
export DECISION_INFRA_BASE_URL="http://127.0.0.1:8080"
export DECISION_INFRA_MODEL="jev-latest"
```

不要把 `TYPESAFE_API_KEY` 放进 QQ 应用配置。它只属于 Decision Infra 的 `.env` 或启动环境。

当前 HUD 不创建回复生成器、不使用 OpenAI/Anthropic 配置、不启动候选生成或候选排序线程，也不显示“复制”“填入”按钮。`DECISION_INFRA_MODEL` 是精确路由；不存在时直接报错，不自动换模型。

## 工作原理

```text
QQ 主进程 com.tencent.qq
  → AXFocusedWindow / AXMainWindow
  → 递归穿过 AXGroup
  → AXWebArea（/main/message）
  → “消息列表”
  → 当前可见 AXStaticText 和有具体描述的 AXImage
  → Decision Infra /v1/systemone
  → jev-latest 多问题结构化判断
  → macOS 悬浮窗
```

QQ 的 Electron 辅助功能树有时不会出现在 `AXWindows`。适配器会启用 `AXManualAccessibility`，并优先从 `AXFocusedWindow`、`AXMainWindow` 开始遍历；不会在 `AXGroup` 或 `AXWebArea` 停止。

## 已知限制

- 只读取当前已经加载、当前可见的消息；不会自动滚动，也不再只保留最后 12 条可见行。每条可见对方消息单独判断；自己的消息只用作上下文。进程内最多保留每个会话最近 32 条已见消息、24 个会话和 128 个判断缓存；退出即清空。一次判断最多使用目标消息之前的 8 条已见消息（双方都算），拼接后最多 2000 个 Unicode 字符，包括昵称和换行；不是 8 个来回，也不会补齐没读到的历史。
- Unicode 表情可读；有具体辅助功能描述的表情图片可读其名称。纯图片/独立表情消息若没有正文文本，当前无法可靠地将其与 AX 头像分开，因而不作为分析目标。普通图片、无描述贴图、语音和无法识别方向的卡片也不会进入分析。
- 滑动到此前没见过的对方历史消息也会分析；滑回已经分析过的消息会用缓存回显。QQ AX 当前没有可依赖的稳定消息 ID；缓存键由会话、发送者、正文、引用文字和上一条对方正文拼成，不是真正唯一标识。完全相同的短句在少数情况下可能共用缓存判断；菜单“立即重新分析”可清除此会话的判断缓存。
- 情绪、需求、言外之意都是基于文字的推断；不输出“亲密度”“权力距离”“情感投资”等长期关系定论。
- 居中时间戳和系统提示会被过滤；发送者容器可读时不依赖气泡宽度或头像位置。若该容器缺失，即使长文字横跨窗口，同一行有可确认的左侧 AX 头像仍按对方消息处理。屏幕上有头像不代表 QQ 把它暴露为 AX 图片；此时仅在气泡左右留白差异明显、且未几乎铺满整个消息区时作最后回退，否则方向未知、不触发分析。
- 引用消息若没有明确的 AX 引用容器，仍可能与新写正文混在一起；此时不能保证引用分析正确。
- QQ 改版可能改变辅助功能层级，需要重新适配容器名称或路径。
- 悬浮窗只在 QQ 位于前台时显示；切到其他应用会清空旧结果。
- 消息正文和进程内最近已见上下文会发送给本机 Decision Infra；后续是否出网取决于它路由到的 Provider。`jev-latest` 会由 infra 发送到托管 Jev。

## 开发与验证

```bash
uv run python -B -m unittest discover -s tests
uv run python src/qq_ax.py
```

测试使用合成 AX 树，不读取真实聊天。真实 QQ 冒烟检查的诊断输出同样不包含消息正文。

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，路径与报错速查见 [FAQ](docs/FAQ.md)，数据流向见 [PRIVACY.md](PRIVACY.md)。

关键文件：

- `src/qq_ax.py`：QQ 辅助功能树读取与消息解析。
- `src/decision_infra.py`：Decision Infra contract 客户端、精确路由和响应校验。
- `src/judge.py`：原有意图与风险等级定义；行动策略和 Jev 三种原语的组合位于 `src/decision_infra.py`。
- `src/hud.py`：Jev-only 悬浮窗和前台/旧结果边界。
- `tests/test_qq_ax.py`：深层遍历、可见行、时间戳与收发方向回归。

## 许可与隐私

MIT（见 `LICENSE`）。只应在本人设备和本人账号上使用。项目不支持读取他人聊天、绕过访问控制、解密数据库或自动发送消息。
