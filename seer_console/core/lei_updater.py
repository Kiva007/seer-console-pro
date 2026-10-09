# -*- coding: utf-8 -*-
"""雷小伊蓝奏云自动更新下载器（纯 HTTP，无浏览器无 node 外部依赖）。

七步链路（2026-09 diag_lei_session.py 实测闭环，电信源 ~6.6MiB/s）：
  ① GET 分享页 https://lxy.lanzouj.com/b052mtlbg → 提取 fid/uid/puid/t/k
  ② POST filemoreajax.php → 文件列表 → 选「雷小伊脱机日常」最高版本条目
  ③ GET 文件页 → 阿里云 WAF acw_sc__v2 JS 挑战 → 纯 Python 求解（unsbox+hexXor）
  ④ GET iframe /fn?... → 提取 wp_sign/ajaxdata/file_param
  ⑤ POST ajaxfile.php downprocess → 下载域名+url（kd=1 电信 / kd=0 普通）
  ⑥ requests.Session（TLS 会话复用，ESA CDN 挑战要求）请求下载地址 → 302 → 真直链
  ⑦ 流式下载 .part → Content-Length/MZ 双校验 → 原子改名

来源选择（用户决策）：默认「电信下载」，失败备选「普通」源（kd=0、再备 &toolsdown 变体）。
幂等性：本地已有同名且完整（MZ + 大小匹配）的文件时跳过下载直接返回。
"""

import os
import re

SHARE_URL = "https://lxy.lanzouj.com/b052mtlbg"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Chrome 导航头集：ESA CDN 指纹校验要求（缺头集会返回「点击验证」人机页）
NAV_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8,"
              "application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# acw_sc__v2 hexXor 固定密钥（2026-09 与 node 权威输出比对一致）
_ACW_KEY = "3000176000856006061501533003690027800375"

DAILY_PREFIX = "雷小伊脱机日常"

# 蓝奏云默认域名（雷小伊分享页）；重聚巅峰分享页是另一域名（wwu.lanzouu.com），
# 由调用方通过 share_url 覆盖，所有域名硬编码均从该值派生
LANZOU_HOST = "https://lxy.lanzouj.com"


class LeiUpdateError(Exception):
    """下载链路失败（网络/解析/校验/版本比较不通过）。"""


class LeiUpdateCancelled(Exception):
    """外部取消（点全部关闭/单号关闭）。"""


def _host_of(share_url: str) -> str:
    """分享页 URL → scheme://netloc（缺省雷小伊域名）"""
    from urllib.parse import urlsplit
    u = urlsplit(share_url or "")
    if u.scheme and u.netloc:
        return f"{u.scheme}://{u.netloc}"
    return LANZOU_HOST


def solve_acw_sc_v2(script: str) -> str:
    """纯 Python 求解阿里云 acw_sc__v2 JS 挑战。

    输入：挑战页/挑战 JS 原文（含 arg1 十六进制串与混淆位置数组）。
    输出：cookie 值（不含 "acw_sc__v2=" 名字）。
    算法（从混淆 JS 逆向，与 node 执行权威输出实测一致）：
      arg1='97E84BA7…'（40 位），m=[0xf,0x23,…]（1..40 的排列，从原文提取）
      unsbox：q[z] = arg1[m[z]-1]
      hexXor：u 与固定 KEY 逐两位异或 → cookie 十六进制串
    """
    m_arg1 = re.search(r"arg1\s*=\s*'([0-9A-Fa-f]+)'", script)
    m_pos = re.search(r"\[((?:0x[0-9a-f]+\s*,\s*){19,}0x[0-9a-f]+)\]", script)
    if not m_arg1 or not m_pos:
        raise LeiUpdateError("WAF 挑战页解析失败（arg1/位置数组未找到）")
    arg1 = m_arg1.group(1)
    m = [int(x, 16) for x in m_pos.group(1).split(",")]
    q = [arg1[mv - 1] for mv in m if 0 < mv <= len(arg1)]
    u = "".join(q)
    key = _ACW_KEY
    v = "".join(f"{int(u[i:i + 2], 16) ^ int(key[i:i + 2], 16):02x}"
                for i in range(0, min(len(u), len(key)), 2))
    return v


def parse_size(text: str) -> int:
    """蓝奏云文件大小文本转字节数：'19.0 M' / '890.6 K' / '1.2 G'；无法解析返回 0。"""
    m = re.match(r"\s*([\d.]+)\s*([KMG]?)", text or "")
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    return int(num * mult)


def _is_complete_exe(path: str, expected_bytes: int = 0) -> bool:
    """判断本地文件是否为完整可用的 exe（复用已下载文件用）。
    判据：≥1MB（过滤下载残件）+ MZ 头 + （提供预期大小时）偏差 ≤5%。"""
    try:
        size = os.path.getsize(path)
        if size < 1024 * 1024:
            return False
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return False
        if expected_bytes and abs(size - expected_bytes) > expected_bytes * 0.05:
            return False
    except OSError:
        return False
    return True


def _extract_share_params(html: str) -> dict:
    """步骤①：从分享页 HTML 提取 filemoreajax 所需参数。
    ⚠️ t/k 的混淆变量名每页随机轮换（实测 2026-09-01 页面叫
    ib49br/_h2ldk，次日叫 ib6ehj/_h4qjc），不能写死名字：
    优先从 ajax data 块的 't':<名> 'k':<名> 引用反查 var 声明值，
    兜底按值形态提取（t=9~13位时间戳，k=32位hex，相邻声明）。"""
    pats = {
        "fid": r"'fid':(\d+)",
        "uid": r"'uid':'(\d+)'",
        "puid": r"'puid':'([^']+)'",
    }
    out = {}
    for name, pat in pats.items():
        m = re.search(pat, html)
        if not m:
            raise LeiUpdateError(f"分享页参数提取失败: {name}")
        out[name] = m.group(1)
    # 结构定位：ajax data 块 't':<var名>, 'k':<var名> → 反查 var <名> = '<值>'
    m = re.search(r"'t'\s*:\s*(\w+)\s*,\s*'k'\s*:\s*(\w+)\s*,", html)
    if m:
        t_m = re.search(rf"var\s+{re.escape(m.group(1))}\s*=\s*'([^']+)'", html)
        k_m = re.search(rf"var\s+{re.escape(m.group(2))}\s*=\s*'([^']+)'", html)
        if t_m and k_m:
            out["t"], out["k"] = t_m.group(1), k_m.group(1)
            return out
    # 兜底：值形态（相邻 var 声明：数字时间戳 + 32 位 hex）
    m = re.search(r"var\s+\w+\s*=\s*'(\d{9,13})'\s*;\s*"
                  r"var\s+\w+\s*=\s*'([0-9a-f]{32})'", html)
    if m:
        out["t"], out["k"] = m.group(1), m.group(2)
        return out
    raise LeiUpdateError("分享页参数提取失败: t/k（变量名已轮换，定位失败）")


def fetch_latest_exe(session, log=None, timeout: float = 30, share_url: str = "",
                     name_prefix: str = "", name_match=None) -> dict:
    """步骤①②：获取分享页中匹配筛选条件的最高版本条目。
    返回蓝奏云列表条目 dict（含 name_all/size/id）。session 为 requests.Session
    兼容对象（get/post/cookies），便于测试注入。
    share_url 非空=其他分享页（如重聚巅峰 wwu.lanzouu.com/s/cjdf）；
    name_match 非空=自定义文件名筛选（优先于 name_prefix 前缀匹配）。"""
    share_url = share_url or SHARE_URL
    host = _host_of(share_url)
    r = session.get(share_url, timeout=timeout)
    params = _extract_share_params(r.text)
    r = session.post(
        f"{host}/filemoreajax.php?file={params['fid']}",
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": share_url},
        data={"lx": 2, "fid": params["fid"], "uid": params["uid"],
              "puid": params["puid"], "pg": 1, "rep": "0",
              "t": params["t"], "k": params["k"], "up": 1},
        timeout=timeout,
    )
    j = r.json()
    if str(j.get("zt")) != "1":
        raise LeiUpdateError(f"文件列表接口失败: {str(j)[:120]}")
    if name_match is not None:
        entries = [it for it in (j.get("text") or [])
                   if name_match(str(it.get("name_all", "")))]
        desc = "自定义筛选"
    else:
        prefix = name_prefix or DAILY_PREFIX
        entries = [it for it in (j.get("text") or [])
                   if str(it.get("name_all", "")).startswith(prefix)]
        desc = prefix
    if not entries:
        raise LeiUpdateError(f"分享页未找到「{desc}」文件")
    from .version import extract_version, compare_version
    best = entries[0]
    for it in entries[1:]:
        if compare_version(extract_version(it["name_all"]) or "0",
                           extract_version(best["name_all"]) or "0") > 0:
            best = it
    return best


def fetch_latest_daily(session, log=None, timeout: float = 30) -> dict:
    """步骤①②（雷小伊专用包装）：获取分享页「雷小伊脱机日常」最高版本条目。"""
    return fetch_latest_exe(session, log=log, timeout=timeout)


def _resolve_download_params(session, file_url: str, log=None,
                             timeout: float = 30, referer: str = ""):
    """步骤③④：过 WAF 挑战后进入文件页，再取 iframe 下载页参数。
    返回 (wp_sign, ajaxdata, ajax_url, fn_url)。
    file_url 已含域名（各分享页域名不同），host 从其派生。
    ⚠️ ajaxfile.php 地址不同站点形态不同：lxy 是相对路径 '/ajaxfile.php?file=N'，
    重聚(wwu)是绝对地址 'https://apifile.woozooo.com/ajaxfile.php?file=N'——
    一律以页面给出的地址为准（绝对则原样用，相对则拼 file_url 的 host）。"""
    host = _host_of(file_url)
    r = session.get(file_url, headers={"Referer": referer or SHARE_URL},
                    timeout=timeout)
    tries = 0
    while "arg1=" in r.text and tries < 3:
        tries += 1
        val = solve_acw_sc_v2(r.text)
        session.cookies.set("acw_sc__v2", val)
        if log:
            log(f"WAF 挑战已求解（第{tries}次）")
        r = session.get(file_url, headers={"Referer": referer or SHARE_URL},
                        timeout=timeout)
    fhtml = r.text
    m_ifr = re.search(r'<iframe[^>]+src="([^"]+)"', fhtml)
    if not m_ifr:
        raise LeiUpdateError("文件页未找到下载 iframe")
    fn_url = host + m_ifr.group(1)
    r = session.get(fn_url, headers={"Referer": file_url}, timeout=timeout)
    fn_html = r.text
    wp_sign = re.search(r"var wp_sign = '([^']+)'", fn_html)
    ajaxdata = re.search(r"var ajaxdata = '([^']+)'", fn_html)
    # ajax 地址：可选绝对域名前缀（重聚 wwu 是 apifile.woozooo.com）；
    # lxy 是带前导斜杠的相对路径，两种形态都要兼容
    m_file = re.search(r"url\s*:\s*'(https?://[^'\"]+/)?/?"
                       r"ajaxfile\.php\?file=(\d+)'", fn_html)
    if not (wp_sign and ajaxdata and m_file):
        raise LeiUpdateError("下载页参数解析失败（wp_sign/ajaxdata/file）")
    ajax_host = m_file.group(1).rstrip("/") if m_file.group(1) else host
    ajax_url = f"{ajax_host}/ajaxfile.php?file={m_file.group(2)}"
    return wp_sign.group(1), ajaxdata.group(1), ajax_url, fn_url


def _downprocess(session, ajax_url: str, wp_sign: str, ajaxdata: str,
                 kd: int, fn_url: str, timeout: float = 30) -> str:
    """步骤⑤：ajaxfile.php downprocess → 组装下载地址。
    kd=1 用响应 dom（电信）；kd=0 强制普通源域名。
    ajax_url 用页面给出的绝对/相对地址（不同站点宿主不同，见
    _resolve_download_params）。"""
    r = session.post(
        ajax_url,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": fn_url},
        data={"action": "downprocess", "websignkey": ajaxdata,
              "signs": ajaxdata, "sign": wp_sign, "websign": "",
              "kd": kd, "ves": 1},
        timeout=timeout,
    )
    j = r.json()
    if str(j.get("zt")) != "1":
        raise LeiUpdateError(f"downprocess 失败: {str(j)[:120]}")
    dom = j["dom"] if kd == 1 else "https://slssctm.dmpdmp.com"
    url = j.get("url") or ""
    if not url:
        raise LeiUpdateError("downprocess 未返回下载 url")
    return f"{dom}/file/{url}"


def _download_from(session, url: str, dest_path: str, log=None,
                   cancel_check=None, timeout: float = 30) -> int:
    """步骤⑥⑦：从下载地址流式下载到 dest_path（处理 ESA 302 挑战链）。
    返回写入字节数。要求内容以 MZ 开头（防验证页/HTML）。
    ⚠️ 下载 CDN 域也可能回 acw_sc__v2 挑战页（实测 wwu 下载域 200+arg1
    而非 302）：首块识别为挑战页时整页求解 cookie 后重试（与文件页同法）。"""
    part = dest_path + ".part"
    cur = url
    challenge_tries = 0
    try:
        # 循环预算覆盖：挑战求解重试 + 302 重定向链（实测电信源 1 次挑战+
        # 若干跳；12 次足够，challenge_tries 另行封顶 3）
        for _ in range(12):
            if cancel_check and cancel_check():
                raise LeiUpdateCancelled()
            with session.get(cur, headers=dict(NAV_HEADERS), stream=True,
                             allow_redirects=False, timeout=(15, 60)) as r:
                if r.status_code in (301, 302, 303, 307):
                    loc = r.headers.get("Location")
                    if not loc:
                        # 同 URL 挑战：响应已更新会话 cookie，重访即可（实测）
                        continue
                    cur = loc
                    continue
                if r.status_code != 200:
                    raise LeiUpdateError(f"下载地址返回 HTTP {r.status_code}")
                # 先取第一块判类型：CDN 可能回 acW 挑战页（非 302 而是 200）
                chunks = r.iter_content(65536)
                first_chunk = next(chunks, None) or b""
                if first_chunk[:6] in (b"<html>", b"<!DOCT") and \
                        b"arg1=" in first_chunk:
                    challenge_tries += 1
                    if challenge_tries > 3:
                        raise LeiUpdateError("CDN WAF 挑战次数过多")
                    page = session.get(cur, headers=dict(NAV_HEADERS),
                                       timeout=timeout)
                    page.encoding = "utf-8"
                    session.cookies.set("acw_sc__v2",
                                        solve_acw_sc_v2(page.text))
                    if log:
                        log(f"CDN WAF 挑战已求解（第{challenge_tries}次），"
                            f"重新下载")
                    break  # 未写盘，跳出后按外层循环重试
                if not first_chunk or first_chunk[:2] != b"MZ":
                    body = first_chunk[:120]
                    raise LeiUpdateError(
                        f"下载内容非 exe（可能触发验证页）: {body!r}")
                total = int(r.headers.get("Content-Length") or 0)
                got = len(first_chunk)
                last_log = 0
                with open(part, "wb") as f:
                    f.write(first_chunk)
                    for chunk in chunks:
                        if cancel_check and cancel_check():
                            raise LeiUpdateCancelled()
                        f.write(chunk)
                        got += len(chunk)
                        if log and got - last_log >= 4 * 1024 * 1024:
                            last_log = got
                            if total:
                                log(f"已下载 {got / 1048576:.1f} / "
                                    f"{total / 1048576:.1f} MiB")
                            else:
                                log(f"已下载 {got / 1048576:.1f} MiB")
                if total and got != total:
                    raise LeiUpdateError(f"下载不完整: {got}/{total} 字节")
                os.replace(part, dest_path)
                return got
        raise LeiUpdateError("下载地址重定向/挑战次数过多")
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def download_from_share(dest_dir: str, share_url: str, name_prefix: str = "",
                        current_path: str = "", log=None, cancel_check=None,
                        timeout: float = 30, session=None,
                        name_match=None) -> str:
    """通用主入口：从蓝奏云分享页下载匹配项到 dest_dir，返回最终落盘路径。

    share_url：分享页地址（雷小伊 SHARE_URL / 重聚巅峰弹窗给出的链接均可，
    域名从中派生，七步链路全复用）；name_prefix：文件名前缀筛选；
    name_match：自定义筛选 callable（优先于 name_prefix）。
    current_path 非空时先做版本比较：分享页版本不高于当前 → 抛 LeiUpdateError
    （防倒退与循环更新）。
    本地已有同名完整文件（MZ + 大小匹配）→ 跳过下载直接返回。
    来源顺序：电信(kd=1) → 普通(kd=0) → 普通+&toolsdown。
    cancel_check() 返回 True 时抛 LeiUpdateCancelled（调用方清理状态）。
    session 可注入（requests.Session 兼容），缺省内部新建。
    """
    os.makedirs(dest_dir, exist_ok=True)
    if session is None:
        import requests
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})

    entry = fetch_latest_exe(session, log=log, timeout=timeout,
                             share_url=share_url, name_prefix=name_prefix,
                             name_match=name_match)
    name = str(entry["name_all"])
    if log:
        log(f"分享页最新: {name}（{entry.get('size', '')}）")
    dest_path = os.path.join(dest_dir, name)

    # 版本比较：分享页不高于当前版本 → 不下载（防循环更新）
    if current_path:
        from .version import extract_version, compare_version
        cur = extract_version(os.path.basename(current_path))
        new = extract_version(name)
        if cur and new and compare_version(new, cur) <= 0:
            raise LeiUpdateError(
                f"分享页版本 {new} 不高于当前 {cur}，跳过下载")

    # 本地已有完整文件 → 直接复用
    expected = parse_size(str(entry.get("size", "")))
    if os.path.isfile(dest_path) and _is_complete_exe(dest_path, expected):
        if log:
            log(f"本地已存在完整文件，跳过下载: {name}")
        return dest_path

    host = _host_of(share_url)

    # ③④ 文件页参数（含 WAF 挑战）
    file_url = f"{host}/{entry['id']}"
    wp_sign, ajaxdata, ajax_url, fn_url = _resolve_download_params(
        session, file_url, log=log, timeout=timeout, referer=share_url)

    # 来源顺序：电信 → 普通 → 普通(&toolsdown 变体)
    attempts = [("电信", 1, ""), ("普通", 0, ""), ("普通备用", 0, "&toolsdown")]
    last_err: Exception = LeiUpdateError("未尝试下载")
    for label, kd, suffix in attempts:
        if cancel_check and cancel_check():
            raise LeiUpdateCancelled()
        try:
            dl_url = _downprocess(session, ajax_url, wp_sign, ajaxdata,
                                  kd, fn_url, timeout=timeout)
            if suffix:
                dl_url += suffix
            if log:
                log(f"开始下载（{label}源）…")
            got = _download_from(session, dl_url, dest_path,
                                 log=log, cancel_check=cancel_check,
                                 timeout=timeout)
            if log:
                log(f"下载完成: {name}（{got / 1048576:.1f} MiB）")
            return dest_path
        except LeiUpdateCancelled:
            raise
        except Exception as e:
            last_err = e
            if log:
                log(f"{label}源下载失败: {e}")
            continue
    raise LeiUpdateError(f"全部下载源失败: {last_err}")


def download_latest_lei(dest_dir: str, current_path: str = "", log=None,
                        cancel_check=None, timeout: float = 30,
                        session=None) -> str:
    """雷小伊专用入口（薄包装）：从 SHARE_URL 下载最新「雷小伊脱机日常」。"""
    return download_from_share(dest_dir, SHARE_URL, DAILY_PREFIX,
                               current_path=current_path, log=log,
                               cancel_check=cancel_check, timeout=timeout,
                               session=session)
