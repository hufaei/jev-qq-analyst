<!-- 一个 PR 只做一件事 -->

## 改了什么

<!-- 一句话说清改动范围，例如：修改 src/qq_ax.py 的消息方向识别逻辑 -->

## 为什么改

<!-- 关联 issue，例如：Closes #12 -->

## 怎么测的

<!-- 手动验证方式或日志片段；有实测数字就写实测数字 -->

- [ ] 只读取当前 QQ 窗口可见的消息，不截图、不读取 QQ 数据库、不注入、不 hook，不自动发送或填入消息
- [ ] 未在 QQ 应用中保存 Provider Key；模型凭据只配置在 Decision Infra
- [ ] 已运行 `uv run python -B -m unittest discover -s tests`
- [ ] 改了用户可见行为 → 已更新 README.md
