# -*- coding: utf-8 -*-
"""重聚巅峰更新弹窗链接提取 + 蓝奏云网页兜底下载。

背景（2026-09-25 用户需求）：重聚巅峰客户端自带更新失败时（实测 2.8.5：
「下载模块分离，以后下载失效后等修复即可下载，无需重新下载新版脱机」），
自动改用更新弹窗内文本给出的「手动下载」蓝奏云链接下载新版。

2026-09-25 diag_peak_link2.py 实测：更新弹窗（标题「检测到新版本 Vx.y.z」）
id=270 Edit（WM_GETTEXT 可读，GetWindowText 读不到）内含完整更新说明：
    更新内容：重聚脱机巅峰 V2.8.5
    …
    手动下载：https://wwu.lanzouu.com/s/cjdf
    百度网盘: https://pan.baidu.com/s/1LK7t3UelAb8BspCPMW-vYQ?pwd=4399
本模块只做两件重聚专属的事：
  ① extract_update_link：从弹窗文本提取蓝奏云分享链接（忽略百度网盘等
     不受支持的源）；
  ② download_peak_from_link：按「巅峰」筛选分享页文件下载到 nanliu 目录。
蓝奏云七步链路（WAF 求解/Session/NAV 头/下载校验/来源重试）全部复用
lei_updater.download_from_share，不重复实现。
"""

import re

from .lei_updater import (LeiUpdateCancelled, LeiUpdateError,
                          download_from_share)

# 实测默认分享页（2026-09-25 弹窗文本）；仅作日志/手动提示兜底，
# 正常一律以弹窗实时链接为准（链接可能随版本变化）
PEAK_SHARE_URL = "https://wwu.lanzouu.com/s/cjdf"

# 弹窗文本里的 URL：按空白/中文标点切分
_LINK_RE = re.compile(r"https?://[^\s，。；、！）)」\]》]+", re.I)


class CjUpdateError(Exception):
    """重聚巅峰网页兜底失败（无链接/筛选无结果/下载链路失败）。"""


def extract_update_link(text: str) -> str:
    """从更新弹窗文本提取蓝奏云分享链接（只认 lanzou 域名）。

    弹窗文本可能同时含蓝奏云「手动下载」与百度网盘链接；自动下载只支持
    蓝奏云（百度网盘需登录/验证码，不支持），因此忽略非 lanzou 链接。
    找不到返回空串。
    """
    for m in _LINK_RE.finditer(text or ""):
        url = m.group(0)
        if "lanzou" in url.lower():
            return url
    return ""


def is_peak_exe(name: str) -> bool:
    """分享页文件筛选：重聚脱机巅峰 exe（排除日常/登录器等）。"""
    return "巅峰" in name and "日常" not in name and "登录器" not in name


def download_peak_from_link(dest_dir: str, link: str, current_path: str = "",
                            log=None, cancel_check=None, timeout: float = 30,
                            session=None) -> str:
    """从更新弹窗给出的蓝奏云分享页下载最新重聚脱机巅峰到 dest_dir。

    link：extract_update_link 提取的分享页地址（wwu.lanzouu.com/s/cjdf）；
    current_path：当前 exe 路径（版本门：分享页不高于当前则抛错，防循环）。
    返回落盘路径。异常：CjUpdateError/LeiUpdateError/LeiUpdateCancelled。
    """
    if not link:
        raise CjUpdateError("弹窗文本中未找到蓝奏云分享链接")
    return download_from_share(dest_dir, link, name_match=is_peak_exe,
                               current_path=current_path, log=log,
                               cancel_check=cancel_check, timeout=timeout,
                               session=session)
