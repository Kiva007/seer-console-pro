"""Win32 自动化：窗口操控、控件枚举、消息投递、文本输入"""

import time
import ctypes
import ctypes.wintypes
from typing import Optional

import win32con
import win32gui
import win32api

from .client import WindowFinder


# ============================================================
# Win32 常量
# ============================================================

BM_CLICK = 0x00F5  # 245: 按钮点击
WM_SETTEXT = 0x000C  # 文本设置
WM_GETTEXT = 0x000D  # 获取文本
WM_GETTEXTLENGTH = 0x000E
EM_SETSEL = 0x00B1  # 选择文本
TCM_SETCURFOCUS = 0x1330  # 4912: 选择夹切换页
WM_COMMAND = 0x0111

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


# ============================================================
# 控件操作
# ============================================================

def get_child_windows(hwnd: int) -> list[int]:
    """枚举窗口的所有直接子窗口句柄"""
    children: list[int] = []

    def enum_callback(child_hwnd, _):
        children.append(child_hwnd)
        return True

    win32gui.EnumChildWindows(hwnd, enum_callback, 0)
    return children


def get_control_id(hwnd: int) -> int:
    """获取控件ID (GetDlgCtrlID)"""
    return win32gui.GetDlgCtrlID(hwnd)


def get_control_text(hwnd: int) -> str:
    """获取控件文本（跨进程可靠：SendMessageW WM_GETTEXT；GetWindowTextW 跨进程读子控件不可靠）"""
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if length == 0:
        return ""
    buf = (ctypes.c_wchar * (length + 1))()
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.byref(buf))
    return buf.value or ""


def send_text(hwnd: int, text: str) -> bool:
    """通过 WM_SETTEXT 发送文本到控件（显式 c_wchar_p，避免 str 转换错误）"""
    return bool(user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text)))


def post_click(hwnd: int) -> bool:
    """投递 BM_CLICK 消息（模拟按钮点击）"""
    return bool(user32.PostMessageW(hwnd, BM_CLICK, 0, 0))


def send_click(hwnd: int) -> bool:
    """发送 BM_CLICK 消息"""
    return bool(user32.SendMessageW(hwnd, BM_CLICK, 0, 0))


def switch_tab(hwnd: int, page: int) -> bool:
    """切换选择夹页面（TCM_SETCURFOCUS, msg=4912）"""
    return bool(user32.SendMessageW(hwnd, TCM_SETCURFOCUS, page, 0))


def set_focus(hwnd: int) -> bool:
    """设置焦点到窗口/控件"""
    user32.SetFocus(hwnd)
    return True


def select_all(hwnd: int) -> bool:
    """全选编辑框文本"""
    # EM_SETSEL: wParam=0, lParam=-1
    return bool(user32.SendMessageW(hwnd, EM_SETSEL, 0, -1))


def clear_edit(hwnd: int) -> bool:
    """清空编辑框（发送 WM_SETTEXT 空字符串）"""
    return send_text(hwnd, "")


def external_edit_get_content(hwnd: int) -> str:
    """读取外部编辑框（跨进程）全部内容"""
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if length == 0:
        return ""
    buf = (ctypes.c_wchar * (length + 1))()
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.byref(buf))
    return buf.value


# ============================================================
# 鼠标操作
# ============================================================

def mouse_click(hwnd: int, x: int, y: int) -> None:
    """在指定控件的(x,y)位置模拟鼠标点击"""
    # 将客户区坐标转屏幕坐标
    lparam = y << 16 | x
    # WM_LBUTTONDOWN + WM_LBUTTONUP
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 0, lparam)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)


def mouse_move_click(hwnd: int, x: int, y: int) -> None:
    """移动鼠标并点击（SendInput方式，更可靠）"""
    import ctypes

    # 获取窗口在屏幕上的位置
    rect = win32gui.GetWindowRect(hwnd)
    screen_x = rect[0] + x
    screen_y = rect[1] + y

    # SendInput 移动+点击
    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("mi", MOUSEINPUT),
        ]

    # 屏幕分辨率
    screen_width = win32api.GetSystemMetrics(0)
    screen_height = win32api.GetSystemMetrics(1)

    # 绝对坐标 = 像素 * 65536 / 屏幕分辨率
    abs_x = int(screen_x * 65536 / screen_width)
    abs_y = int(screen_y * 65536 / screen_height)

    inputs = (INPUT * 3)()

    # 移动
    inputs[0].type = INPUT_MOUSE
    inputs[0].mi.dx = abs_x
    inputs[0].mi.dy = abs_y
    inputs[0].mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE

    # 按下
    inputs[1].type = INPUT_MOUSE
    inputs[1].mi.dx = abs_x
    inputs[1].mi.dy = abs_y
    inputs[1].mi.dwFlags = MOUSEEVENTF_LEFTDOWN

    # 释放
    inputs[2].type = INPUT_MOUSE
    inputs[2].mi.dx = abs_x
    inputs[2].mi.dy = abs_y
    inputs[2].mi.dwFlags = MOUSEEVENTF_LEFTUP

    ctypes.windll.user32.SendInput(3, ctypes.byref(inputs), ctypes.sizeof(INPUT))


def input_real_keyboard(top_hwnd: int, target_hwnd: int, text: str) -> None:
    """真实键盘输入（SendInput）：置前台→真实鼠标点击目标控件→Ctrl+A→输入文本。

    雷小伊等客户端的密码框拒绝 WM_SETTEXT/WM_CHAR 等窗口消息，只接受真实键盘输入。
    top_hwnd: 目标控件的顶层窗口（用于置前台）；target_hwnd: 目标控件。
    """
    import ctypes

    # ---- SendInput 结构定义 ----
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                    ("wParamH", ctypes.c_ushort)]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUTUNION)]

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    VK_MENU = 0x12
    VK_CONTROL = 0x11
    VK_A = 0x41

    def send_input(inp: INPUT) -> None:
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def mouse_click_screen(x: int, y: int) -> None:
        screen_w = win32api.GetSystemMetrics(0)
        screen_h = win32api.GetSystemMetrics(1)
        ax = int(x * 65536 / screen_w)
        ay = int(y * 65536 / screen_h)
        for flags in (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                      MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.union.mi.dx = ax
            inp.union.mi.dy = ay
            inp.union.mi.mouseData = 0
            inp.union.mi.dwFlags = flags
            inp.union.mi.time = 0
            inp.union.mi.dwExtraInfo = None
            send_input(inp)
            time.sleep(0.05)

    def key_vk(vk: int, down: bool) -> None:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.wScan = 0
        inp.union.ki.dwFlags = 0 if down else KEYEVENTF_KEYUP
        inp.union.ki.time = 0
        inp.union.ki.dwExtraInfo = None
        send_input(inp)
        time.sleep(0.02)

    def type_unicode(text: str) -> None:
        arr = (INPUT * len(text))()
        for i, ch in enumerate(text):
            arr[i].type = INPUT_KEYBOARD
            arr[i].union.ki.wVk = 0
            arr[i].union.ki.wScan = ord(ch)
            arr[i].union.ki.dwFlags = KEYEVENTF_UNICODE
            arr[i].union.ki.time = 0
            arr[i].union.ki.dwExtraInfo = None
        user32.SendInput(len(text), ctypes.byref(arr), ctypes.sizeof(INPUT))
        time.sleep(0.1)

    # ① 置前台（AttachThreadInput + ALT 技巧绕过前台锁）
    target_tid = user32.GetWindowThreadProcessId(top_hwnd, None)
    my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(my_tid, target_tid, True)
    user32.BringWindowToTop(top_hwnd)
    user32.SetForegroundWindow(top_hwnd)
    user32.AttachThreadInput(my_tid, target_tid, False)
    key_vk(VK_MENU, True)
    key_vk(VK_MENU, False)
    user32.SetForegroundWindow(top_hwnd)
    time.sleep(0.4)

    # ② 真实鼠标点击目标控件中心
    rect = win32gui.GetWindowRect(target_hwnd)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    mouse_click_screen(cx, cy)
    time.sleep(0.5)

    # ③ Ctrl+A 全选（覆盖旧内容）
    key_vk(VK_CONTROL, True)
    key_vk(VK_A, True)
    key_vk(VK_A, False)
    key_vk(VK_CONTROL, False)
    time.sleep(0.2)

    # ④ 输入文本
    type_unicode(text)
    time.sleep(0.3)


# ============================================================
# 真实鼠标点击 / 前台 / 窗口安全移位（模块级工具）
# ============================================================

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_INPUT_MOUSE = 0
_KEYEVENTF_KEYUP = 0x0002
_VK_MENU = 0x12


def real_click_screen(x: int, y: int) -> None:
    """真实鼠标点击屏幕坐标（SendInput，需进程 DPI-aware 后使用物理像素坐标）"""
    screen_w = win32api.GetSystemMetrics(0)
    screen_h = win32api.GetSystemMetrics(1)
    ax = int(x * 65536 / screen_w)
    ay = int(y * 65536 / screen_h)
    for flags in (_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE,
                  _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP):
        inp = _INPUT()
        inp.type = _INPUT_MOUSE
        inp.union.mi.dx = ax
        inp.union.mi.dy = ay
        inp.union.mi.mouseData = 0
        inp.union.mi.dwFlags = flags
        inp.union.mi.time = 0
        inp.union.mi.dwExtraInfo = None
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.05)


def real_click_control(control_hwnd: int) -> None:
    """真实鼠标点击控件中心（GetWindowRect 物理坐标 + SendInput）"""
    rect = win32gui.GetWindowRect(control_hwnd)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    real_click_screen(cx, cy)


def foreground_window(hwnd: int) -> None:
    """把窗口置为前台（AttachThreadInput + ALT 技巧绕过前台锁）"""
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(my_tid, target_tid, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(my_tid, target_tid, False)
    # ALT 技巧
    inp = _INPUT()
    inp.type = 1
    inp.union.ki.wVk = _VK_MENU
    inp.union.ki.wScan = 0
    inp.union.ki.dwFlags = 0
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = None
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    inp.union.ki.dwFlags = _KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)


def move_window_safe(hwnd: int) -> None:
    """把窗口移到屏幕左侧 40% 之外的安全位置（避免被左侧常驻窗口遮挡），再置前台。

    雷小伊登录/换装需要真实鼠标键盘输入，若窗口被其他窗口遮挡会输入失败。
    """
    screen_w = win32api.GetSystemMetrics(0)
    rect = win32gui.GetWindowRect(hwnd)
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    x = int(screen_w * 0.42)
    y = 60
    win32gui.SetWindowPos(hwnd, 0, x, y, width, height, 0x0004)  # SWP_NOZORDER
    time.sleep(0.5)
    foreground_window(hwnd)


# ============================================================
# 窗口位置和大小
# ============================================================

def set_window_pos(hwnd: int, x: int, y: int, width: int = 0, height: int = 0) -> bool:
    """设置窗口位置和大小"""
    flags = 0x0001  # SWP_NOSIZE（如果不指定大小）
    if width > 0 and height > 0:
        flags = 0
    else:
        flags = 0x0001  # 不改变大小

    return bool(user32.SetWindowPos(
        hwnd,
        0,  # HWND_TOP
        x, y,
        width, height,
        flags,
    ))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """获取窗口矩形（left, top, right, bottom）"""
    return win32gui.GetWindowRect(hwnd)


def set_window_topmost(hwnd: int) -> bool:
    """仅置顶窗口（不移动不改变大小；TOPMOST 置顶在批量/遮挡场景比 SetForegroundWindow 可靠）"""
    return bool(user32.SetWindowPos(
        hwnd,
        -1,  # HWND_TOPMOST
        0, 0, 0, 0,
        0x0001 | 0x0002 | 0x0040,  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    ))


def tile_window(hwnd: int, col: int, row: int,
                start_x: int, start_y: int,
                spacing_x: int = 0, spacing_y: int = 0) -> bool:
    """
    平铺排列窗口

    Args:
        hwnd: 窗口句柄
        col: 列号（1-based）
        row: 行号（1-based）
        start_x: 起始X坐标（右上角）
        start_y: 起始Y坐标（右上角）
        spacing_x: 列间距
        spacing_y: 行间距
    """
    x = start_x - (col - 1) * spacing_x
    y = start_y - (row - 1) * spacing_y
    return set_window_pos(hwnd, max(0, x), max(0, y))


# ============================================================
# 工具函数
# ============================================================

def delay(ms: int) -> None:
    """毫秒延迟"""
    time.sleep(ms / 1000.0)


def check_box_set(hwnd: int, checked: bool) -> bool:
    """设置复选框状态"""
    BM_SETCHECK = 0x00F1
    return bool(user32.SendMessageW(hwnd, BM_SETCHECK, 1 if checked else 0, 0))


def is_check_box_checked(hwnd: int) -> bool:
    """获取复选框状态"""
    BM_GETCHECK = 0x00F0
    result = user32.SendMessageW(hwnd, BM_GETCHECK, 0, 0)
    return result == 1  # BST_CHECKED = 1
