"""游戏客户端进程管理：subprocess + psutil + 事件驱动窗口发现"""

import os
import time
import threading
import subprocess
import ctypes
import ctypes.wintypes
from dataclasses import dataclass, field
from typing import Optional, Callable

import psutil


# ============================================================
# Windows API 常量与函数
# ============================================================

# SetWinEventHook
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_DESTROY = 0x8001
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

# WinEvent 回调类型
WinEventProc = ctypes.WINFUNCTYPE(
    None,
    ctypes.wintypes.HANDLE,  # hWinEventHook
    ctypes.wintypes.DWORD,   # event
    ctypes.wintypes.HWND,    # hwnd
    ctypes.wintypes.LONG,    # idObject
    ctypes.wintypes.LONG,    # idChild
    ctypes.wintypes.DWORD,   # dwEventThread
    ctypes.wintypes.DWORD,   # dwmsEventTime
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 窗口枚举回调类型
EnumWindowsProc = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL,
    ctypes.wintypes.HWND,
    ctypes.wintypes.LPARAM,
)

# 显式声明 Win32 函数签名（64位下 LPARAM 为 8 字节，否则 id() 指针值会被截断为 32 位 int）
user32.EnumWindows.argtypes = [EnumWindowsProc, ctypes.wintypes.LPARAM]
user32.EnumWindows.restype = ctypes.wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
user32.IsWindow.restype = ctypes.wintypes.BOOL
user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
user32.PostMessageW.restype = ctypes.wintypes.BOOL
user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL


# ============================================================
# 窗口数据结构
# ============================================================

@dataclass
class GameWindow:
    """游戏窗口运行时状态"""
    thread_handle: int = 0
    thread_id: int = 0
    process_handle: int = 0
    process_id: int = 0
    window_handle: int = 0
    output_handle: int = 0  # 输出框句柄
    changed: bool = False  # 自检后被更改状态
    timed_out: bool = False  # 是否超时
    # 本次登录流程的最终结果。display_name 可能保留旧窗口/控件文本，不能单独作为登录成功依据。
    login_succeeded: bool = False
    login_finished: threading.Event = field(default_factory=threading.Event)
    # 停止/重启会立即置位。旧登录线程在此后不得继续写成功状态或操作窗口。
    login_cancelled: threading.Event = field(default_factory=threading.Event)
    login_thread: Optional[threading.Thread] = None
    exe_path: str = ""  # 实际启动的 exe 路径（引擎更新流程定位新版用）
    # 批量启动开放登录期自动更新（雷小伊）；单号运行/自检重启=False
    # （对齐 XryM allow_update 语义：仅批量「全部运行」才自动换版本）
    allow_login_update: bool = True
    # subprocess.Popen 对象
    process: Optional[subprocess.Popen] = None
    # 登录后控件句柄快照
    control_snapshot: dict[str, int] = field(default_factory=dict)


# ============================================================
# 进程管理
# ============================================================

class ProcessManager:
    """统一管理游戏客户端进程的创建、监控、销毁"""

    def __init__(self):
        self._lock = threading.Lock()
        self.last_error = ""  # 最近一次 launch 失败的原因（供上层日志）

    def launch(self, exe_path: str, args: list[str] | None = None,
               cwd: str | None = None) -> Optional[GameWindow]:
        """启动一个游戏客户端进程，返回带PID的窗口结构"""
        if not os.path.isfile(exe_path):
            return None

        cmd = [exe_path]
        if args:
            cmd.extend(args)

        work_dir = cwd or os.path.dirname(exe_path)

        # 使用 CREATE_NO_WINDOW 避免弹出控制台窗口（如适用）
        creation_flags = 0
        if exe_path.lower().endswith(".exe"):
            creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=work_dir,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self.last_error = str(e)
            return None

        gw = GameWindow(
            process_id=proc.pid,
            process=proc,
            process_handle=proc.pid,  # 暂时用pid代替
        )
        return gw

    def kill(self, gw: GameWindow, force: bool = True) -> bool:
        """彻底销毁一个游戏进程及其所有子进程"""
        if gw.process is None and gw.process_id == 0:
            return True

        pid = gw.process_id
        try:
            proc = psutil.Process(pid)
            if force:
                # 先杀子进程，再杀父进程
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
            proc.kill()
            proc.wait(timeout=10)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

        # 清理子进程引用的残留
        gw.process = None
        gw.process_id = 0
        gw.window_handle = 0
        return True

    def is_alive(self, gw: GameWindow) -> bool:
        """检查进程是否存活"""
        if gw.process_id == 0:
            return False
        try:
            proc = psutil.Process(gw.process_id)
            return proc.is_running()
        except psutil.NoSuchProcess:
            return False

    def wait_for_exit(self, gw: GameWindow, timeout: float = 30.0) -> bool:
        """等待进程自然退出"""
        if gw.process is None:
            return True
        try:
            gw.process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False


# ============================================================
# 窗口发现（事件驱动 + PID精确匹配）
# ============================================================

class WindowFinder:
    """窗口查找：SetWinEventHook事件驱动 + EnumWindows精确匹配"""

    _event_hook: int = 0
    _callback: Optional[WinEventProc] = None

    @staticmethod
    def find_window_by_pid(
        pid: int,
        title_pattern: str = "",
        class_pattern: str = "",
        match_full_title: bool = False,
        timeout: float = 30.0,
        interval: float = 0.3,
    ) -> Optional[int]:
        """
        轮询查找——在事件钩子不可用时的后备方案。
        按PID枚举所有顶层窗口，匹配标题/类名。

        Args:
            pid: 目标进程ID
            title_pattern: 标题子串（空=任意）
            class_pattern: 窗口类名子串（空=任意）
            match_full_title: True=完全匹配标题，False=子串匹配
            timeout: 超时秒数
            interval: 轮询间隔

        Returns:
            窗口句柄（整数），超时返回None
        """
        start = time.time()
        while time.time() - start < timeout:
            hwnd = WindowFinder._enum_windows_by_pid(
                pid, title_pattern, class_pattern, match_full_title
            )
            if hwnd:
                return hwnd
            time.sleep(interval)
        return None

    @staticmethod
    def _enum_windows_by_pid(
        pid: int,
        title_pattern: str = "",
        class_pattern: str = "",
        match_full_title: bool = False,
    ) -> Optional[int]:
        """枚举窗口，按PID+标题/类名匹配"""

        # 需要搜索的数据结构
        search_ctx = {
            "pid": pid,
            "title": title_pattern,
            "class": class_pattern,
            "match_full": match_full_title,
            "found_hwnd": 0,
        }

        @EnumWindowsProc
        def enum_callback(hwnd, lparam):
            hwnd = int(hwnd or 0)  # c_void_p → int
            ctx = ctypes.cast(lparam, ctypes.py_object).value

            # 获取窗口进程ID
            pid_buf = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))

            if pid_buf.value != ctx["pid"]:
                return True  # 继续枚举

            # 检查标题
            if ctx["title"]:
                title_buf = (ctypes.c_wchar * 512)()
                user32.GetWindowTextW(hwnd, title_buf, 511)
                title = title_buf.value or ""
                if ctx["match_full"]:
                    if title != ctx["title"]:
                        return True
                else:
                    if ctx["title"] not in title:
                        return True

            # 检查类名
            if ctx["class"]:
                class_buf = (ctypes.c_wchar * 256)()
                user32.GetClassNameW(hwnd, class_buf, 255)
                class_name = class_buf.value or ""
                if ctx["class"] not in class_name:
                    return True

            # 注意：客户端主窗口可能被易语言 _EL_HideOwner 隐藏（后台运行），
            # 因此不能强制要求可见；按 PID+标题+类名匹配即可。

            ctx["found_hwnd"] = hwnd
            return False  # 找到，停止枚举

        user32.EnumWindows(enum_callback, id(search_ctx))
        return search_ctx["found_hwnd"] if search_ctx["found_hwnd"] else None

    @staticmethod
    def find_all_windows_by_pid(pid: int) -> list[int]:
        """获取指定PID的所有顶层窗口句柄"""
        hwnds = []

        @EnumWindowsProc
        def enum_callback(hwnd, lparam):
            hwnd = int(hwnd or 0)  # c_void_p → int
            pid_buf = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
            if pid_buf.value == pid and user32.IsWindowVisible(hwnd):
                hwnds.append(hwnd)
            return True

        user32.EnumWindows(enum_callback, 0)
        return hwnds

    @staticmethod
    def get_window_title(hwnd: int) -> str:
        """获取窗口标题"""
        buf = (ctypes.c_wchar * 1024)()
        user32.GetWindowTextW(hwnd, buf, 1023)
        return buf.value or ""

    @staticmethod
    def get_class_name(hwnd: int) -> str:
        """获取窗口类名"""
        buf = (ctypes.c_wchar * 256)()
        user32.GetClassNameW(hwnd, buf, 255)
        return buf.value or ""

    @staticmethod
    def is_window_valid(hwnd: int) -> bool:
        """IsWindow() 检查句柄是否仍然有效"""
        return bool(user32.IsWindow(hwnd))

    @staticmethod
    def close_window(hwnd: int) -> bool:
        """发送 WM_CLOSE 消息"""
        WM_CLOSE = 0x0010
        return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))

    @staticmethod
    def set_foreground_window(hwnd: int) -> bool:
        """将窗口设为前台焦点"""
        return bool(user32.SetForegroundWindow(hwnd))
