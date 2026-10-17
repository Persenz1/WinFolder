# WinFolder：源码部署与维护

本项目按源码部署。每台电脑复用本机 Python 环境，由 agent 补齐依赖并配置启动路径，不制作包含 Python/Qt 的大安装包。

## 在新电脑安装

1. 将仓库放到用户指定的长期目录。程序在 Windows 上运行；推荐 64 位 Python 3.12，这是已验证的环境。
2. 先检查本机已有 Python/Conda 环境，选择可用的解释器。不要照搬其它电脑的盘符、用户名或环境路径。新建环境前先征求用户同意。
3. 用选中的 `python.exe` 显式执行 `-m pip install -r requirements.txt`。已满足要求的依赖会复用；不要无故升级整个环境。
4. 在仓库根目录写 `pythonw.path`：内容是该环境中 `pythonw.exe` 的绝对路径，不加引号，使用 UTF-16LE（PowerShell 的 `-Encoding Unicode`）。这个文件已被 Git 忽略，不提交任何机器专用路径。
5. 双击 `启动 WinFolder.vbs`，或从 PowerShell 运行该文件。首次启动会出现管理窗口，后续通常只显示托盘图标。
6. 检查 `%APPDATA%\WinFolder\winfolder.log` 是否成功启动点击监听，再测试一个现有分组的展开、应用启动和切窗收起。不要为了测试删除用户文件。

无现成 Python 时，先与用户确定安装位置或新环境，再继续。不要修改 Windows 全局 PATH 来适配本程序。

## 更新、迁移与开机自启

- 更新代码前停止本仓库的 WinFolder 实例；保留 `pythonw.path`、`%APPDATA%\WinFolder` 设置和桌面分组。
- 只停止命令行中明确包含本仓库 `run.pyw` 的进程，不结束其它 Python 程序。
- 改变项目位置或解释器后，重新写 `pythonw.path`；若 WinFolder 原本启用了开机自启，用选中的 `python.exe` 从仓库目录执行：
  ```powershell
  & $winFolderPython -c "from winfolder import autostart; assert autostart.enable()"
  ```
- 若原本未开启自启，保持关闭。自启项位于当前用户的 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\WinFolder`。
- 后台启动辅助进程时使用隐藏窗口。部署完成后保留一个正常运行的实例即可。

## 修改边界

- 只处理当前需求，不顺手重构。不要把缓存、日志、临时测试数据、Python 环境或第三方依赖副本提交到仓库。
- 分组是真实文件夹。文件操作失败必须保留用户数据；恢复文件清单不能使用过滤过的显示列表。
- 功能改动只做直接相关的验证。临时文件放在仓库 `_tmp/` 的专属子目录中。
