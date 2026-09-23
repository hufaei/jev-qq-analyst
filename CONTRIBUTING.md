# 贡献指南

当前产品分析 macOS QQ 当前窗口内可见的对方消息：`src/qq_ax.py` 负责辅助功能（AX）读取，`src/decision_infra.py` 请求 Decision Infra，`src/hud.py` 显示逐条判断。自己的消息只作上下文。功能与隐私边界见 [README](README.md) 和 [PRIVACY.md](PRIVACY.md)。

## 开工前认领 issue

为避免重复工作，先检查 assignee、认领评论和关联 PR：

```bash
gh issue view <n> --json assignees
gh issue view <n> --comments
gh pr list --state open --search "<n>"
```

没有其他人认领时，在 issue 下评论拟做的改动，并设置自己为 assignee：

```bash
gh issue comment <n> --body "认领：<一句话说明计划>"
gh issue edit <n> --add-assignee @me
```

认领后 7 天没有 open PR，其他人可在原认领评论下回复后接手。新想法先开 issue 讨论；会修改相同文件的多个 issue 串行处理。

## 提交 PR

- 从目标仓库的 `main` 建分支 `<type>/<issue号>-<摘要>`，如 `fix/11-qq-message-direction`；没有写权限则从 fork 提 PR。
- 一个 PR 解决一件事。commit 使用 Conventional Commits 和中文说明，如 `fix(qq_ax): …`、`docs: …`。
- PR 描述写清改动、原因、实际验证结果；关联 issue 用 `Closes #n` 写在描述中。当前仓库要求 squash 合并。
- 遇到冲突时，本地更新目标分支、解决冲突并重跑相关验证，再推送。

## 验证改动

CI 在 PR 和 `main` push 上运行以下离线测试；本地先运行同一命令：

```bash
uv run python -B -m unittest discover -s tests
```

测试使用合成数据，不读取真实聊天，也不调用 Provider。按改动范围补充检查：

| 改动 | 额外检查 |
| --- | --- |
| QQ AX 读取、发送者或引用解析 | `uv run python src/qq_ax.py` 看数量与方向摘要；在真实 QQ 窗口检查可见消息、长消息、自己与对方、引用和切换会话 |
| Decision Infra 请求、配置或响应 | 确认 `tests/test_decision_infra.py` 与 `tests/test_settings.py` 覆盖改动；在已配置的网关验证精确路由与错误显示 |
| HUD 显示、轮询或缓存 | `./preview.command` 检查合成卡片；真实 QQ 中检查前台切换、滚动后缓存和逐条分析 |

真实 QQ 验收需要 macOS“辅助功能”授权。`preview.command` 不读 QQ、不调用网关，不能代替实际接入验证。修改用户可见行为请同步 README；修改配置或数据流向请同步 `.env.example`、FAQ 与隐私说明。

## 保持当前边界

- 只读取 QQ 已加载、可见的辅助功能节点。不截图、不 OCR、不读数据库、不注入、不 hook、不自动滚动。
- 不确定收发方向时不要请求模型；引用只作背景。自己的消息不单独分析。
- QQ 应用不保存 Provider Key，也不直连 TypeSafe；模型 ID 是 Decision Infra 的精确路由，不静默切换模型。
- 当前 HUD 不生成、复制、填入或发送回复。仓库中的旧生成、OCR 和填入模块不是当前界面的调用路径。

更具体的入口与改动注意事项见 [AGENTS.md](AGENTS.md)。
