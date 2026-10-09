"""检查公开 Git 内容；只输出文件路径和提交号，不输出敏感值。"""

import argparse
import configparser
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys


# 任意层级都不上传的扩展名：截图、网页、办公文档、压缩包、影音。
UNSAFE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico",
    ".html", ".htm", ".pdf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".mp4", ".avi", ".mov", ".mkv", ".wav",
)
# 允许上传的 Markdown 白名单（相对仓库根的小写 POSIX 路径）；其余 Markdown 一律不上传。
ALLOWED_MARKDOWN = ("readme.md", "tools/readme.md")


def run_git(project, *args, git_dir=None):
    command = ["git", "--git-dir", str(git_dir)] if git_dir else ["git", "-C", str(project)]
    result = subprocess.run(command + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("仓库检查的 Git 操作失败。")
    return result.stdout


def forbidden_path(name):
    path = PurePosixPath(name)
    parts = [part.lower() for part in path.parts]
    base = parts[-1]
    if "information" in parts or base == "agents.md":
        return True
    if base in ("ctrl.ini", "账号文本.txt") or base.startswith(".env"):
        return True
    if any(part in ("__pycache__", ".venv", "venv", "回收站", "seer-private-config", "seer-re-legacy")
           or part.startswith(".git") and part != ".gitignore" for part in parts):
        return True
    if ".bak" in base or ".snapshot-" in base or base.endswith((".log", ".token", ".pem", ".key", ".lnk", ".pyc", ".pyo")):
        return True
    if len(parts) == 1 and base.startswith(("_", "diag_")):
        return True
    if base.endswith(UNSAFE_SUFFIXES):
        return True  # 截图、网页、办公文档、压缩包等任意层级都不上传
    if base.endswith(".md") and "/".join(parts) not in ALLOWED_MARKDOWN:
        return True  # 只放行白名单内的说明文档，避免本机笔记随子目录混入
    if len(parts) == 1 and base.endswith(".txt") and base != "账号文本.example.txt":
        return True
    return False


def extract_values(name, raw):
    """从本机敏感文件提取足够长的凭据与账号标识，不打印或持久化。"""
    values = set()
    text = raw.decode("gbk", errors="replace")
    if name.endswith("账号文本.txt"):
        for line in text.splitlines():
            fields = line.split("|")
            if len(fields) >= 15:
                for index in (1, 2, 3, 15):
                    if index < len(fields) and len(fields[index].strip()) >= 4:
                        values.add(fields[index].strip())
    elif name.endswith("ctrl.ini"):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error:
            raise RuntimeError("本机敏感配置无法解析，拒绝在未知状态下上传。") from None
        for section in parser.sections():
            suffix = section.rsplit(".", 1)[-1]
            if re.fullmatch(r"\d{5,}", suffix):
                values.add(suffix)
            for key, value in parser[section].items():
                if re.search(r"token|secret|api.?key|目录|路径", key, re.I) and len(value) >= 4:
                    values.add(value)
    return values


def object_names(project, git_dir=None):
    raw = run_git(project, "rev-list", "--objects", "--all", git_dir=git_dir)
    for row in raw.decode("utf-8").splitlines():
        sha, separator, name = row.partition(" ")
        if separator:
            yield sha, name


def known_values(project):
    values = set()
    git_directory = Path(run_git(project, "rev-parse", "--absolute-git-dir").decode().strip())
    settings_path = git_directory / "seer-guard.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    sensitive_project = Path(settings.get("sensitive_project", project)).resolve()
    for name in ("ctrl.ini", "账号文本.txt", "information/ctrl.ini", "information/账号文本.txt"):
        path = sensitive_project / name
        if path.is_file():
            values.update(extract_values(name, path.read_bytes()))
    histories = [sensitive_project.parent / "SEER-private-config" / ".git"]
    if settings.get("legacy_git"):
        histories.append(Path(settings["legacy_git"]))
    for directory in histories:
        if not directory.exists():
            continue
        for sha, name in object_names(project, directory):
            if name.endswith(("ctrl.ini", "账号文本.txt")):
                raw = run_git(project, "cat-file", "blob", sha, git_dir=directory)
                values.update(extract_values(name, raw))
    return values


def tree_entries(project, revision):
    raw = run_git(project, "ls-tree", "-r", "-z", "--full-tree", revision)
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, sha = header.decode("ascii").split()
        yield name.decode("utf-8"), sha, kind


def staged_entries(project):
    raw = run_git(project, "ls-files", "--stage", "-z")
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, sha, stage = header.decode("ascii").split()
        if stage != "0":
            raise RuntimeError("索引存在未解决的合并冲突，拒绝上传。")
        yield name.decode("utf-8"), sha, "blob"


def check(project):
    project = Path(project).resolve()
    values = known_values(project)
    literals = [re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)")
                if value.isdecimal() else re.compile(re.escape(value)) for value in values]
    token_pattern = re.compile(r"(?:github_pat_|gh[pousr]_|sk-(?:proj-)?)[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    revisions = run_git(project, "rev-list", "--all").decode("ascii").splitlines()
    failures = set()
    checked_blobs = {}

    def inspect(entries, label):
        for name, sha, kind in entries:
            if forbidden_path(name):
                failures.add((label, name, "禁传路径"))
                continue
            if kind != "blob":
                failures.add((label, name, "未审核的子模块"))
                continue
            if sha not in checked_blobs:
                raw = run_git(project, "cat-file", "blob", sha)
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("gbk", errors="replace")
                checked_blobs[sha] = bool(token_pattern.search(text) or any(pattern.search(text) for pattern in literals))
            if checked_blobs[sha]:
                failures.add((label, name, "敏感内容"))

    inspect(staged_entries(project), "index")
    for revision in revisions:
        inspect(tree_entries(project, revision), revision[:12])
        message = run_git(project, "show", "-s", "--format=%B", revision).decode("utf-8", errors="replace")
        if token_pattern.search(message) or any(pattern.search(message) for pattern in literals):
            failures.add((revision[:12], "<commit-message>", "敏感内容"))
    if failures:
        for label, name, reason in sorted(failures):
            print(f"BLOCK {label} {name}: {reason}", file=sys.stderr)
        raise RuntimeError("公开仓库检查失败；请修正内容或历史，不绕过检查。")
    return {"commits": len(revisions), "blobs": len(checked_blobs)}


def install(project, legacy_git=None, sensitive_project=None):
    project = Path(project).resolve()
    directory = Path(run_git(project, "rev-parse", "--absolute-git-dir").decode().strip())
    hooks = directory / "hooks"
    hooks.mkdir(exist_ok=True)
    for command in ("pre-commit", "pre-push"):
        path = hooks / command
        if path.exists() and "seer-repo-guard" not in path.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError("已有其他 Git hook，拒绝覆盖。")
        executable = Path(sys.executable).as_posix()
        script = (project / "tools" / "repo_guard.py").as_posix()
        content = "#!/bin/sh\n# seer-repo-guard\nexec " + shlex.quote(executable) + " " + shlex.quote(script) + " " + command + "\n"
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    config = directory / "seer-guard.json"
    settings = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    if legacy_git:
        settings["legacy_git"] = str(Path(legacy_git).resolve())
    if sensitive_project:
        settings["sensitive_project"] = str(Path(sensitive_project).resolve())
    config.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")


def main():
    # 终端里按控制台编码输出（中文正常）；管道或重定向时改用 UTF-8，
    # 否则检查结果里的中文会被其他程序按 UTF-8 解码成乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "install", "pre-commit", "pre-push"))
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--legacy-git", type=Path)
    parser.add_argument("--sensitive-project", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.project, args.legacy_git, args.sensitive_project)
            print("已安装本地提交和推送检查。")
        else:
            if args.command == "pre-push":
                sys.stdin.read()  # 消费 Git 的 ref 列表，检查所有本地历史
            report = check(args.project)
            if args.command == "check":
                print(f"PASS: {report['commits']} 个提交，{report['blobs']} 个文件对象；未输出敏感值。")
        return 0
    except (RuntimeError, OSError, UnicodeError, ValueError):
        print("操作失败：仓库检查或 hook 安装未完成；未输出敏感值。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
