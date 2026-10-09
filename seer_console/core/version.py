"""客户端版本工具：文件名版本提取/比较/目录最新版本扫描

从 main_window 的版本管理逻辑抽出，供 UI 与引擎（XM 更新流程）共用。
"""

import os
import re
import glob


def extract_base_name(filename: str) -> str:
    """提取文件名的基础部分（去掉版本号）"""
    name = os.path.splitext(filename)[0]

    # 方案一：优先处理带【】的版本号
    m = re.search(r"【", name)
    if m:
        return name[:m.start()]

    # 方案二：从右往左找版本号分隔符
    separators = " vVerTt版【】()（）"
    for i in range(len(name) - 1, -1, -1):
        if name[i] in separators:
            # 检查前面是否数字
            if i > 0 and name[i - 1].isdigit():
                return name[:i]
            # 也可能是版本号前空格
            if i > 0 and name[i] == " ":
                return name[:i]

    return name


def extract_version(filename: str) -> str:
    """从文件名提取版本号，如 2.0.16, 1.8.12"""
    m = re.search(r"(?<=[^0-9.])(\d+\.)+\d+", filename)
    if m:
        return m.group()
    return ""


def compare_version(a: str, b: str) -> int:
    """比较两个版本号：a>b返回1，a<b返回-1，相等返回0"""
    parts_a = [int(x) for x in a.split(".")]
    parts_b = [int(x) for x in b.split(".")]

    max_len = max(len(parts_a), len(parts_b))
    for i in range(max_len):
        va = parts_a[i] if i < len(parts_a) else 0
        vb = parts_b[i] if i < len(parts_b) else 0
        if va > vb:
            return 1
        if va < vb:
            return -1
    return 0


def compare_xrym_version(a: str, b: str) -> int:
    """比较 XM（XryM）软件版本号——XM 作者的版本号习惯与其他软件不同：
    X.X.XX 的末段两位数表示两个子级（2.5.21 = 2.5.2.1），新出的 2.5.3 应大于旧版 2.5.21。
    规则：末段 ≥10 时拆成两段（X.X.XX → X.X.X.X）后逐段比较；只用于 XM，不改变通用
    compare_version 行为。a>b返回1，a<b返回-1，相等返回0"""
    def split_parts(v: str) -> list[int]:
        parts = [int(x) for x in v.split(".")]
        if len(parts) >= 2 and parts[-1] >= 10:
            tail = parts[-1]
            parts[-1:] = [tail // 10, tail % 10]
        return parts

    pa, pb = split_parts(a), split_parts(b)
    max_len = max(len(pa), len(pb))
    for i in range(max_len):
        va = pa[i] if i < len(pa) else 0
        vb = pb[i] if i < len(pb) else 0
        if va > vb:
            return 1
        if va < vb:
            return -1
    return 0


def get_latest_version_path(full_path: str, xrym_style: bool = False) -> str:
    """获取目录下最新版本的文件路径（同前缀 exe 中版本号最大者；单文件时返回原路径）。
    xrym_style=True：用 XM 专属版本规则比较（X.X.XX 末段两位数 → X.X.X.X，仅 XM 使用）"""
    directory = os.path.dirname(full_path)
    basename = os.path.splitext(os.path.basename(full_path))[0]

    base = extract_base_name(basename)

    pattern = os.path.join(directory, base + "*")
    candidates = []
    for f in glob.glob(pattern):
        if f.lower().endswith(".exe"):
            candidates.append(f)

    if len(candidates) <= 1:
        return full_path

    cmp = compare_xrym_version if xrym_style else compare_version
    current_ver = extract_version(os.path.basename(full_path))
    best_path = full_path

    for f in candidates:
        # XM 专用：过滤下载中断的残件（<1MB，实测中断下载只剩 ~348KB；完整 XM exe 约 20MB）
        if xrym_style:
            try:
                if os.path.getsize(f) < 1024 * 1024:
                    continue
            except OSError:
                continue
        ver = extract_version(os.path.basename(f))
        if ver and current_ver and cmp(ver, current_ver) > 0:
            current_ver = ver
            best_path = f
        elif ver and not current_ver:
            current_ver = ver
            best_path = f

    return best_path
