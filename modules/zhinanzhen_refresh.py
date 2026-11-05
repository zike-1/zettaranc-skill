"""打开指南针让它更新活跃市值（0AMV），更新到目标交易日后正常退出。

- 只打开再关闭不会更新（2026-11-03 试了 4 次）：要在指南针里打开 0AMV 页面，它才去下载
  0AMV 日线（日志里的 1840/1841 请求）并写进 ``Z_SK/day.vdat``。本脚本等启动完成后，往右下角的
  命令框（主窗口里唯一的 Edit 控件）发「0AMV」和回车；
- 所有操作都是发给指南针自己窗口的消息：不抢键盘焦点、不往别的窗口打字；
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


def _top_windows(pid: int) -> list[int]:
    found: list[int] = []

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_ENUM(cb), 0)
    return found


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


def refresh_amv(target: str, wait_seconds: int = 180, exe: Path | None = None) -> RefreshResult:
    """target：期望活跃市值更新到的日期（YYYY-MM-DD），通常是当天。"""
    before = latest_amv_date()
    if before >= target:
        return RefreshResult(target, before, before, False, False, 0)
    running = _wavmain_pids()
    pid = None
    exe = exe or wavmain_exe()
    if not running:
        if exe is None:
            return RefreshResult(target, before, before, False, False, 0, "没有配置指南针（.env 里的 ZHINANZHEN_DATA_DIR 或 ZHINANZHEN_EXE）")
        if not exe.is_file():
            return RefreshResult(target, before, before, False, False, 0, f"找不到指南针：{exe}")
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        pid = proc.pid
    start = time.time()
    opened = False
    page_at = start
    if pid is not None:
        time.sleep(STARTUP_SETTLE)
        page_at = time.time()
        opened = open_amv_page(pid)
    after = before
    while time.time() - start < wait_seconds:
        date, locked = read_amv_date()
        after = max(after, date)
        if after >= target:
            break
        # 指南针占着文件读不了内容，但打开 0AMV 页面之后文件被写过，说明数据已经下来了：
        # 不必空等到超时，退出指南针后再读确认
        if locked and pid is not None and amv_file_mtime() >= page_at:
            break
        time.sleep(3)
    waited = int(time.time() - start)
    closed = False
    if pid is not None:
        time.sleep(3)  # 数据刚写完，留一点时间收尾
        closed = _close_gracefully(pid)
    # 指南针退出后文件才放开：再读几次，以退出后读到的日期为准
    for _ in range(10):
        if after >= target:
            break
        date, locked = read_amv_date()
        after = max(after, date)
        if not locked:
            break
        time.sleep(1)
    error = ""
    if after < target:
        error = f"等了 {waited} 秒，活跃市值还停在 {after or '无数据'}"
        if pid is not None and not opened:
            error += "（没能打开 0AMV 页面）"
        elif pid is None:
            error += "（指南针本来就开着，没有动它）"
    return RefreshResult(target, before, after, pid is not None, closed, waited, error, opened)
