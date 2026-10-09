"""本机配置版本：原始字节快照、元信息预览与显式恢复。"""

import argparse
import configparser
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

FILES = ("ctrl.ini", "账号文本.txt")


def git(store, *args, input_data=None):
    result = subprocess.run(
        ["git", "-C", str(store), *args], input=input_data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError("配置版本库 Git 操作失败；未输出可能含敏感信息的诊断。")
    return result.stdout


def stable_read(project):
    """两次读取一致才保存，避免捕捉程序正在写入的半份文件。"""
    for _ in range(3):
        first = {name: (project / name).read_bytes() for name in FILES}
        time.sleep(0.05)
        second = {name: (project / name).read_bytes() for name in FILES}
        if first == second:
            return first
    raise RuntimeError("运行文件正在变化，请保存配置完成后重试。")


def sensitive_values(data):
    """只在内存中读取凭据，用于拒绝包含真实值的提交说明。"""
    values = set()
    for line in data[FILES[1]].decode("gbk").splitlines():
        parts = line.split("|")
        if len(parts) >= 15:
            for index in (1, 2, 3, 15):
                if index < len(parts) and len(parts[index].strip()) >= 4:
                    values.add(parts[index].strip())
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(data[FILES[0]].decode("gbk"))
    for section in parser.sections():
        for key, value in parser[section].items():
            if re.search(r"token|secret|api.?key|目录|路径", key, re.I) and len(value) >= 4:
                values.add(value)
    return values


def atomic_write(path, data):
    temporary = path.with_name(path.name + ".snapshot-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()  # 仅清理本次创建的中转文件


class ConfigHistory:
    def __init__(self, project, store):
        self.project = Path(project).resolve()
        self.store = Path(store).resolve()
        if self.project == self.store or self.project in self.store.parents or self.store in self.project.parents:
            raise RuntimeError("配置版本库必须与代码目录分开，不能互相嵌套。")

    def ensure_store(self):
        self.store.mkdir(parents=True, exist_ok=True)
        rules = self.store / "README.local.md"
        if not rules.exists():
            rules.write_text(
                "# 本机配置版本约定\n\n仅跟踪 ctrl.ini 和账号文本.txt；保存原始 GBK 字节。"
                "不设置远端，不上传。其他文件仅供本机维护，不自动清理。\n", encoding="utf-8",
            )
        if not (self.store / ".git").exists():
            if any((self.store / name).exists() for name in FILES):
                raise RuntimeError("目标目录已有未管理的配置文件，拒绝覆盖。")
            git(self.store, "init", "-b", "main")
            git(self.store, "config", "core.autocrlf", "false")
            git(self.store, "config", "user.name", "Local configuration")
            git(self.store, "config", "user.email", "local-config@localhost")
            (self.store / ".gitignore").write_text("*\n!ctrl.ini\n!账号文本.txt\n", encoding="utf-8")
        self.validate_store(require_push_block=False)  # 拒绝推送 hook 在本方法末尾安装
        hook = self.store / ".git" / "hooks" / "pre-push"
        if hook.exists() and "seer-local-config-only" not in hook.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError("配置版本库已有其他推送 hook，拒绝覆盖；请先检查。")
        hook.write_text(
            "#!/bin/sh\n# seer-local-config-only\n"
            "echo 'BLOCK: local configuration history must not be pushed.' >&2\nexit 1\n",
            encoding="utf-8", newline="\n",
        )
        hook.chmod(0o755)
        self.validate_store()  # 装好 hook 再核对一次，避免带着不完整的保护继续

    def push_block_installed(self):
        hook = self.store / ".git" / "hooks" / "pre-push"
        return hook.is_file() and "seer-local-config-only" in hook.read_text(encoding="utf-8", errors="replace")

    def validate_store(self, require_push_block=True):
        if not (self.store / ".git").is_dir():
            raise RuntimeError("配置版本库尚未初始化，请先 save。")
        if git(self.store, "remote").strip():
            raise RuntimeError("配置版本库设置了远端，拒绝操作；它只能用于本机。")
        if git(self.store, "config", "--get", "core.autocrlf").strip() != b"false":
            raise RuntimeError("配置版本库必须关闭自动换行转换，避免改变原始字节。")
        tracked = set(git(self.store, "ls-files", "-z").decode("utf-8").split("\0")) - {""}
        if tracked - set(FILES):
            raise RuntimeError("配置版本库存在范围外的跟踪文件，拒绝操作。")
        if require_push_block and not self.push_block_installed():
            raise RuntimeError("配置版本库缺少拒绝推送的本地 hook，请先执行 save 重装后再操作。")

    @contextmanager
    def lock(self):
        path = self.store / ".config-history.lock"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError("已有配置历史操作或遗留锁，请先检查，不自动删除锁。") from None
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            path.unlink()  # 只释放本次操作创建的锁

    def head(self):
        result = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else None

    def commit_data(self, revision):
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", revision):
            raise RuntimeError("版本号必须为 7 至 40 位 Git 提交哈希。")
        sha = git(self.store, "rev-parse", "--verify", revision + "^{commit}").decode("ascii").strip()
        git(self.store, "merge-base", "--is-ancestor", sha, "HEAD")
        return sha, {name: git(self.store, "show", sha + ":" + name) for name in FILES}

    def save_data(self, data, message):
        values = sensitive_values(data)
        head = self.head()
        if head:
            _, previous = self.commit_data(head)
            values.update(sensitive_values(previous))
        if not message.strip() or "\n" in message or any(value in message for value in values):
            raise RuntimeError("提交说明为空、多行或含真实敏感值，请只描述变更用途。")
        if head and previous == data and not git(self.store, "status", "--porcelain").strip():
            return head, False
        for name, content in data.items():
            atomic_write(self.store / name, content)
        git(self.store, "add", "--", *FILES)
        if head and not git(self.store, "diff", "--cached", "--name-only").strip():
            return head, False
        git(self.store, "commit", "--no-gpg-sign", "-F", "-", input_data=(message + "\n").encode("utf-8"))
        return self.head(), True

    def save(self, message):
        self.ensure_store()
        with self.lock():
            return self.save_data(stable_read(self.project), message)

    def history(self):
        self.validate_store()
        if not self.head():
            return "暂无配置版本。"
        return git(self.store, "log", "--date=iso-strict", "--format=%h %ad %s").decode("utf-8")

    def application_running(self):
        if not (self.project / "seer_console" / "main.py").exists():
            return False  # 独立测试目录没有中控台入口
        if os.name != "nt":
            raise RuntimeError("无法在此平台确认中控台已关闭，拒绝实际恢复。")
        project = str(self.project).replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop'; "
            "$project='" + project + "'; "
            "$all=@(Get-CimInstance Win32_Process); "
            "$p=@($all | Where-Object { $_.Name -match '^pythonw?\\.exe$' }); "
            "if (@($p | Where-Object { -not $_.CommandLine }).Count -gt 0) { exit 4 }; "
            "$app=@($p | Where-Object { "
            "$_.CommandLine -match 'seer_console([\\\\/]|\\.)main(\\.py)?' }); "
            "if ($app.Count -eq 0) { $app=@($all | Where-Object { "
            "$_.ExecutablePath -and "
            "$_.ExecutablePath.StartsWith($project + '\\', [StringComparison]::OrdinalIgnoreCase) }) }; "
            "if ($app.Count -gt 0) { exit 3 }"
        )
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode not in (0, 3):
            raise RuntimeError("无法确认中控台进程状态，拒绝实际恢复。")
        return result.returncode == 3

    def restore(self, revision, apply=False):
        self.validate_store()
        sha, target = self.commit_data(revision)
        summary = {"commit": sha[:12], "apply": apply,
                   "files": {name: len(data) for name, data in target.items()}}
        if not apply:
            return summary
        if self.application_running():
            raise RuntimeError("中控台仍在运行，请先关闭中控台；无需关闭游戏账号进程。")
        with self.lock():
            original = stable_read(self.project)
            backup_sha, _ = self.save_data(original, "恢复前自动快照")
            if self.application_running() or stable_read(self.project) != original:
                raise RuntimeError("恢复前运行状态或文件发生变化，已保留快照，未应用恢复。")
            try:
                for name, content in target.items():
                    atomic_write(self.project / name, content)
                if stable_read(self.project) != target:
                    raise RuntimeError("恢复后字节校验失败。")
            except Exception:
                try:
                    for name, content in original.items():
                        atomic_write(self.project / name, content)
                except Exception:
                    raise RuntimeError("恢复及回滚失败，原始文件仍保存在恢复前快照中。") from None
                raise RuntimeError("恢复失败，已回滚原始字节并保留恢复前快照。") from None
            summary["backup_commit"] = backup_sha[:12]
        return summary


def main():
    default_project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=default_project)
    parser.add_argument("--store", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("save")
    save.add_argument("--message", required=True)
    commands.add_parser("history")
    restore = commands.add_parser("restore")
    restore.add_argument("--commit", required=True)
    restore.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        history = ConfigHistory(args.project, args.store or args.project.resolve().parent / "SEER-private-config")
        if args.command == "save":
            sha, changed = history.save(args.message)
            print(("已保存配置版本：" if changed else "配置未变化，沿用版本：") + sha[:12])
        elif args.command == "history":
            print(history.history())
        else:
            result = history.restore(args.commit, args.apply)
            print(("已恢复：" if args.apply else "仅预览，不修改运行文件：") + result["commit"])
            for name, size in result["files"].items():
                print(f"  {name}: {size} 字节")
            if args.apply:
                print("恢复前快照：" + result["backup_commit"])
        return 0
    except RuntimeError as error:
        print("操作失败：" + str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, configparser.Error):
        print("操作失败，请检查文件、版本、锁或中控台状态；敏感内容未输出。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
