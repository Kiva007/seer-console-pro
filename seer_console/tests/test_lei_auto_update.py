# -*- coding: utf-8 -*-
"""雷小伊全自动更新单元测试（2026-09：纯 HTTP 蓝奏云下载 + 登录线程集成）。

覆盖：
  1. lei_updater.solve_acw_sc_v2：纯 Python WAF 挑战求解（已知向量）
  2. lei_updater.parse_size / _is_complete_exe
  3. lei_updater.fetch_latest_daily：文件列表解析与最高版本选择
  4. lei_updater.download_latest_lei：七步全链路（FakeSession）——
     电信源成功 / WAF 挑战 / 版本门 / 本地复用 / 取消 / 电信失败普通备源 / 全源失败
  5. engine._persist_lei_path：配置写回（客户端列表 + 雷小伊目录7）
  6. engine._launch_one：_lei_updated_path 覆盖 + allow_login_update 透传
  7. engine._login_lei_xiao_yi ②：更新检测决策矩阵
     （relaunched 重走/dismissed 继续/无弹窗/取消/aborted/轮次上限）
  8. engine._lei_handle_update：全自动更新状态机
     （成功换进程/下载失败回退/单号不更新/取消/新版启动失败旧版兜底/双双失败）
  9. engine.launch_all：雷小伊批量的更新记录清空与门控

运行（无 pytest 依赖，同 test_peak_update_cancel.py 风格）：
    python seer_console/tests/test_lei_auto_update.py
"""

import functools
import os
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.account import AccountManager
from seer_console.core.client import GameWindow, WindowFinder
from seer_console.core.config import ConfigManager, ClientEntry
from seer_console.core.engine import AutomationEngine, ClientType
import seer_console.core.engine as eng_mod
import seer_console.core.lei_updater as lu


# ============================================================
# 断言工具
# ============================================================

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# ============================================================
# 已知 WAF 挑战向量（diag 实测，node 权威输出比对一致）
# ============================================================

ACW_ARG1 = "97E84BA7B7105A528BDFA0677C2693EB29AE01C6"
ACW_M = "[0xf,0x23,0x1d,0x18,0x21,0x10,0x1,0x26,0xa,0x9,0x13,0x1f,0x28,0x1b," \
        "0x16,0x17,0x19,0xd,0x6,0xb,0x27,0x12,0x14,0x8,0xe,0x15,0x20,0x1a,0x2," \
        "0x1e,0x7,0x4,0x11,0x5,0x3,0x1c,0x22,0x25,0xc,0x24]"
ACW_COOKIE = "6a9735f17b5b020073a4caa49abf1aa8a366937b"  # node 权威输出（40 位十六进制）
ACW_SCRIPT = f"var arg1='{ACW_ARG1}';var m={ACW_M};(function(){{}})();"


# ============================================================
# 引擎测试环境（同 test_peak_update_cancel.py 风格）
# ============================================================

LEDGER_LINES = [
    "1|*|900000019|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6",
    "1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6",
]


def make_ledger() -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="gbk")
    tmp.write("\n".join(LEDGER_LINES) + "\n")
    tmp.close()
    return tmp.name


class _FakeConfig:
    """最小配置桩：支持 _persist_lei_path 用到的全部接口。"""

    def __init__(self):
        self.client_paths: dict = {}
        self.saved = 0
        self.lei_auto_update = True

    def get_all_window_config(self) -> dict:
        return {}

    def get_seal_names(self) -> dict:
        return {}

    def get_lei_auto_update(self) -> bool:
        """[雷小伊] 雷小伊启动更新（默认真；测试可改为 False 验证关闭分支）"""
        return self.lei_auto_update

    def get_client_entries(self) -> list:
        return []

    def set_client_entries(self, entries) -> None:
        pass

    def set_client_path(self, section: str, key: str, path: str) -> None:
        self.client_paths[(section, key)] = path

    def get_client_path(self, section: str, key: str) -> str:
        return self.client_paths.get((section, key), "")

    def get_current_daily_path(self) -> str:
        return ""

    def save(self) -> None:
        self.saved += 1


class _FakePM:
    """可编排 launch 结果的进程管理器桩。launch_results 依序消费。"""

    def __init__(self, launch_results=None):
        self.kills: list = []
        self.launches: list[str] = []
        self.last_error = ""
        self.launch_results = list(launch_results or [])

    def kill(self, gw) -> None:
        self.kills.append(gw)

    def launch(self, exe_path: str):
        self.launches.append(exe_path)
        if self.launch_results:
            return self.launch_results.pop(0)
        return SimpleNamespace(process_id=1234, process=object(),
                               login_cancelled=threading.Event(),
                               login_finished=threading.Event(),
                               login_thread=None, window_handle=0,
                               output_handle=0, timed_out=False)


def make_engine(pm=None, cfg=None):
    ledger = make_ledger()
    am = AccountManager(ledger)
    am.load()
    logs: list[str] = []
    engine = AutomationEngine(am, cfg or _FakeConfig(), pm or _FakePM(),
                              None, log_callback=logs.append)
    return engine, am, logs, ledger


class FakeTime:
    """替换 engine.time：sleep 直接推进时钟，等待循环瞬时完成。"""

    def __init__(self):
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


class _FinderShim:
    """替换 engine.WindowFinder：close_window 记录，其余委托真实类。"""

    def __init__(self):
        self.closed: list[int] = []

    def close_window(self, hwnd: int) -> None:
        self.closed.append(hwnd)

    def __getattr__(self, name):
        return getattr(WindowFinder, name)


class ScriptedFinder:
    """find_window_by_pid 按脚本出结果：plan=[(title, cls, result), …]，
    匹配即消费；未匹配返回 None（对齐 WindowFinder 的「未找到=None」契约，
    绝不能返回 0——0 会被登录流程当作有效句柄走进真实 Win32 调用）。"""

    def __init__(self):
        self.plan: list[tuple[str, str, int]] = []
        self.calls: list[tuple] = []
        self.closed: list[int] = []

    def expect(self, title: str, cls: str, result: int) -> None:
        self.plan.append((title, cls, result))

    def find_window_by_pid(self, pid, title, cls, match_full_title=False,
                           timeout=0):
        self.calls.append((pid, title, cls))
        for i, (pt, pc, pr) in enumerate(self.plan):
            if pt == title and pc == cls:
                self.plan.pop(i)
                return pr
        return None

    def close_window(self, hwnd: int) -> None:
        self.closed.append(hwnd)


# ============================================================
# engine 模块全局名打桩工具
# ============================================================
# ⚠️ 桩必须挂 engine 模块全局名：engine 方法体内的 WindowFinder / time /
# get_child_windows 等按「模块全局名」解析，往 engine 实例上设同名属性
# 拦不住（第 7 节曾因此全 FAIL：真实 Win32 调用 + 真实 sleep 跑进测试）。
_MODULE_GLOBAL_NAMES = ("WindowFinder", "time", "get_child_windows",
                        "get_control_id", "get_control_text",
                        "real_click_control", "delay")


def _restore_module_globals(fn):
    """测试函数退出时恢复 engine 模块全局名（防桩泄漏到后续测试）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        saved = {n: getattr(eng_mod, n) for n in _MODULE_GLOBAL_NAMES}
        try:
            return fn(*args, **kwargs)
        finally:
            for n, v in saved.items():
                setattr(eng_mod, n, v)
    return wrapper


# ============================================================
# 1. solve_acw_sc_v2
# ============================================================

def test_solve_acw() -> None:
    print("\n[1] lei_updater.solve_acw_sc_v2（纯 Python WAF 挑战求解）")
    val = lu.solve_acw_sc_v2(ACW_SCRIPT)
    check("1a 已知向量求解一致", val == ACW_COOKIE, f"got {val}")
    # 完整挑战页 HTML（含 <script> 包裹）也应能解
    html = f"<html><script>var arg1='{ACW_ARG1}';var xx={ACW_M};</script></html>"
    val2 = lu.solve_acw_sc_v2(html)
    check("1b HTML 包裹同样可解", val2 == ACW_COOKIE, f"got {val2}")
    try:
        lu.solve_acw_sc_v2("<html>no challenge here</html>")
        check("1c 无挑战内容抛错", False, "未抛出")
    except lu.LeiUpdateError:
        check("1c 无挑战内容抛错", True)


# ============================================================
# 2. parse_size / _is_complete_exe
# ============================================================

def test_size_helpers() -> None:
    print("\n[2] parse_size / _is_complete_exe")
    check("2a 19.0 M", lu.parse_size("19.0 M") == int(19.0 * 1048576))
    check("2b 890.6 K", lu.parse_size("890.6 K") == int(890.6 * 1024))
    check("2c 1.2 G", lu.parse_size("1.2 G") == int(1.2 * 1024 ** 3))
    check("2d 空串→0", lu.parse_size("") == 0)
    check("2e 乱码→0", lu.parse_size("abc") == 0)

    tmpdir = tempfile.mkdtemp(prefix="lei_sz_")
    body_1m = b"MZ" + b"A" * (1200 * 1024)   # ~1.17MiB

    f_ok = os.path.join(tmpdir, "ok.exe")
    open(f_ok, "wb").write(body_1m)
    check("2f MZ+≥1MB+大小吻合", lu._is_complete_exe(f_ok, len(body_1m)))
    check("2g 无预期大小（只查 MZ/≥1MB）", lu._is_complete_exe(f_ok, 0))
    check("2h 预期大小偏差>5%", not lu._is_complete_exe(f_ok, len(body_1m) * 2))

    f_bad = os.path.join(tmpdir, "bad.exe")
    open(f_bad, "wb").write(b"XX" + b"A" * (1200 * 1024))
    check("2i 非 MZ 头", not lu._is_complete_exe(f_bad, 0))

    f_small = os.path.join(tmpdir, "small.exe")
    open(f_small, "wb").write(b"MZ" + b"A" * 1000)
    check("2j <1MB 残件", not lu._is_complete_exe(f_small, 0))
    check("2k 不存在", not lu._is_complete_exe(os.path.join(tmpdir, "no.exe"), 0))


# ============================================================
# 3/4. FakeSession 全链路
# ============================================================

class _FakeCookies:
    def __init__(self):
        self.store: dict[str, str] = {}

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


SHARE_HTML = ("var ck='x'; 'fid':12345,'uid':'99','puid':'abc';"
              "var ib49br='1112345678';var _h2ldk='a0d77add4f7112c25255e23a77ca7e38';"
              "data:{'lx':2,'fid':12345,'t':ib49br,'k':_h2ldk,'up':1}")
LIST_JSON = {"zt": 1, "text": [
    {"id": "f01", "name_all": "雷小伊脱机日常 Beta 1.8.13.exe", "size": "1.0 M"},
    {"id": "f02", "name_all": "雷小伊脱机日常 Beta 1.8.15.exe", "size": "1.0 M"},
    {"id": "f03", "name_all": "雷小伊登录器 2.0.17.exe", "size": "1.0 M"},
]}
FILE_PAGE = '<iframe src="/fn?XYZ789">'
FN_PAGE = ("var wp_sign = 'S1'; var ajaxdata = 'A1'; "
           "url: '/ajaxfile.php?file=77'")
AJAX_JSON = {"zt": 1, "dom": "https://developer2.lanrar.com", "url": "?DLKEY"}
DL_BODY = b"MZ" + b"X" * 1024


class FakeSession:
    """按 URL 规则路由的 requests.Session 桩（记录 gets/posts）。"""

    def __init__(self, dl_router=None):
        self.cookies = _FakeCookies()
        self.headers: dict = {}
        self.gets: list[str] = []
        self.posts: list[str] = []
        # dl_router(url) → _FakeResp；缺省 200 MZ
        self.dl_router = dl_router

    def get(self, url, **kw):
        self.gets.append(url)
        if url == lu.SHARE_URL:
            return _FakeResp(text=SHARE_HTML)
        if url == "https://lxy.lanzouj.com/f02":
            if "acw_sc__v2" not in self.cookies.store:
                return _FakeResp(text="<script>" + ACW_SCRIPT + "</script>")
            return _FakeResp(text=FILE_PAGE)
        if url == "https://lxy.lanzouj.com/fn?XYZ789":
            return _FakeResp(text=FN_PAGE)
        if url.startswith("https://developer2.lanrar.com/file/") or \
                url.startswith("https://slssctm.dmpdmp.com/file/"):
            if self.dl_router:
                return self.dl_router(url)
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


def test_fetch_latest_daily() -> None:
    print("\n[3] fetch_latest_daily（列表解析与最高版本选择）")
    s = FakeSession()
    entry = lu.fetch_latest_daily(s)
    check("3a 选中最高版本 1.8.15",
          entry["name_all"] == "雷小伊脱机日常 Beta 1.8.15.exe",
          f"got {entry['name_all']}")
    check("3b 提交了列表接口", any("filemoreajax" in u for u in s.posts))

    # 接口失败
    s2 = FakeSession()
    orig = LIST_JSON
    bad = {"zt": 0, "info": "失败"}
    lu_list_bad = bad

    class S2(FakeSession):
        def post(self, url, **kw):
            self.posts.append(url)
            if "filemoreajax.php" in url:
                return _FakeResp(json_data=lu_list_bad)
            return FakeSession.post(self, url, **kw)

    s2 = S2()
    try:
        lu.fetch_latest_daily(s2)
        check("3c zt!=1 抛错", False, "未抛出")
    except lu.LeiUpdateError:
        check("3c zt!=1 抛错", True)

    # 无日常文件
    empty = {"zt": 1, "text": [{"id": "f03",
                                "name_all": "雷小伊登录器 2.0.17.exe"}]}

    class S3(FakeSession):
        def post(self, url, **kw):
            self.posts.append(url)
            if "filemoreajax.php" in url:
                return _FakeResp(json_data=empty)
            return FakeSession.post(self, url, **kw)

    try:
        lu.fetch_latest_daily(S3())
        check("3d 无日常文件抛错", False, "未抛出")
    except lu.LeiUpdateError:
        check("3d 无日常文件抛错", True)
    _ = orig

    # 3e 变量名轮换回归（2026-09-02 实测用户机器：ib49br/_h2ldk → ib6ehj/_h4qjc）
    rotated = ("<script>var in32l5 ='雷小伊';var pwd;var pgs;"
               "var ib6ehj = '1788298060';"
               "var _h4qjc = 'a0d77add4f7112c25255e23a77ca7e38';"
               "pgs =1;function file(){$.ajax({type:'post',"
               "url:'/filemoreajax.php?file=8948676',data:{'lx':2,"
               "'fid':8948676,'uid':'3675175','puid':'CDsGZw',"
               "'pg':pgs,'rep':'0','t':ib6ehj,'k':_h4qjc,'up':1,},"
               "dataType:'json'});}</script>")
    p = lu._extract_share_params(rotated)
    check("3e 轮换变量名结构定位",
          p["t"] == "1788298060"
          and p["k"] == "a0d77add4f7112c25255e23a77ca7e38"
          and p["fid"] == "8948676" and p["uid"] == "3675175",
          str(p))

    # 3f 兜底：无 't':<名> 引用时按值形态提取（相邻 var 声明）
    fallback = ("var ib6ehj = '1788298060';"
                "var _h4qjc = 'a0d77add4f7112c25255e23a77ca7e38';")
    p = lu._extract_share_params("'fid':1,'uid':'2','puid':'x';" + fallback)
    check("3f 值形态兜底提取", p["t"] == "1788298060"
          and p["k"] == "a0d77add4f7112c25255e23a77ca7e38", str(p))


def test_download_full_chain() -> None:
    print("\n[4] download_latest_lei 七步全链路（FakeSession）")
    tmpdir = tempfile.mkdtemp(prefix="lei_dl_")
    cur = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.13.exe")
    logs: list[str] = []
    s = FakeSession()
    path = lu.download_latest_lei(tmpdir, current_path=cur,
                                  log=logs.append, session=s)
    name = "雷小伊脱机日常 Beta 1.8.15.exe"
    check("4a 返回目标路径", os.path.basename(path) == name, path)
    check("4b 文件已落盘且 MZ",
          os.path.isfile(path) and open(path, "rb").read(2) == b"MZ")
    check("4c WAF cookie 已求解",
          s.cookies.store.get("acw_sc__v2") == ACW_COOKIE,
          str(s.cookies.store))
    check("4d 电信源一次成功",
          sum(1 for u in s.gets if "developer2" in u) == 1
          and not any("slssctm" in u for u in s.gets))
    check("4e 走过文件页+iframe",
          any(u.endswith("/f02") for u in s.gets)
          and any("/fn?" in u for u in s.gets))
    check("4f 有过程日志", any("分享页最新" in m for m in logs))

    # 4g 版本门：分享页不高于当前 → 不下载
    cur_same = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.15.exe")
    os.remove(path)
    s2 = FakeSession()
    try:
        lu.download_latest_lei(tmpdir, current_path=cur_same, session=s2)
        check("4g 版本不升抛错", False, "未抛出")
    except lu.LeiUpdateError as e:
        check("4g 版本不升抛错", "不高于当前" in str(e), str(e))
    check("4g2 未触发下载页请求",
          not any("/f02" in u or "/fn?" in u for u in s2.gets))

    # 4h 本地已有完整文件 → 复用（大小须与列表 size "1.0 M" 吻合，偏差≤5%）
    os.remove(path) if os.path.exists(path) else None
    local = os.path.join(tmpdir, name)
    payload = b"MZ" + b"B" * (1024 * 1024 - 2)   # 恰好 1 MiB
    open(local, "wb").write(payload)
    s3 = FakeSession()
    path3 = lu.download_latest_lei(
        tmpdir, current_path=cur, log=logs.append, session=s3)
    check("4h 复用本地文件", path3 == local and os.path.getsize(local) == len(payload))
    check("4h2 未请求文件页", not any("/f02" in u for u in s3.gets))

    # 4i 取消
    os.remove(local)
    try:
        lu.download_latest_lei(tmpdir, current_path=cur, session=FakeSession(),
                               cancel_check=lambda: True)
        check("4i 取消抛 LeiUpdateCancelled", False, "未抛出")
    except lu.LeiUpdateCancelled:
        check("4i 取消抛 LeiUpdateCancelled", True)

    # 4j 电信 404 → 普通源成功
    def dl_router(url):
        if "developer2" in url:
            return _FakeResp(status_code=404, text="gone")
        return _FakeResp(body=DL_BODY,
                         headers={"Content-Length": str(len(DL_BODY))})

    os.remove(path) if os.path.exists(path) else None
    s4 = FakeSession(dl_router=dl_router)
    logs4: list[str] = []
    path4 = lu.download_latest_lei(tmpdir, current_path=cur,
                                   log=logs4.append, session=s4)
    check("4j 普通源兜底成功",
          os.path.basename(path4) == name and open(path4, "rb").read(2) == b"MZ")
    check("4j2 电信失败日志", any("电信源下载失败" in m for m in logs4))
    check("4j3 普通源下载请求", any("slssctm" in u for u in s4.gets))

    # 4k 全部源失败
    def all_bad(url):
        return _FakeResp(status_code=404, text="gone")

    os.remove(path4)
    try:
        lu.download_latest_lei(tmpdir, current_path=cur,
                               session=FakeSession(dl_router=all_bad))
        check("4k 全源失败抛错", False, "未抛出")
    except lu.LeiUpdateError as e:
        check("4k 全源失败抛错", "全部下载源失败" in str(e), str(e))


# ============================================================
# 5. _persist_lei_path
# ============================================================

def test_persist_lei_path() -> None:
    print("\n[5] engine._persist_lei_path（配置写回）")
    tmpdir = tempfile.mkdtemp(prefix="lei_cfg_")
    ini = os.path.join(tmpdir, "ctrl.ini")
    old = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.13.exe")
    new = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.15.exe")
    cfg = ConfigManager(ini)
    cfg.set_client_entries([
        ClientEntry(name="雷小伊 登录器", path=os.path.join(tmpdir, "登录器 2.0.16.exe"),
                    kind="登录器"),
        ClientEntry(name="雷小伊 日常", path=old, kind="日常", is_daily=True),
    ])
    engine, am, logs, ledger = make_engine(cfg=cfg)
    engine._persist_lei_path(new)
    check("5a 雷小伊目录7 已写新路径",
          cfg.get_client_path("雷小伊", "雷小伊目录7") == new)
    entries = cfg.get_client_entries()
    lei = next(e for e in entries if e.name == "雷小伊 日常")
    check("5b 客户端列表条目已更新", lei.path == new)
    check("5c 登录器条目不受影响",
          next(e for e in entries if e.kind == "登录器").path
          .endswith("登录器 2.0.16.exe"))


# ============================================================
# 6. _launch_one：路径覆盖与 allow_login_update 透传
# ============================================================

def test_launch_one_plumb() -> None:
    print("\n[6] _launch_one（_lei_updated_path 覆盖 + allow_login_update 透传）")
    tmpdir = tempfile.mkdtemp(prefix="lei_lo_")
    old = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.13.exe")
    new = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.15.exe")
    open(old, "wb").close()
    open(new, "wb").close()

    pm = _FakePM()
    engine, am, logs, ledger = make_engine(pm=pm)
    engine.login_window = lambda *a, **k: None   # 不真正起登录流程
    account = am.accounts[0]

    # 6a 批量默认：无更新记录 → 原路径；allow_login_update=True
    gw = engine._launch_one(0, ClientType.LEI_XIAO_YI, old, account)
    check("6a 原路径启动", pm.launches[-1] == old)
    check("6a2 exe_path 记录", gw.exe_path == old)
    check("6a3 默认开放更新", gw.allow_login_update is True)

    # 6b 有更新记录 → 新路径
    engine._lei_updated_path = new
    engine._launch_one(0, ClientType.LEI_XIAO_YI, old, account)
    check("6b 新路径覆盖", pm.launches[-1] == new)

    # 6c 记录指向不存在的文件 → 回退原路径
    engine._lei_updated_path = os.path.join(tmpdir, "不存在.exe")
    engine._launch_one(0, ClientType.LEI_XIAO_YI, old, account)
    check("6c 失效记录回退原路径", pm.launches[-1] == old)

    # 6d allow_update=False 透传
    engine._lei_updated_path = ""
    gw4 = engine._launch_one(0, ClientType.LEI_XIAO_YI, old, account,
                             allow_update=False)
    check("6d 单号不开放更新", gw4.allow_login_update is False)


# ============================================================
# 7. _login_lei_xiao_yi ② 决策矩阵
# ============================================================

def _patch_engine_time(engine=None) -> FakeTime:
    # 挂 engine 模块全局 time（方法体按模块全局名解析）
    ft = FakeTime()
    eng_mod.time = ft
    return ft


@_restore_module_globals
def test_login_step2_matrix() -> None:
    print("\n[7] _login_lei_xiao_yi ② 更新检测决策矩阵")
    H_INFO, H_HINT = 11, 22

    # 7a relaunched：换进程后用新 pid 重走①②，第二轮无弹窗 → 检测过 → WTWindow 超时
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    gw.allow_login_update = True
    engine._daily_windows[0] = gw
    finder = ScriptedFinder()
    finder.expect("信息", "#32770", H_INFO)
    finder.expect("提示", "#32770", H_HINT)
    finder.expect("信息", "#32770", 33)
    eng_mod.WindowFinder = finder
    handled = []

    def fake_handle(i, g, hwnd, allow):
        handled.append((i, allow))
        g.process_id = 999          # 模拟进程替换
        return "relaunched"

    engine._lei_handle_update = fake_handle
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7a 处理器调用一次", handled == [(0, True)], str(handled))
    check("7a2 第二轮用新 pid 查信息弹窗",
          any(c[0] == 999 and c[1] == "信息" for c in finder.calls))
    check("7a3 更新标志放行", engine._update_flag == 0)
    check("7a4 检测过日志", any("日常更新检测已过" in m for m in logs))
    check("7a5 WTWindow 超时退出（timed_out）", gw.timed_out is True)

    # 7b dismissed：弹窗关闭后用当前版本继续
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    engine._daily_windows[0] = gw
    finder = ScriptedFinder()
    finder.expect("信息", "#32770", H_INFO)
    finder.expect("提示", "#32770", H_HINT)
    eng_mod.WindowFinder = finder
    engine._lei_handle_update = lambda *a: "dismissed"
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7b dismissed 标志放行", engine._update_flag == 0)
    check("7b2 继续登录日志", any("用当前版本继续登录" in m for m in logs))

    # 7c 非首账号：allow_update=False 传给处理器
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    engine._daily_windows[1] = gw
    finder = ScriptedFinder()
    finder.expect("信息", "#32770", H_INFO)
    finder.expect("提示", "#32770", H_HINT)
    eng_mod.WindowFinder = finder
    handled.clear()
    engine._lei_handle_update = lambda i, g, h, allow: (
        handled.append((i, allow)) or "dismissed")
    engine._login_lei_xiao_yi(1, gw)
    check("7c 非首账号不自动更新", handled == [(1, False)], str(handled))

    # 7g [雷小伊] 雷小伊启动更新未勾选：首账号 allow_update=False（每账号点取消继续）
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    engine._config_manager.lei_auto_update = False
    gw = GameWindow(process_id=100)
    gw.allow_login_update = True
    engine._daily_windows[0] = gw
    finder = ScriptedFinder()
    finder.expect("信息", "#32770", H_INFO)
    finder.expect("提示", "#32770", H_HINT)
    eng_mod.WindowFinder = finder
    handled.clear()
    engine._lei_handle_update = lambda i, g, h, allow: (
        handled.append((i, allow)) or "dismissed")
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7g 未勾选自动更新 → allow_update=False",
          handled == [(0, False)], str(handled))
    check("7g2 dismissed 后放行（用当前版本继续登录）",
          engine._update_flag == 0 and any("用当前版本继续登录" in m for m in logs))

    # 7d 无弹窗：不调处理器，直接放行
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    engine._daily_windows[0] = gw
    finder = ScriptedFinder()
    finder.expect("信息", "#32770", H_INFO)
    eng_mod.WindowFinder = finder
    called = []
    engine._lei_handle_update = lambda *a: called.append(1) or "dismissed"
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7d 无弹窗不调处理器", called == [])
    check("7d2 标志放行+检测过日志",
          engine._update_flag == 0 and any("日常更新检测已过" in m for m in logs))

    # 7e 取消：标志从 -1 兜底为 0，防止批量主循环挂起
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    gw.login_cancelled.set()
    engine._daily_windows[0] = gw
    eng_mod.WindowFinder = ScriptedFinder()
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7e 取消时标志兜底放行", engine._update_flag == 0)
    check("7e2 取消日志", any("登录已取消" in m for m in logs))

    # 7f aborted：置超时并直接中止
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    engine._daily_windows[0] = gw
    finder = ScriptedFinder()
    finder.expect("信息", "#32770", H_INFO)
    finder.expect("提示", "#32770", H_HINT)
    eng_mod.WindowFinder = finder
    engine._lei_handle_update = lambda *a: "aborted"
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7f aborted 标志放行+超时",
          engine._update_flag == 0 and gw.timed_out is True)
    check("7f2 未进入临界区找窗口",
          not any(c[1] == "" for c in finder.calls))

    # 7g 轮次上限：连续 relaunched 3 轮后第 4 次弹窗只关闭不更新
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    gw = GameWindow(process_id=100)
    engine._daily_windows[0] = gw
    finder = ScriptedFinder()
    for _ in range(4):
        finder.expect("信息", "#32770", H_INFO)
        finder.expect("提示", "#32770", H_HINT)
    eng_mod.WindowFinder = finder
    handled.clear()
    results = ["relaunched", "relaunched", "relaunched", "dismissed"]

    def fake_handle2(i, g, hwnd, allow):
        handled.append(allow)
        g.process_id += 1
        return results[len(handled) - 1]

    engine._lei_handle_update = fake_handle2
    engine._update_flag = -1
    engine._login_lei_xiao_yi(0, gw)
    check("7g 四轮弹窗处理", len(handled) == 4, str(handled))
    check("7g2 前三轮开更新、第四轮关闭",
          handled == [True, True, True, False], str(handled))
    check("7g3 最终放行", engine._update_flag == 0)


# ============================================================
# 8. _lei_handle_update 状态机
# ============================================================

class _UpdateEnv:
    """_lei_handle_update 测试环境：Win32/下载/进程全桩化。"""

    def __init__(self, cfg=None):
        self.engine, self.am, self.logs, self.ledger = make_engine(cfg=cfg)
        self.gw = GameWindow(process_id=100,
                             exe_path=r"D:\Games\Seer\lxy\雷小伊脱机日常 Beta 1.8.13.exe")
        self.gw.allow_login_update = True
        self.engine._daily_windows[0] = self.gw
        self.account = self.am.accounts[0]
        self.account.current_action = "运行日常"

        self.children = {501: (65535, "更新啦(〃'▽'〃)\r\n更新说明：测试"),
                         502: (2, "取消")}
        self.clicks: list[int] = []
        self.shim = _FinderShim()
        # 桩挂 engine 模块全局名（方法体按模块全局解析，实例属性拦不住）
        self._mod_saved = {n: getattr(eng_mod, n) for n in
                           ("get_child_windows", "get_control_id",
                            "get_control_text", "real_click_control",
                            "delay", "WindowFinder")}
        eng_mod.get_child_windows = lambda hwnd: (
            list(self.children) if hwnd == 777 else [])
        eng_mod.get_control_id = lambda h: self.children[h][0]
        eng_mod.get_control_text = lambda h: self.children[h][1]
        eng_mod.real_click_control = lambda h: self.clicks.append(h)
        eng_mod.delay = lambda ms: None
        eng_mod.WindowFinder = self.shim

        self.pm = _FakePM()
        self.engine._process_manager = self.pm
        self.dl_calls: list[dict] = []
        self._orig_dl = lu.download_latest_lei
        self.new_path = r"D:\Games\Seer\lxy\雷小伊脱机日常 Beta 1.8.15.exe"

    def stub_download(self, result=None, raise_exc=None):
        env = self

        def fake_dl(dest_dir, current_path="", log=None, cancel_check=None,
                    **kw):
            env.dl_calls.append({"dest": dest_dir, "cur": current_path})
            if raise_exc is not None:
                raise raise_exc
            return result or env.new_path

        lu.download_latest_lei = fake_dl

    def restore(self):
        lu.download_latest_lei = self._orig_dl
        for n, v in self._mod_saved.items():
            setattr(eng_mod, n, v)


@_restore_module_globals
def test_handle_update_states() -> None:
    print("\n[8] _lei_handle_update 状态机")
    H_HINT = 777

    # 8a 全自动成功：点取消 → 下载 → 写配置 → 杀旧 → 启新
    tmpdir = tempfile.mkdtemp(prefix="lei_hu_")
    ini = os.path.join(tmpdir, "ctrl.ini")
    cfg = ConfigManager(ini)
    cfg.set_client_entries([
        ClientEntry(name="雷小伊 日常", path=env_old_exe(), kind="日常",
                    is_daily=True)])
    env = _UpdateEnv(cfg=cfg)
    env.stub_download()
    try:
        result = env.engine._lei_handle_update(0, env.gw, H_HINT, True)
        check("8a 返回 relaunched", result == "relaunched", result)
        check("8a2 点了取消按钮", env.clicks == [502], str(env.clicks))
        check("8a3 未用 WM_CLOSE 兜底", env.shim.closed == [])
        check("8a4 下载参数（目录+当前版本基准）",
              env.dl_calls and env.dl_calls[0]["cur"].endswith("1.8.13.exe")
              and env.dl_calls[0]["dest"].endswith("lxy"))
        check("8a5 杀旧进程", env.pm.kills == [env.gw])
        check("8a6 启动新版本", env.pm.launches == [env.new_path])
        check("8a7 gw 进程替换",
              env.gw.process_id == 1234 and env.gw.exe_path == env.new_path)
        check("8a8 批内路径缓存", env.engine._lei_updated_path == env.new_path)
        check("8a9 配置已写回",
              cfg.get_client_path("雷小伊", "雷小伊目录7") == env.new_path)
        check("8a10 动作恢复运行日常",
              env.account.current_action == "运行日常")
        check("8a11 弹窗正文入日志", any("更新啦" in m for m in env.logs))
        check("8a12 新版进程日志", any("新版进程已启动" in m for m in env.logs))
    finally:
        env.restore()

    # 8b 下载失败：降级 dismissed，不动进程
    env = _UpdateEnv()
    env.stub_download(raise_exc=lu.LeiUpdateError("模拟网络失败"))
    try:
        result = env.engine._lei_handle_update(0, env.gw, H_HINT, True)
        check("8b 失败返回 dismissed", result == "dismissed", result)
        check("8b2 未杀进程", env.pm.kills == [])
        check("8b3 失败日志+回退提示",
              any("自动下载失败" in m for m in env.logs)
              and any("用当前版本继续登录" in m for m in env.logs))
        check("8b4 动作恢复", env.account.current_action == "运行日常")
    finally:
        env.restore()

    # 8c 单号/自检：只关弹窗不下载
    env = _UpdateEnv()
    env.stub_download()
    try:
        result = env.engine._lei_handle_update(0, env.gw, H_HINT, False)
        check("8c 单号返回 dismissed", result == "dismissed", result)
        check("8c2 未下载", env.dl_calls == [])
        check("8c3 单号日志", any("单号/自检运行不自动更新" in m for m in env.logs))
        check("8c4 弹窗已点取消", env.clicks == [502])
    finally:
        env.restore()

    # 8d 下载中取消
    env = _UpdateEnv()
    env.stub_download(raise_exc=lu.LeiUpdateCancelled())
    try:
        result = env.engine._lei_handle_update(0, env.gw, H_HINT, True)
        check("8d 取消返回 cancelled", result == "cancelled", result)
        check("8d2 未杀进程", env.pm.kills == [])
    finally:
        env.restore()

    # 8e 新版启动失败 → 旧版兜底重启
    env = _UpdateEnv()
    env.pm.launch_results = [
        None,
        SimpleNamespace(process_id=888, process=object(),
                        login_cancelled=threading.Event(),
                        login_finished=threading.Event(),
                        login_thread=None, window_handle=0, output_handle=0,
                        timed_out=False),
    ]
    env.stub_download()
    try:
        result = env.engine._lei_handle_update(0, env.gw, H_HINT, True)
        check("8e 旧版兜底返回 relaunched", result == "relaunched", result)
        check("8e2 先新后旧两次启动",
              env.pm.launches == [env.new_path, env.gw_exe_old()],
              str(env.pm.launches))
        check("8e3 gw 用旧版", env.gw.process_id == 888
              and env.gw.exe_path == env.gw_exe_old())
        check("8e4 批内缓存回退清空", env.engine._lei_updated_path == "")
        check("8e5 旧版重启日志", any("已用旧版重启" in m for m in env.logs))
    finally:
        env.restore()

    # 8f 新旧都启动失败 → aborted
    env = _UpdateEnv()
    env.pm.launch_results = [None, None]
    env.stub_download()
    try:
        result = env.engine._lei_handle_update(0, env.gw, H_HINT, True)
        check("8f 双失败返回 aborted", result == "aborted", result)
        check("8f2 gw 置超时", env.gw.timed_out is True)
        check("8f3 日志", any("旧版重启也失败" in m for m in env.logs))
    finally:
        env.restore()


def env_old_exe() -> str:
    return r"D:\Games\Seer\lxy\雷小伊脱机日常 Beta 1.8.13.exe"


# _UpdateEnv.gw_exe_old 辅助
def _gw_exe_old(self):
    return env_old_exe()


_UpdateEnv.gw_exe_old = _gw_exe_old


# ============================================================
# 9. launch_all：雷小伊批量的清空与门控
# ============================================================

@_restore_module_globals
def test_launch_all_lei() -> None:
    print("\n[9] launch_all（雷小伊批量）")
    old = r"D:\Games\Seer\lxy\雷小伊脱机日常 Beta 1.8.13.exe"
    engine, am, logs, ledger = make_engine()
    _patch_engine_time(engine)
    engine._lei_updated_path = "stale_path"
    launches: list[tuple] = []

    def fake_launch_one(i, client_type, exe_path, account, allow_update=True):
        launches.append((i, exe_path, allow_update))
        engine._update_flag = 0   # 模拟②检测完成
        return None

    engine._launch_one = fake_launch_one
    engine.launch_all(ClientType.LEI_XIAO_YI, old, selfcheck=False)
    check("9a 更新记录已清空", engine._lei_updated_path == "")
    check("9b 队列账号全部启动", len(launches) == 2, str(launches))
    check("9c 批量开放更新", all(l[2] is True for l in launches))
    check("9d 全部运行日志", any("全部运行" in m for m in logs))


# ============================================================
# 入口
# ============================================================

def main() -> int:
    test_solve_acw()
    test_size_helpers()
    test_fetch_latest_daily()
    test_download_full_chain()
    test_persist_lei_path()
    test_launch_one_plumb()
    test_login_step2_matrix()
    test_handle_update_states()
    test_launch_all_lei()
    print(f"\n{'=' * 50}")
    if _failed:
        print(f"FAILED {len(_failed)} / {_passed + len(_failed)}:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print(f"ALL PASSED ({_passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
