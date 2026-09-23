# Windows 构建（PyInstaller 单文件 exe）

在 Windows 10/11 + Python 3.12/3.13 环境下，从仓库根目录执行：

```bat
py -3.13 -m pip install pyinstaller uiautomation psutil
py -3.13 -m PyInstaller --noconfirm --onefile --windowed --name jev-qq-analyst ^
  --collect-all uiautomation --collect-all comtypes ^
  --add-data "shared/decision_questions.json;shared" ^
  --distpath packaging/dist --workpath packaging/build --specpath packaging ^
  src/hud_win.py
```

产物：`packaging/dist/jev-qq-analyst.exe`（约 22 MB，无需目标机安装 Python）。

重建（spec 已提交，日常用这条即可）：

```bat
py -3.13 -m PyInstaller --noconfirm --distpath packaging/dist --workpath packaging/build packaging/jev-qq-analyst.spec
```

说明：

- `--add-data` 把 `shared/decision_questions.json` 打进包；`decision_infra.py`
  在冻结环境下从 `sys._MEIPASS` 解析该文件。注意 `--add-data` 的源路径按
  `--specpath`（即 `packaging/`）解析，写相对路径时要以仓库根为基准或用绝对路径。
- `--collect-all uiautomation --collect-all comtypes` 确保 UIA COM 绑定完整打入。
- 日常开发无需打包：仓库根目录的 `start.bat` 直接以源码运行（缺依赖时自动安装）。

运行期文件（不进仓库）：设置与日志在 `%APPDATA%\jev-qq-analyst\`
（`config.json` 中 API Key 经 DPAPI 加密、`hud.log` 仅记录阶段信息，不含聊天文本）。
