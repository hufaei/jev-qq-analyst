# Windows 构建（PyInstaller 单文件 exe）

发布包由 GitHub Actions 的 Windows runner 自动构建。需要从源码重建时，在 Windows 10/11 安装 `uv`，从仓库根目录执行：

```bat
uv run --locked --python 3.13 python -m unittest discover -s tests -p test_windows_adapter.py
uv run --locked --python 3.13 --with pyinstaller==6.16.0 python -m PyInstaller --noconfirm --distpath packaging/dist --workpath packaging/build packaging/jev-qq-analyst.spec
```

产物：`packaging/dist/jev-qq-analyst.exe`（约 22 MB，无需目标机安装 Python）。

说明：

- spec 把 `shared/decision_questions.json`、`uiautomation` 和 `comtypes` 打进包；`decision_infra.py` 在冻结环境下从 `sys._MEIPASS` 读取问题契约。
- `uv.lock` 锁定运行依赖；PyInstaller 版本在构建命令与 CI 中固定。CI 会实际生成 EXE，但不连接真实 QQ。
- 日常开发无需打包：仓库根目录的 `start.bat` 直接以源码运行（缺依赖时自动安装）。
- 当前 EXE 未做 Authenticode 签名，下载时 Windows 可能显示来源警告。

运行期文件（不进仓库）：设置与日志在 `%APPDATA%\jev-qq-analyst\`
（`config.json` 中 API Key 经 DPAPI 加密、`hud.log` 仅记录阶段信息，不含聊天文本）。
