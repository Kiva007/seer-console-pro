# -*- coding: utf-8 -*-
"""重聚巅峰网页兜底下载单元测试（2026-09：自带更新失败 → 弹窗链接 → 蓝奏云）。

覆盖：
  1. chong_ju_updater.extract_update_link：从更新弹窗文本提取蓝奏云链接
     （真实双链接文本→只取蓝奏云、仅百度网盘→空、中文标点截断、域名变体）
  2. chong_ju_updater.is_peak_exe：巅峰 exe 名称判定（混列表选巅峰）
  3. chong_ju_updater.download_peak_from_link：分享页→列表→(WAF)→文件页→
     iframe→ajaxfile→下载 七步链路（host 从 share_url 派生，如 wwu.lanzouu.com），
     选「重聚脱机巅峰」最高版本、版本门、无巅峰文件报错
  4. engine._read_update_popup_text：点更新前预读弹窗全文（链接藏在 Edit 里）
  5. engine._handle_update：巅峰落盘判定（自带更新成功→不走兜底；
     未落盘新版本→走兜底；兜底失败返回 False）
  6. engine._peak_web_update_fallback：无链接/取消/下载失败/成功四分支

运行（无 pytest 依赖，同 test_peak_update_cancel.py 风格）：
    python seer_console/tests/test_chongju_web_update.py
"""

import os
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.account import AccountManager
from seer_console.core.engine import AutomationEngine, ClientType
import seer_console.core.chong_ju_updater as cju
import seer_console.core.engine as eng_mod
import seer_console.core.lei_updater as lu

_CHECKS = {"n": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    _CHECKS["n"] += 1
    if cond:
        print(f"  [PASS] {name}")
    else:
        _CHECKS["fail"] += 1
        print(f"  [FAIL] {name}  {detail}")


# ============================================================
# FakeSession（蓝奏云七步路由，host 可配——重聚实测是 wwu.lanzouu.com）
# ============================================================

class _FakeCookies:
    def __init__(self):
        self.store: dict = {}

    def set(self, name, value):
        self.store[name] = value


class _FakeResp:
    def __init__(self, status_code=200, text="", json_data=None, body=b"",
                 headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._json

    def iter_content(self, n):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# 分享页：t/k 变量名沿用 09-01 诊断时的形态（结构定位测的是引用块）
SHARE_HTML = ("var ck='x'; 'fid':777,'uid':'99','puid':'abc';"
              "var ib49br='1112345678';"
              "var _h2ldk='a0d77add4f7112c25255e23a77ca7e38';"
              "data:{'lx':2,'fid':777,'t':ib49br,'k':_h2ldk,'up':1}")
FILE_PAGE = '<iframe src="/fn?ABC123">'
FN_PAGE = ("var wp_sign = 'S1'; var ajaxdata = 'A1'; "
           "url: '/ajaxfile.php?file=88'")
AJAX_JSON = {"zt": 1, "dom": "https://developer2.lanrar.com", "url": "?DLKEY"}
DL_BODY = b"MZ" + b"Y" * 4096
PEAK_LINK = "https://wwu.lanzouu.com/s/cjdf"

# 文件列表（测试可整体替换）：默认混合巅峰/日常/雷小伊三个文件
LIST_JSON = {"zt": 1, "text": [
    {"id": "f11", "name_all": "重聚脱机巅峰【2.8.5】.exe", "size": "19.0 M"},
    {"id": "f12", "name_all": "重聚脱机日常【2.3.0】.exe", "size": "9.0 M"},
    {"id": "f13", "name_all": "雷小伊脱机日常 Beta 1.9.0.exe", "size": "1.0 M"},
]}


class HostSession:
    """按 URL 规则路由的 requests.Session 桩（host 取自 PEAK_LINK）。
    默认预置 acw cookie 跳过 WAF（WAF 求解由 lei 套件覆盖）。"""

    def __init__(self):
        self.cookies = _FakeCookies()
        self.cookies.set("acw_sc__v2", "seeded")  # 预置：文件页直接返回
        self.gets: list = []
        self.posts: list = []

    def get(self, url, **kw):
        self.gets.append(url)
        if url == PEAK_LINK:
            return _FakeResp(text=SHARE_HTML)
        if url == "https://wwu.lanzouu.com/f11":
            return _FakeResp(text=FILE_PAGE)
        if url == "https://wwu.lanzouu.com/fn?ABC123":
            return _FakeResp(text=FN_PAGE)
        if url.startswith("https://developer2.lanrar.com/file/") or \
                url.startswith("https://slssctm.dmpdmp.com/file/"):
            return _FakeResp(body=DL_BODY,
                             headers={"Content-Length": str(len(DL_BODY))})
        return _FakeResp(status_code=404, text="not found")

    def post(self, url, **kw):
        self.posts.append(url)
        if "filemoreajax.php" in url:
            return _FakeResp(json_data=LIST_JSON)
        if "ajaxfile.php" in url:
            return _FakeResp(json_data=AJAX_JSON)
        return _FakeResp(status_code=404, text="not found")


# ============================================================
# 1. extract_update_link
# ============================================================

# diag_peak_link.py 实测 2.8.5 弹窗 id=270 Edit 真实文本
REAL_TEXT = ("更新内容：重聚脱机巅峰 V2.8.5\r\n"
             "2.8.5【巅峰本次可能要手动下载】\r\n"
             "1.修复部分win7用户插件和指令无法下载问题\r\n\r\n"
             "手动下载：https://wwu.lanzouu.com/s/cjdf\r\n"
             "百度网盘: https://pan.baidu.com/s/1LK7t3UelAb8BspCPMW-vYQ?pwd=4399")


def test_extract_update_link() -> None:
    print("\n[1] extract_update_link（弹窗文本 → 蓝奏云链接）")
    link = cju.extract_update_link(REAL_TEXT)
    check("1a 真实双链接文本 → 只取蓝奏云",
          link == "https://wwu.lanzouu.com/s/cjdf", link)
    check("1b 不含百度网盘", "pan.baidu" not in link)

    check("1c 仅百度网盘 → 空串",
          cju.extract_update_link(
              "百度网盘: https://pan.baidu.com/s/1abc?pwd=123") == "")

    check("1d 空文本 → 空串", cju.extract_update_link("") == "")

    check("1e URL 后跟中文标点 → 干净截断",
          cju.extract_update_link("手动下载：https://wwu.lanzouu.com/s/cjdf，打开即可")
          == "https://wwu.lanzouu.com/s/cjdf")

    for dom in ("lanzouj.com", "lanzouu.com", "lanzouo.com", "lanzouw.com"):
        u = f"https://wwu.{dom}/s/ab12"
        check(f"1f 域名变体 {dom} 命中",
              cju.extract_update_link(f"下载:{u}") == u, u)

    check("1g 多个蓝奏云链接 → 取第一个",
          cju.extract_update_link(
              "https://a.lanzouj.com/s/x1 https://b.lanzouj.com/s/x2")
          == "https://a.lanzouj.com/s/x1")

    check("1h 无 http 前缀 → 空串",
          cju.extract_update_link("下载:wwu.lanzouu.com/s/cjdf") == "")


# ============================================================
# 2. is_peak_exe
# ============================================================

def test_is_peak_exe() -> None:
    print("\n[2] is_peak_exe（巅峰 exe 名称判定）")
    check("2a 巅峰 exe → True",
          cju.is_peak_exe("重聚脱机巅峰【2.8.5】.exe"))
    check("2b 日常 exe → False",
          not cju.is_peak_exe("重聚脱机日常【2.3.0】.exe"))
    check("2c 雷小伊登录器 → False",
          not cju.is_peak_exe("雷小伊登录器 2.0.17.exe"))
    check("2d 雷小伊日常 → False",
          not cju.is_peak_exe("雷小伊脱机日常 Beta 1.9.0.exe"))


# ============================================================
# 3. download_peak_from_link（七步链路，host 从链接派生）
# ============================================================

def test_download_peak_from_link() -> None:
    print("\n[3] download_peak_from_link（wwu 分享页全链路）")
    tmpdir = tempfile.mkdtemp(prefix="cj_web_")
    cur = os.path.join(tmpdir, "重聚脱机巅峰【2.8.4】.exe")
    logs: list = []

    s = HostSession()
    path = cju.download_peak_from_link(tmpdir, PEAK_LINK, current_path=cur,
                                       log=logs.append, session=s)
    name = "重聚脱机巅峰【2.8.5】.exe"
    check("3a 选中巅峰最高版本并落盘",
          os.path.basename(path) == name
          and os.path.isfile(path)
          and open(path, "rb").read(2) == b"MZ",
          f"path={path}")
    check("3b 分享页 GET 用的是弹窗链接",
          s.gets[0] == PEAK_LINK, str(s.gets[:2]))
    check("3c host 从链接派生（文件页/iframe/下载都打 wwu 或 CDN）",
          any(u == "https://wwu.lanzouu.com/f11" for u in s.gets)
          and any("/fn?" in u for u in s.gets), str(s.gets))
    check("3d 提交了列表接口", any("filemoreajax" in u for u in s.posts))
    check("3e 电信源一次成功",
          any("developer2.lanrar.com" in u for u in s.gets)
          and not any("slssctm" in u for u in s.gets))

    # 3f 版本门：分享页版本不高于当前 → 抛错（不触发下载页请求）
    cur_same = os.path.join(tmpdir, name)
    os.remove(path)
    s2 = HostSession()
    try:
        cju.download_peak_from_link(tmpdir, PEAK_LINK, current_path=cur_same,
                                    session=s2)
        check("3f 版本不升抛错", False, "未抛出")
    except lu.LeiUpdateError as e:
        check("3f 版本不升抛错", "不高于当前" in str(e), str(e))
    check("3f2 未触发下载页请求",
          not any("/f11" in u or "/fn?" in u for u in s2.gets))

    # 3g 列表里只有日常/雷小伊 → 报错
    global LIST_JSON
    orig_list = LIST_JSON
    LIST_JSON = {"zt": 1, "text": [
        {"id": "f12", "name_all": "重聚脱机日常【2.3.0】.exe", "size": "9.0 M"},
    ]}
    try:
        s3 = HostSession()
        try:
            cju.download_peak_from_link(tmpdir, PEAK_LINK, current_path=cur,
                                        session=s3)
            check("3g 无巅峰文件抛错", False, "未抛出")
        except (cju.CjUpdateError, lu.LeiUpdateError):
            check("3g 无巅峰文件抛错", True)
    finally:
        LIST_JSON = orig_list

    # 清理
    for f in os.listdir(tmpdir):
        os.unlink(os.path.join(tmpdir, f))
    os.rmdir(tmpdir)


# ============================================================
# engine 测试基建（_FakeConfig/_FakePM/make_engine）
# ============================================================

class _FakeConfig:
    def __init__(self):
        self.peak_path = r"C:\dir\重聚脱机巅峰【2.8.4】.exe"
        self.saved = 0

    def get_all_window_config(self) -> dict:
        return {}

    def get_seal_names(self) -> dict:
        return {}

    def get_current_peak_path(self) -> str:
        return self.peak_path

    def save(self) -> None:
        self.saved += 1


class _FakePM:
    def __init__(self):
        self.kills: list = []
        self.launches: list = []

    def kill(self, gw) -> None:
        self.kills.append(gw)

    def launch(self, exe_path: str):
        self.launches.append(exe_path)
        return SimpleNamespace(process_id=1234, process=object(),
                               login_cancelled=threading.Event(),
                               login_finished=threading.Event(),
                               login_thread=None, window_handle=0,
                               output_handle=0, timed_out=False)


def make_engine():
    ledger = os.path.join(tempfile.mkdtemp(prefix="cj_led_"), "ledger.txt")
    open(ledger, "w", encoding="utf-8").write("")
    am = AccountManager(ledger)
    am.load()
    logs: list = []
    engine = AutomationEngine(am, _FakeConfig(), _FakePM(), None,
                              log_callback=logs.append)
    return engine, am, logs, ledger


def _patch_handle_update_env(engine, landed: bool):
    """给 _handle_update 巅峰分支搭桩，返回 (clicked, persists, loads)。"""
    clicked: list = []
    persists: list = []
    loads: list = []
    cur = engine._config_manager.get_current_peak_path()
    new_path = r"C:\dir\重聚脱机巅峰【2.8.5】.exe"
    engine._config_vals = {31: 220}
    engine._load_config = lambda: loads.append(1)
    engine._kill_orphan_peak_processes = lambda: None
    engine._persist_peak_path_after_update = lambda: persists.append(True)
    eng_mod.get_child_windows = lambda h: [900, 901]
    eng_mod.get_control_id = lambda h: 220
    eng_mod.post_click = clicked.append
    eng_mod.external_edit_get_content = lambda h: (
        "更新内容：重聚脱机巅峰 V2.8.5\r\n手动下载：https://wwu.lanzouu.com/s/cjdf"
    ) if h == 901 else "立即更新"
    eng_mod.WindowFinder.is_window_valid = staticmethod(
        lambda h: False)  # 弹窗已销毁
    eng_mod.get_latest_version_path = lambda p: (
        new_path if landed else cur)
    return clicked, persists, loads


def _restore_handle_update_env(orig):
    (eng_mod.get_child_windows, eng_mod.get_control_id,
     eng_mod.post_click, eng_mod.external_edit_get_content,
     eng_mod.WindowFinder.is_window_valid,
     eng_mod.get_latest_version_path) = orig


_ORIG_FNS = (eng_mod.get_child_windows, eng_mod.get_control_id,
             eng_mod.post_click, eng_mod.external_edit_get_content,
             eng_mod.WindowFinder.is_window_valid,
             eng_mod.get_latest_version_path)


# ============================================================
# 4. _read_update_popup_text
# ============================================================

def test_read_update_popup_text() -> None:
    print("\n[4] engine._read_update_popup_text（预读弹窗全文）")
    engine, _, _, ledger = make_engine()
    try:
        eng_mod.get_child_windows = lambda h: [901, 902]
        eng_mod.external_edit_get_content = lambda h: (
            "更新内容：重聚脱机巅峰 V2.8.5\r\n手动下载：https://wwu.lanzouu.com/s/cjdf"
            if h == 901 else "")
        text = engine._read_update_popup_text(100)
        check("4a 读取到 Edit 全文（含链接）",
              text == "更新内容：重聚脱机巅峰 V2.8.5\r\n手动下载：https://wwu.lanzouu.com/s/cjdf",
              text)
        eng_mod.get_child_windows = lambda h: []
        check("4b 无子控件 → 空串",
              engine._read_update_popup_text(100) == "")
    finally:
        eng_mod.get_child_windows = _ORIG_FNS[0]
        eng_mod.external_edit_get_content = _ORIG_FNS[3]
        os.unlink(ledger)


# ============================================================
# 5. _handle_update：巅峰落盘判定与兜底分流
# ============================================================

def test_handle_update_landing() -> None:
    print("\n[5] engine._handle_update（落盘判定 → 是否走兜底）")

    # 5a 自带更新成功（目录落盘新版本）→ 不走兜底
    engine, _, logs, ledger = make_engine()
    try:
        clicked, persists, loads = _patch_handle_update_env(engine, landed=True)
        fallback_calls: list = []
        orig_dl = cju.download_peak_from_link
        cju.download_peak_from_link = (
            lambda *a, **k: fallback_calls.append(1) or "")
        try:
            ok = engine._handle_update(100, ClientType.CHONG_JU_PEAK, pid=1,
                                       i=0, gw=SimpleNamespace())
        finally:
            cju.download_peak_from_link = orig_dl
        check("5a 落盘新版本 → 返回 True", ok is True)
        check("5a2 写了配置", persists == [True], f"persists={persists}")
        check("5a3 日志「巅峰更新完成」",
              any("巅峰更新完成" in m for m in logs), f"logs={logs}")
        check("5a4 未调用网页兜底", fallback_calls == [],
              f"fallback_calls={fallback_calls}")
    finally:
        _restore_handle_update_env(_ORIG_FNS)
        os.unlink(ledger)

    # 5b 未落盘新版本 → 走兜底，兜底成功 → True
    engine2, _, logs2, ledger2 = make_engine()
    try:
        clicked2, persists2, loads2 = _patch_handle_update_env(engine2, landed=False)
        fb: list = []
        orig_dl = cju.download_peak_from_link
        cju.download_peak_from_link = (
            lambda *a, **k: fb.append((a, k)) or r"C:\dir\重聚脱机巅峰【2.8.5】.exe")
        try:
            ok2 = engine2._handle_update(100, ClientType.CHONG_JU_PEAK, pid=1,
                                         i=0, gw=SimpleNamespace())
        finally:
            cju.download_peak_from_link = orig_dl
        check("5b 未落盘 → 日志「自带更新失败」",
              any("自带更新失败" in m for m in logs2), f"logs2={logs2}")
        check("5b2 调用了网页兜底", len(fb) == 1, f"fb={fb}")
        check("5b3 兜底成功 → 返回 True", ok2 is True)
        check("5b4 日志「巅峰更新完成（网页下载）」",
              any("网页下载" in m for m in logs2), f"logs2={logs2}")
    finally:
        _restore_handle_update_env(_ORIG_FNS)
        os.unlink(ledger2)

    # 5c 兜底失败 → 返回 False（调用方置 _update_flag=0 放行/降级）
    engine3, _, _, ledger3 = make_engine()
    try:
        _patch_handle_update_env(engine3, landed=False)
        orig_dl = cju.download_peak_from_link
        cju.download_peak_from_link = (
            lambda *a, **k: (_ for _ in ()).throw(
                lu.LeiUpdateError("下载失败")))
        try:
            ok3 = engine3._handle_update(100, ClientType.CHONG_JU_PEAK, pid=1,
                                         i=0, gw=SimpleNamespace())
        finally:
            cju.download_peak_from_link = orig_dl
        check("5c 兜底失败 → 返回 False", ok3 is False)
    finally:
        _restore_handle_update_env(_ORIG_FNS)
        os.unlink(ledger3)


# ============================================================
# 6. _peak_web_update_fallback 四分支
# ============================================================

def test_peak_web_fallback() -> None:
    print("\n[6] engine._peak_web_update_fallback（无链接/取消/失败/成功）")
    engine, _, logs, ledger = make_engine()
    try:
        # 6a 无链接 → False
        ok = engine._peak_web_update_fallback("更新内容：V2.8.5 已发布",
                                              r"C:\dir\重聚脱机巅峰【2.8.4】.exe")
        check("6a 无链接 → False", ok is False)
        check("6a2 日志提示未找到链接",
              any("未找到蓝奏云下载链接" in m for m in logs), f"logs={logs}")

        # 6b 取消 → False
        logs.clear()
        orig_dl = cju.download_peak_from_link
        cju.download_peak_from_link = (
            lambda *a, **k: (_ for _ in ()).throw(lu.LeiUpdateCancelled()))
        try:
            ok2 = engine._peak_web_update_fallback(
                "手动下载：https://wwu.lanzouu.com/s/cjdf",
                r"C:\dir\重聚脱机巅峰【2.8.4】.exe",
                i=0, gw=SimpleNamespace())
        finally:
            cju.download_peak_from_link = orig_dl
        check("6b 取消 → False", ok2 is False)
        check("6b2 日志「网页兜底已取消」",
              any("已取消" in m for m in logs), f"logs={logs}")

        # 6c 下载失败 → False + 手动指引
        logs.clear()
        cju.download_peak_from_link = (
            lambda *a, **k: (_ for _ in ()).throw(
                lu.LeiUpdateError("全部源失败")))
        try:
            ok3 = engine._peak_web_update_fallback(
                "手动下载：https://wwu.lanzouu.com/s/cjdf",
                r"C:\dir\重聚脱机巅峰【2.8.4】.exe")
        finally:
            cju.download_peak_from_link = orig_dl
        check("6c 失败 → False", ok3 is False)
        check("6c2 日志含「可手动下载」+链接",
              any("可手动下载" in m and "cjdf" in m for m in logs),
              f"logs={logs}")

        # 6d 成功 → True + 重载配置 + 写回
        logs.clear()
        loads6: list = []
        persists6: list = []
        engine._load_config = lambda: loads6.append(1)
        engine._persist_peak_path_after_update = (
            lambda: persists6.append(True))
        cju.download_peak_from_link = (
            lambda *a, **k: r"C:\dir\重聚脱机巅峰【2.8.5】.exe")
        try:
            ok4 = engine._peak_web_update_fallback(
                "手动下载：https://wwu.lanzouu.com/s/cjdf",
                r"C:\dir\重聚脱机巅峰【2.8.4】.exe",
                i=0, gw=SimpleNamespace())
        finally:
            cju.download_peak_from_link = orig_dl
        check("6d 成功 → True", ok4 is True)
        check("6d2 日志「巅峰更新完成（网页下载）」",
              any("巅峰更新完成（网页下载）" in m for m in logs), f"logs={logs}")
        check("6d3 重载了配置+写回路径",
              loads6 == [1] and persists6 == [True],
              f"loads={loads6} persists={persists6}")

        # 6e 当前路径为空且配置也没有 → False
        logs.clear()
        engine._config_manager.peak_path = ""
        cju.download_peak_from_link = lambda *a, **k: "不应被调用"
        try:
            ok5 = engine._peak_web_update_fallback(
                "手动下载：https://wwu.lanzouu.com/s/cjdf", "")
        finally:
            cju.download_peak_from_link = orig_dl
        check("6e 无路径配置 → False", ok5 is False)
        check("6e2 日志「未配置巅峰客户端路径」",
              any("未配置巅峰客户端路径" in m for m in logs), f"logs={logs}")
    finally:
        os.unlink(ledger)


if __name__ == "__main__":
    test_extract_update_link()
    test_is_peak_exe()
    test_download_peak_from_link()
    test_read_update_popup_text()
    test_handle_update_landing()
    test_peak_web_fallback()
    print(f"\n{'=' * 50}")
    if _CHECKS["fail"] == 0:
        print(f"ALL PASSED: {_CHECKS['n']} 项")
    else:
        print(f"FAILED: {_CHECKS['fail']}/{_CHECKS['n']} 项")
        sys.exit(1)
