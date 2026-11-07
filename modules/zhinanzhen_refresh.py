"""打开指南针让它更新活跃市值（0AMV），更新到目标交易日后正常退出。

- 只打开再关闭不会更新（2026-11-03 试了 4 次）：要在指南针里打开 0AMV 页面，它才去下载
  0AMV 日线（日志里的 1840/1841 请求）并写进 ``Z_SK/day.vdat``。本脚本等启动完成后，往右下角的
  命令框（主窗口里唯一的 Edit 控件）发「0AMV」和回车；
- 所有操作都是发给指南针自己窗口的消息：不抢键盘焦点、不往别的窗口打字；
- 由本脚本打开时：输入 0AMV 后等十几秒就正常退出，退出后再读文件（开着时文件被独占）；
- 由本脚本打开的指南针跑在一个看不见的桌面上（CreateDesktop，2026-11-05 实测）：启动窗口、连接状态、
  退出确认弹窗都不会弹到前台，也不抢焦点；它顺带拉起的指南通（IMMainV2 / ZnzBrowser）退出后会留在那个
  桌面上，先发关闭消息，不退再结束（只结束这次在隐藏桌面上启动的进程）。建不了隐藏桌面时照旧在前台打开。
  设 ZHINANZHEN_VISIBLE=1 可以改回前台打开（排查问题时用）；
- 指南针已经开着（用户在用）就不动它，只等数据；
- 由本脚本打开的，结束时点主窗口关闭，再在「您真的要退出指南针吗?」弹窗里点「退出(Y)」，
  不强杀进程；
- 只看 ``Z_SK/day.vdat`` 里 0AMV 的最新日期，不改指南针的任何文件和设置。

Windows 专用（ctypes 调 user32）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

def wavmain_exe() -> Path | None:
    """指南针主程序：ZHINANZHEN_EXE；没设时按 ZHINANZHEN_DATA_DIR（…/WavMain/ANALYSE/Data/ChinaStk）往上三级找 WavMain.exe。"""
    explicit = os.environ.get("ZHINANZHEN_EXE", "").strip()
    if explicit:
        return Path(explicit)
    data_dir = os.environ.get("ZHINANZHEN_DATA_DIR", "").strip()
    return Path(data_dir).parents[2] / "WavMain.exe" if data_dir else None
EXIT_BUTTON = "退出(&Y)"
AMV_COMMAND = "0AMV"
BM_CLICK = 0x00F5
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
VK_RETURN = 0x0D
#: 启动后等这么久再输入：先让登录和启动下载跑完（实测约 6 秒）
STARTUP_SETTLE = 12
#: 打开 0AMV 页面后等这么久就退出（数据几秒就下完）
AMV_PAGE_WAIT = 15
#: 第一次退出后还没更新时，再开一次等这么久
AMV_RETRY_WAIT = 30


@dataclass
class RefreshResult:
    target: str
    before: str
    after: str
    started_by_us: bool
    closed: bool
    waited_seconds: int
    error: str = ""
    amv_page_opened: bool = False

    @property
    def updated(self) -> bool:
        return self.after >= self.target


def read_amv_date() -> tuple[str, bool]:
    """(0AMV 最新一行的日期 YYYY-MM-DD, 文件被占用)。读不到日期为空串。

    指南针开着时独占 Z_SK/day.vdat，读会报 PermissionError（2026-11-04 晚实测，整段 180 秒都读不了），
    这不是没有数据，调用方等它退出后再读。
    """
    from modules import compass_vdat

    try:
        rows = compass_vdat.read_amv_rows()
    except PermissionError:
        return "", True
    except Exception as e:  # noqa: BLE001 — 读失败按「没有数据」处理，原因记日志
        logger.warning("读取活跃市值失败: %s", e)
        return "", False
    return (rows[-1]["date"] if rows else ""), False


def latest_amv_date() -> str:
    """0AMV 最新一行的日期（YYYY-MM-DD）；读不到（含指南针占着文件）为空串。"""
    return read_amv_date()[0]


def amv_file_mtime() -> float:
    """day.vdat 的修改时间（秒）；没配置或读不到为 0。指南针占着文件时也能读到修改时间。"""
    from modules import compass_vdat

    root = compass_vdat.configured_data_dir()
    try:
        return (root / "Z_SK" / "day.vdat").stat().st_mtime if root is not None else 0.0
    except OSError:
        return 0.0


# ───────────────────────── 窗口操作（user32） ─────────────────────────

_user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
_ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if _user32 else None
if _user32 is not None:
    # 64 位下句柄超出 int 范围，必须声明参数类型
    _user32.EnumWindows.argtypes = [_ENUM, wintypes.LPARAM]
    _user32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM, wintypes.LPARAM]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, _ENUM, wintypes.LPARAM]
    _user32.CreateDesktopW.restype = wintypes.HANDLE
    _user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p]
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]


def _text(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


#: 本次在隐藏桌面上打开指南针时的桌面句柄；None 表示在用户桌面上
_desktop = None
HIDDEN_DESKTOP = "zettaranc-amv"


def _desktop_windows() -> list[tuple[int, int]]:
    """当前操作的桌面上可见的顶层窗口：[(hwnd, 进程号)]。"""
    found: list[tuple[int, int]] = []

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if _user32.IsWindowVisible(hwnd):
            found.append((hwnd, owner.value))
        return True

    if _desktop is not None:
        _user32.EnumDesktopWindows(_desktop, _ENUM(cb), 0)
    else:
        _user32.EnumWindows(_ENUM(cb), 0)
    return found


def _top_windows(pid: int) -> list[int]:
    return [hwnd for hwnd, owner in _desktop_windows() if owner == pid]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


def _launch(exe: Path) -> int:
    """打开指南针，返回进程号。优先放到隐藏桌面上（不弹到前台）；建不了就在用户桌面上打开。"""
    global _desktop
    _desktop = None
    if os.environ.get("ZHINANZHEN_VISIBLE", "").strip() not in ("1", "true", "yes"):
        try:
            desk = _user32.CreateDesktopW(HIDDEN_DESKTOP, None, None, 0, 0x01FF | 0x10000000, None)  # DESKTOP_ALL | GENERIC_ALL
            if desk:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                si = _STARTUPINFOW()
                si.cb = ctypes.sizeof(si)
                si.lpDesktop = HIDDEN_DESKTOP
                pi = _PROCESS_INFORMATION()
                if kernel32.CreateProcessW(str(exe), None, None, None, False, 0, None, str(exe.parent),
                                           ctypes.byref(si), ctypes.byref(pi)):
                    kernel32.CloseHandle(pi.hThread)
                    kernel32.CloseHandle(pi.hProcess)
                    _desktop = desk
                    return int(pi.dwProcessId)
                _user32.CloseDesktop(desk)
            logger.warning("建不了隐藏桌面（错误 %s），指南针改在前台打开", ctypes.get_last_error())
        except (OSError, AttributeError) as e:
            logger.warning("建不了隐藏桌面（%s），指南针改在前台打开", e)
    return subprocess.Popen([str(exe)], cwd=str(exe.parent)).pid


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def _child_pids(parent: int) -> list[int]:
    """父进程号是 parent 的进程（父进程退出后也查得到）。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out: list[int] = []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == parent and entry.th32ProcessID != parent:
                out.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _finish_desktop(pid: int, closed: bool) -> bool:
    """隐藏桌面收尾：指南针没正常退出就结束它；留在桌面上的指南通等先发关闭、不退再结束；关掉桌面。

    返回指南针是否已经退出。在用户桌面上打开的（_desktop 为 None）什么都不做。
    """
    global _desktop
    if _desktop is None:
        return closed
    if not closed and pid in _wavmain_pids():
        logger.warning("指南针没有正常退出（隐藏桌面上看不见），结束进程")
        _kill_tree(pid)
        closed = pid not in _wavmain_pids()
    # 指南针拉起的指南通（IMMainV2 → ZnzBrowser）不随它退出，窗口也不可见：按父进程找出来，
    # 先给隐藏桌面上的窗口发关闭，几秒后还在就连同子进程结束。只动这次启动的指南针的子进程。
    helpers = _child_pids(pid)
    for hwnd, owner in _desktop_windows():
        if owner in helpers:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(6):
        if not set(helpers) & set(_child_pids(pid)):
            break
        time.sleep(0.5)
    for helper in set(helpers) & set(_child_pids(pid)):
        _kill_tree(helper)
    _user32.CloseDesktop(_desktop)
    _desktop = None
    return closed


def _children(hwnd) -> list[int]:
    found: list[int] = []

    def cb(child, _):
        found.append(child)
        return True

    _user32.EnumChildWindows(hwnd, _ENUM(cb), 0)
    return found


def _wavmain_pids() -> list[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq WavMain.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace", check=False,
    ).stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].strip('"').isdigit():
            pids.append(int(parts[1].strip('"')))
    return pids


def _main_window(pid: int) -> int | None:
    for hwnd in _top_windows(pid):
        if _class(hwnd) != "#32770" and _text(hwnd).startswith("指南针"):
            return hwnd
    return None


def window_title(pid: int) -> str:
    hwnd = _main_window(pid)
    return _text(hwnd) if hwnd else ""


def open_amv_page(pid: int, timeout: float = 60) -> bool:
    """在命令框里输入 0AMV 回车，打开活跃市值页面。返回窗口标题是否切到了 0AMV。"""
    deadline = time.time() + timeout
    edit = None
    while time.time() < deadline and edit is None:
        main = _main_window(pid)
        if main:
            edits = [c for c in _children(main) if _class(c) == "Edit" and _user32.IsWindowVisible(c)]
            edit = edits[0] if len(edits) == 1 else None
        if edit is None:
            time.sleep(1)
    if edit is None:
        logger.warning("没找到指南针命令框")
        return False
    for ch in AMV_COMMAND:
        _user32.PostMessageW(edit, WM_CHAR, ord(ch), 1)
        time.sleep(0.15)
    time.sleep(1.2)  # 等键盘精灵列出候选
    _user32.PostMessageW(edit, WM_KEYDOWN, VK_RETURN, 0x001C0001)
    _user32.PostMessageW(edit, WM_CHAR, VK_RETURN, 0x001C0001)
    _user32.PostMessageW(edit, WM_KEYUP, VK_RETURN, 0xC01C0001)
    for _ in range(10):
        time.sleep(1)
        if AMV_COMMAND in window_title(pid):
            return True
    return False


def _close_gracefully(pid: int, timeout: float = 60) -> bool:
    """关主窗口 → 等确认弹窗 → 点「退出(Y)」→ 等进程退出。"""
    for hwnd in _top_windows(pid):
        if _class(hwnd) != "#32770":
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        if pid not in _wavmain_pids():
            return True
        if not clicked:
            for hwnd in _top_windows(pid):
                if _class(hwnd) == "#32770":
                    for child in _children(hwnd):
                        if _text(child) == EXIT_BUTTON:
                            result = ctypes.c_size_t()
                            _user32.SendMessageTimeoutW(child, BM_CLICK, 0, 0, 2, 3000, ctypes.byref(result))
                            clicked = True
        time.sleep(1)
    return pid not in _wavmain_pids()


# ───────────────────────── 主流程 ─────────────────────────


def _read_after_exit(after: str, target: str, tries: int = 10) -> str:
    """指南针退出后文件才放开：再读几次，以退出后读到的日期为准。"""
    for _ in range(tries):
        if after >= target:
            break
        date, locked = read_amv_date()
        after = max(after, date)
        if not locked:
            break
        time.sleep(1)
    return after


def refresh_amv(target: str, wait_seconds: int = 180, exe: Path | None = None,
                page_wait: int = AMV_PAGE_WAIT, retry_wait: int = AMV_RETRY_WAIT) -> RefreshResult:
    """target：期望活跃市值更新到的日期（YYYY-MM-DD），通常是当天。

    由本脚本打开指南针时（2026-11-05 改）：启动 → 输入 0AMV → 等 ``page_wait`` 秒 → 正常退出 → 读文件。
    指南针开着时独占 day.vdat、退出时才写完整，所以不再边开着边等（以前要空等满 180 秒）。
    第一次退出后还没到 target，再开一次、等 ``retry_wait`` 秒。
    指南针本来就开着（用户在用）时不动它，只按 ``wait_seconds`` 轮询。
    """
    before = latest_amv_date()
    if before >= target:
        return RefreshResult(target, before, before, False, False, 0)
    start = time.time()
    after = before
    if _wavmain_pids():
        while time.time() - start < wait_seconds:
            after = max(after, read_amv_date()[0])
            if after >= target:
                break
            time.sleep(3)
        waited = int(time.time() - start)
        error = "" if after >= target else f"等了 {waited} 秒，活跃市值还停在 {after or '无数据'}（指南针本来就开着，没有动它）"
        return RefreshResult(target, before, after, False, False, waited, error)
    exe = exe or wavmain_exe()
    if exe is None:
        return RefreshResult(target, before, before, False, False, 0, "没有配置指南针（.env 里的 ZHINANZHEN_DATA_DIR 或 ZHINANZHEN_EXE）")
    if not exe.is_file():
        return RefreshResult(target, before, before, False, False, 0, f"找不到指南针：{exe}")
    opened = closed = False
    rounds = 0
    for wait in (page_wait, retry_wait):
        rounds += 1
        pid = _launch(exe)
        try:
            time.sleep(STARTUP_SETTLE)
            opened = open_amv_page(pid)
            time.sleep(wait)
            closed = _close_gracefully(pid)
        finally:
            closed = _finish_desktop(pid, closed)
        after = _read_after_exit(after, target)
        if after >= target or not closed:
            break  # 没能正常退出就不再开第二个
    waited = int(time.time() - start)
    error = ""
    if after < target:
        error = f"开了 {rounds} 次指南针、共 {waited} 秒，活跃市值还停在 {after or '无数据'}"
        if not opened:
            error += "（没能打开 0AMV 页面）"
        elif not closed:
            error += "（指南针没有正常退出）"
    return RefreshResult(target, before, after, True, closed, waited, error, opened)
