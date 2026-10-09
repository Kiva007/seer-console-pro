# 维护工具约定

- 本目录只放维护脚本、说明和对应测试；真实账号、配置、版本库、锁文件和报告存放在项目目录之外。
- `config_history.py` 用标准库维护本机配置快照，不设置远端、不联网；保存和恢复保持原始字节。
- `repo_guard.py` 检查公开提交，安装的 hooks 放在本机 `.git/hooks/`；hook 路径和本机敏感数据不提交。
- 测试放 `seer_console/tests/test_local_maintenance.py`，使用系统临时目录中的虚构数据。测试目录保留供检查，清理须另获许可。

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

版本库只在本机保存，不是异地备份。不要给它设置远端。不要把配置版本库放进代码目录。诊断可显式使用 `--project <目录> --store <目录>` 指定独立测试环境。

## 公开仓库检查

```powershell
python tools/repo_guard.py install
python tools/repo_guard.py check
```

每次新克隆需要安装本地 hooks。检查涵盖已暂存内容和所有本地分支、标签的历史；禁止真实配置、账号、`information/`、任何 `AGENTS.md`、备份、日志和诊断输出进入公开仓库。同时从本机真实文件、配置版本历史和迁移前历史提取已知凭据，只在内存中匹配，不输出匹配值。

这项检查不能识别所有未知秘密，提交前仍须核对文件范围。检查失败应修正提交内容或分支历史，不绕过检查。
