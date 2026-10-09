# 维护工具约定

- 本目录只放维护脚本、说明和对应测试；真实账号、配置、版本库、锁文件和报告存放在项目目录之外。
- `config_history.py` 用标准库维护本机配置快照，不设置远端、不联网；保存和恢复保持原始字节。
- `repo_guard.py` 检查公开提交，安装的 hooks 放在本机 `.git/hooks/`；hook 路径和本机敏感数据不提交。
- 测试放 `seer_console/tests/test_local_maintenance.py`，使用系统临时目录中的虚构数据。测试目录保留供检查，清理须另获许可。
- 两个脚本的输出编码：直接在中控台/终端运行按控制台编码显示中文；输出被管道或重定向（非终端）时自动改用 UTF-8，避免中文在别的程序里变成乱码。

## 保存真实配置版本

在项目根目录运行：

```powershell
python tools/config_history.py save --message "调整日常任务设置"
python tools/config_history.py history
```

默认保存根目录 `ctrl.ini`、`账号文本.txt` 到同级 `SEER-private-config/` 的独立 Git 仓库。不输出文件内容；说明只写用途，不能包含密码、token、账号或配置值。没有变化时不创建新版本。

只修改未保存的界面选项不会进入快照；先在中控台保存配置，再运行 `save`。保存过程中若文件持续变化，脚本拒绝生成快照。

## 恢复

```powershell
python tools/config_history.py restore --commit <版本号>
# 关闭中控台后，再显式应用；无需关闭游戏账号进程
python tools/config_history.py restore --commit <版本号> --apply
```

第一条只预览版本和文件大小。实际恢复前检测中控台进程、保存恢复前版本，并检查文件未被并发改写。两个文件替换失败时尝试恢复原始字节；恢复版本不会删除后续历史。恢复后重新打开中控台读取文件。

进程检测把"命令行里运行 `seer_console\main.py`／`-m seer_console.main` 的解释器"和"从项目目录启动的可执行体（打包成 exe 的形式）"都算作中控台在运行；只要读不到某个 Python 进程的命令行，就按"状态未知"拒绝实际恢复（拿不到命令行时无法排除它就是中控台）。因此在项目目录内跑虚拟环境或打包产物时恢复会被拒绝，这是有意的保守行为；确认不需要保留中控台时先结束这些进程。

版本库只在本机保存，不是异地备份。脚本拒绝操作设置了远端的版本库，并安装拒绝推送的本地 hook；校验版本库时会一并确认这个 hook 仍在（缺失或被换成其他 hook 时拒绝操作，运行一次 `save` 即可重装）。不要给它设置远端。不要把配置版本库放进代码目录。诊断可显式使用 `--project <目录> --store <目录>` 指定独立测试环境。

## 公开仓库检查

```powershell
python tools/repo_guard.py install
python tools/repo_guard.py check
```

每次新克隆需要安装本地 hooks。检查涵盖已暂存内容和所有本地分支、标签的历史；禁止真实配置、账号、`information/`、任何 `AGENTS.md`、备份、日志和诊断输出进入公开仓库。截图、网页、办公文档、压缩包和影音文件（`repo_guard.py` 的 `UNSAFE_SUFFIXES`）在任意层级都禁止；Markdown 只放行 `ALLOWED_MARKDOWN` 白名单里的 `README.md` 与 `tools/README.md`，其余 Markdown（含子目录笔记）一律拦截——需要新增说明文档时先把它加进白名单。同时从本机真实文件、配置版本历史和迁移前历史提取已知凭据，只在内存中匹配，不输出匹配值。

这项检查不能识别所有未知秘密，提交前仍须核对文件范围。检查失败应修正提交内容或分支历史，不绕过检查。
