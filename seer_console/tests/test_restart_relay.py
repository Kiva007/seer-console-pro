# -*- coding: utf-8 -*-
"""重启中继回归测试（2026-10 bug：_restart_app 的 cmd 中继静默失败）。

用户症状：工具→重启确认后，中控台关闭，弹出 ping 127.0.0.1 的命令行后
自动关闭，中控台没有启动。

诊断期实测（marker 变体测试，红→绿证据）：
  subprocess.Popen(["cmd", "/c", relay]) 时 Python 的 list2cmdline 把中继串
  整体加引号、内嵌引号转义为 \\"：
      cmd /c "ping ... & cd /d \\"D:\\...\\" & start \\"\\" \\"pyw\\" ..."
  cmd 不认 \\" 转义 → cd /d \\"...\\" 报「卷标语法错误」、
  start 拿不到可执行文件 → 新实例永远起不来（DETACHED/无DETACHED/无ping
  三变体全红）。直接 Popen([pythonw, 脚本]) 0.25s 绿。

修复机制（本文件锁定）：纯 python 中继脚本写入 %TEMP%，由本解释器
（pythonw，GUI 子系统无窗口）Popen 拉起；sleep 等旧进程退出后 Popen
新实例并自删、留日志。零 cmd / 零 start / 零引号转义。

运行（无 pytest 依赖）：
    python seer_console/tests/test_restart_relay.py
"""

import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.ui.main_window import build_restart_relay_script  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_RESULTS = {"pass": 0, "fail": 0}


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  {detail}" if detail and not ok else ""))
    _RESULTS["pass" if ok else "fail"] += 1


def _wait_marker(path, seconds=8.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if os.path.isfile(path):
            return time.time() - t0
        time.sleep(0.2)
    return None


def test_relay_content():
    """中继脚本内容：纯 python 机制、零 cmd/start 危险结构、路径正确。"""
    print("\n[1] 中继脚本内容")
    root = os.path.join(tempfile.gettempdir(), "seer_relay_t")
    target = os.path.join(root, "main.py")
    script = build_restart_relay_script(root, target,
                                        os.path.join(tempfile.gettempdir(),
                                                     "seer_restart_relay.log"))
    check("1a 不含 cmd/start 危险结构与 \\\" 转义（红因）",
          'start ""' not in script and "cmd /c" not in script
          and '\\"' not in script and "ping" not in script)
    check("1b 含 sleep 等待 + Popen 拉起 + 自删",
          "time.sleep" in script and "subprocess.Popen" in script
          and "os.remove" in script)
    check("1c 含 root/target/日志路径",
          repr(root) in script and repr(target) in script)
    # 生产路径生成（repo 真路径）：入口必须是 seer_console\main.py
    prod = build_restart_relay_script(
        REPO, os.path.join(REPO, "seer_console", "main.py"),
        os.path.join(tempfile.gettempdir(), "seer_restart_relay.log"))
    check("1d 生产中继目标=仓库 seer_console/main.py",
          repr(os.path.join(REPO, "seer_console", "main.py")) in prod)
    check("1e 重启标记与前台激活权限传递",
          '"--activate-after-restart"' in prod
          and "AllowSetForegroundWindow(p.pid)" in prod)
    compile(prod, "test-restart-relay", "exec")


def test_relay_survives_parent_exit():
    """核心 bug 模式：父进程 spawn 中继后立即退出，中继仍须在延时后拉起目标。"""
    print("\n[2] 父进程立即退出后中继仍拉起目标（bug 模式闭环）")
    work = os.path.join(tempfile.gettempdir(), "seer_relay_e2e")
    os.makedirs(work, exist_ok=True)
    marker = os.path.join(work, "_marker.done")
    target = os.path.join(work, "_marker_target.py")
    log_path = os.path.join(work, "_relay.log")
    for p in (marker, log_path):
        if os.path.isfile(p):
            os.remove(p)
    with open(target, "w", encoding="utf-8") as f:
        f.write("open(r'%s', 'w').write('ok')\n" % marker)

    relay_path = os.path.join(work, "_relay.py")
    with open(relay_path, "w", encoding="utf-8") as f:
        f.write(build_restart_relay_script(work, target, log_path, delay=0.5))

    # runner：拉起中继后立即退出（对齐真实应用 spawn 中继 → QApplication.quit）
    runner = os.path.join(work, "_spawn_and_exit.py")
    with open(runner, "w", encoding="utf-8") as f:
        f.write(
            "import os, subprocess, sys\n"
            "subprocess.Popen([sys.executable, %r],\n"
            "                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP\n"
            "                 | getattr(subprocess, 'DETACHED_PROCESS', 0),\n"
            "                 close_fds=True)\n"
            "os._exit(0)\n" % relay_path)
    subprocess.run([sys.executable, runner], timeout=15)

    took = _wait_marker(marker, 8)
    check("2a 父已退出，中继仍拉起目标（marker 生成）",
          took is not None, f"waited 8s, marker missing")
    if took:
        print(f"       （marker 出现于 {took:.2f}s）")
    time.sleep(0.5)
    log_txt = (open(log_path, encoding="utf-8").read()
               if os.path.isfile(log_path) else "")
    check("2b 中继日志含 launched（可诊断）", "launched" in log_txt,
          log_txt[-200:])


def main():
    print("===== 重启中继回归测试 =====")
    test_relay_content()
    test_relay_survives_parent_exit()
    p, f = _RESULTS["pass"], _RESULTS["fail"]
    print(f"\n===== 结果: {p} 通过, {f} 失败 =====")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
