# -*- coding: utf-8 -*-
"""屏幕中央的原生 Win32 分层窗口启动动画（不依赖 WebView2 / .NET）。

背景：双击 exe 后 WebView2 与 .NET 初始化要 1~3 秒，旧方案「先开 WebView2
窗口再在里面播 CSS 动画」必然慢半拍。这里用纯 ctypes 建一个
WS_EX_LAYERED + UpdateLayeredWindow 的 per-pixel alpha 窗口：
  - 第一帧只需解一张 PNG（约 10ms），双击后几乎立刻出现在屏幕正中；
  - 无边框、置顶、不抢焦点、不进任务栏（WS_EX_TOOLWINDOW + NOACTIVATE）；
  - 卡片外完全透明，桌面壁纸透出来；
  - 开盒帧索引由「实际加载进度」驱动：加载慢 -> 盒子开得慢，加载快 -> 开得快；
  - 加载完成（服务 + 隐藏窗口 + 页面预加载三者齐备）才播闪光终场，
    终场 55% 处发 reveal_event 让 launcher 显示主窗口，最后整体淡出。

数据帧由 make_splash_frames.py 预渲染，放在 desktop/splash_frames/，
frozen 态经 PyInstaller datas 落到 _MEIPASS/desktop/splash_frames/。
"""
from __future__ import annotations

import ctypes as C
import json
import math
import os
import threading
import time
from ctypes import wintypes as WT
from pathlib import Path

# ---------------------------------------------------------------- Win32 定义
if os.name == "nt":
    user32 = C.WinDLL("user32", use_last_error=True)
    gdi32 = C.WinDLL("gdi32", use_last_error=True)
    kernel32 = C.WinDLL("kernel32", use_last_error=True)

    WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_uint,
                            C.c_size_t, C.c_ssize_t)

    class WNDCLASSW(C.Structure):
        _fields_ = [
            ("style", C.c_uint), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int),
            ("hInstance", C.c_void_p), ("hIcon", C.c_void_p),
            ("hCursor", C.c_void_p), ("hbrBackground", C.c_void_p),
            ("lpszMenuName", C.c_wchar_p), ("lpszClassName", C.c_wchar_p),
        ]

    class BITMAPINFOHEADER(C.Structure):
        _fields_ = [
            ("biSize", C.c_uint), ("biWidth", C.c_int), ("biHeight", C.c_int),
            ("biPlanes", C.c_ushort), ("biBitCount", C.c_ushort),
            ("biCompression", C.c_uint), ("biSizeImage", C.c_uint),
            ("biXPelsPerMeter", C.c_int), ("biYPelsPerMeter", C.c_int),
            ("biClrUsed", C.c_uint), ("biClrImportant", C.c_uint),
        ]

    class BITMAPINFO(C.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", C.c_uint * 3)]

    class POINT(C.Structure):
        _fields_ = [("x", C.c_long), ("y", C.c_long)]

    class SIZE(C.Structure):
        _fields_ = [("cx", C.c_long), ("cy", C.c_long)]

    class BLENDFUNCTION(C.Structure):
        _fields_ = [("BlendOp", C.c_ubyte), ("BlendFlags", C.c_ubyte),
                    ("SourceConstantAlpha", C.c_ubyte),
                    ("AlphaFormat", C.c_ubyte)]

    user32.CreateWindowExW.argtypes = [
        C.c_uint, C.c_wchar_p, C.c_wchar_p, C.c_uint,
        C.c_int, C.c_int, C.c_int, C.c_int,
        C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p,
    ]
    user32.CreateWindowExW.restype = C.c_void_p
    user32.DefWindowProcW.argtypes = [C.c_void_p, C.c_uint,
                                      C.c_size_t, C.c_ssize_t]
    user32.DefWindowProcW.restype = C.c_ssize_t
    user32.UpdateLayeredWindow.argtypes = [
        C.c_void_p, C.c_void_p, C.POINTER(POINT), C.POINTER(SIZE),
        C.c_void_p, C.POINTER(POINT), C.c_uint,
        C.POINTER(BLENDFUNCTION), C.c_uint,
    ]
    user32.UpdateLayeredWindow.restype = C.c_int
    gdi32.CreateDIBSection.argtypes = [
        C.c_void_p, C.c_void_p, C.c_uint,
        C.POINTER(C.c_void_p), C.c_void_p, C.c_uint,
    ]
    gdi32.CreateDIBSection.restype = C.c_void_p

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01
PM_REMOVE = 0x0001
WM_DESTROY, WM_CLOSE, WM_QUIT = 0x0002, 0x0010, 0x0012
SW_SHOWNOACTIVATE = 4

MIN_OPEN_SECONDS = 1.10   # 加载极快时也至少让开盒动画可见这么久
REVEAL_AT = 0.55          # 闪光终场进度到 55% 时显示主窗口
FPS = 30.0


def ensure_dpi_aware() -> None:
    """必须在任何窗口 API 之前调用：否则 GetSystemMetrics 给逻辑像素、
    UpdateLayeredWindow 用物理像素，浮层会偏移且尺寸不对。"""
    if os.name != "nt":
        return
    try:
        user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _premul_bgra(path: Path, size: int) -> bytes:
    """PNG(RGBA) -> 预乘 alpha 的 BGRA 字节，供 UpdateLayeredWindow 使用。

    Pillow 在这里才导入：首帧走 first.bgra 原始字节，不用等 Pillow 初始化。
    """
    from PIL import Image, ImageChops  # 延迟导入，缩短「双击 -> 首帧」

    with Image.open(path) as src:
        im = src.convert("RGBA")
        if im.size != (size, size):
            im = im.resize((size, size), Image.LANCZOS)
        r, g, b, a = im.split()
        return Image.merge(
            "RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a),
                     ImageChops.multiply(r, a), a)
        ).tobytes()


class NativeSplash:
    """进度驱动的屏幕中央浮层；start() 返回 False 时由 launcher 回退旧动画页。"""

    def __init__(self, state, frames_dir: Path, log=print) -> None:
        self.state = state
        self.frames_dir = Path(frames_dir)
        self.log = log
        self.reveal_event = threading.Event()
        self.finished = threading.Event()
        self.failed = threading.Event()
        self.first_frame = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._wndproc = None          # 防 GC：Win32 回调必须保持引用
        self._bits: C.c_void_p | None = None
        self._hbmp = None
        self._memdc = None
        self._cur_idx = -1
        self._cur_data: bytes | None = None
        self._t_first: float | None = None

        meta = json.loads((self.frames_dir / "manifest.json").read_text("utf-8"))
        self.size = int(meta["size"])
        self.n_open = int(meta["open"])
        self.n_flash = int(meta["flash"])
        self.flash_seconds = float(meta.get("flash_seconds", 0.95))
        self.first_raw = self.frames_dir / meta.get("first_raw", "first.bgra")
        self._first_bytes: bytes | None = None

    # ------------------------------------------------------------ 对外接口
    def start(self) -> bool:
        if os.name != "nt":
            self.log("原生浮层: 非 Windows，跳过")
            return False
        if not (self.frames_dir / "open_000.png").is_file():
            self.log(f"原生浮层: 缺帧目录 {self.frames_dir}")
            return False
        ensure_dpi_aware()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="blindbox-native-splash")
        self._thread.start()
        self.first_frame.wait(3.0)
        return self._t_first is not None and not self.failed.is_set()

    def close(self) -> None:
        self._stop.set()
        # 浮层崩溃/被跳过时也要放行主窗口，避免 launcher 永久等待
        self.reveal_event.set()

    # ------------------------------------------------------------ 内部实现
    def _frame_path(self, idx: int) -> Path:
        if idx < self.n_open:
            return self.frames_dir / f"open_{idx:03d}.png"
        return self.frames_dir / f"flash_{idx - self.n_open:03d}.png"

    def _run(self) -> None:
        try:
            self._create_window()
            self._t_first = time.time()
            self.first_frame.set()
            self.log(f"原生浮层首帧上屏 @ {time.strftime('%H:%M:%S')} "
                     f"(尺寸 {self.size}x{self.size}, 屏幕正中)")
            self._pump()
        except Exception as e:
            import traceback
            self.log(f"原生浮层失败，回退内嵌动画页: {type(e).__name__}: {e}\n"
                     + traceback.format_exc(limit=5))
            self.failed.set()
            self.first_frame.set()
        finally:
            self._destroy_window()
            self.reveal_event.set()      # 兜底：绝不卡住主窗口显示
            self.finished.set()

    def _create_window(self) -> None:
        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(C.c_void_p(hwnd))
                return 0
            return user32.DefWindowProcW(C.c_void_p(hwnd), msg,
                                         wparam, lparam)

        self._wndproc = wndproc
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = wndproc
        wc.hInstance = C.c_void_p(hinst)
        wc.lpszClassName = "AIBlindBoxNativeSplash"
        wc.hCursor = C.c_void_p(user32.LoadCursorW(None, 32512))
        if not user32.RegisterClassW(C.byref(wc)) and C.get_last_error() != 1410:
            raise OSError(f"RegisterClassW 失败: {C.get_last_error()}")

        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self._pos = POINT((sw - self.size) // 2, (sh - self.size) // 2)
        self._size = SIZE(self.size, self.size)
        self._src = POINT(0, 0)
        self._blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            "AIBlindBoxNativeSplash", "AI 盲盒", WS_POPUP,
            self._pos.x, self._pos.y, self.size, self.size,
            None, None, C.c_void_p(hinst), None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowExW 失败: {C.get_last_error()}")

        hdc = user32.GetDC(None)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = C.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.size
        bmi.bmiHeader.biHeight = -self.size      # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0         # BI_RGB
        self._bits = C.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(
            C.c_void_p(hdc), C.byref(bmi), 0, C.byref(self._bits), None, 0)
        if not self._hbmp or not self._bits.value:
            raise OSError(f"CreateDIBSection 失败: {C.get_last_error()}")
        self._memdc = gdi32.CreateCompatibleDC(C.c_void_p(hdc))
        gdi32.SelectObject(C.c_void_p(self._memdc), C.c_void_p(self._hbmp))
        user32.ReleaseDC(None, C.c_void_p(hdc))

        self._draw(0, 255)
        user32.ShowWindow(C.c_void_p(self._hwnd), SW_SHOWNOACTIVATE)
        self._draw(0, 255)

    def _draw(self, idx: int, alpha: int) -> None:
        if idx != self._cur_idx:
            self._cur_data = None
            need = self.size * self.size * 4
            # 首帧优先用预生成的原始 BGRA，避免 Pillow 导入 + PNG 解码
            if idx == 0 and self.first_raw.is_file():
                raw = self.first_raw.read_bytes()
                self._cur_data = raw if len(raw) == need else None
            if self._cur_data is None:
                self._cur_data = _premul_bgra(self._frame_path(idx), self.size)
            self._cur_idx = idx
        C.memmove(self._bits.value, self._cur_data, len(self._cur_data))
        self._blend.SourceConstantAlpha = max(0, min(255, int(alpha)))
        ok = user32.UpdateLayeredWindow(
            C.c_void_p(self._hwnd), None, C.byref(self._pos),
            C.byref(self._size), C.c_void_p(self._memdc),
            C.byref(self._src), 0, C.byref(self._blend), ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow 失败: {C.get_last_error()}")

    def _pump(self) -> None:
        msg = WT.MSG()
        t0 = time.time()
        shown_p = 0.0
        phase = "opening"
        flash_t0 = 0.0
        revealed = False
        last_idx, last_alpha = -1, -1
        dt = 1.0 / FPS

        while not self._stop.is_set():
            while user32.PeekMessageW(C.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self._stop.set()
                    break
                user32.TranslateMessage(C.byref(msg))
                user32.DispatchMessageW(C.byref(msg))
            if self._stop.is_set():
                break

            now = time.time()
            snap = self.state.snapshot()
            if snap["status"] == "error":
                self.log("原生浮层: 后台服务启动失败，结束动画")
                break

            if phase == "opening":
                # 目标进度 = max(实际阶段进度, 极缓的时间兜底)。
                # 阶段进度是主驱动（加载快 -> 盒子开得快），时间兜底只保证
                # 某阶段长时间卡住时画面不至于完全静止，不能反过来盖过阶段值。
                creep = min(0.85, (now - t0) * 0.030)
                target = max(snap["progress"], creep)
                rate = 2.2 if snap["all_ready"] else 1.6
                delta = max(-dt * 0.05, min(dt * rate, target - shown_p))
                shown_p = max(0.0, min(1.0, shown_p + delta))
                idx = min(self.n_open - 1, int(shown_p * (self.n_open - 1)))
                if (snap["all_ready"] and shown_p >= 0.999
                        and (now - t0) >= MIN_OPEN_SECONDS):
                    phase = "flash"
                    flash_t0 = now
                    self.log(f"加载完成 @ +{now - t0:.2f}s -> 开始闪光终场")
            if phase == "flash":
                t = (now - flash_t0) / self.flash_seconds
                idx = self.n_open + min(
                    self.n_flash - 1, max(0, int(t * (self.n_flash - 1))))
                if t >= REVEAL_AT and not revealed:
                    revealed = True
                    self.reveal_event.set()
                    self.log(f"闪光 {int(REVEAL_AT * 100)}% -> 通知显示主窗口 "
                             f"@ +{now - t0:.2f}s")
                if t >= 1.0:
                    self.log(f"浮层淡出完成 @ +{now - t0:.2f}s")
                    break

            # 轻微呼吸感：即使进度停住，整层也有极淡的亮度起伏
            alpha = 246 + int(9 * (0.5 + 0.5 * math.sin((now - t0) * 2.6)))
            if idx != last_idx or abs(alpha - last_alpha) >= 3:
                self._draw(idx, alpha)
                last_idx, last_alpha = idx, alpha
            time.sleep(dt)

    def _destroy_window(self) -> None:
        try:
            if self._hwnd:
                user32.DestroyWindow(C.c_void_p(self._hwnd))
        except Exception:
            pass
        try:
            if self._memdc:
                gdi32.DeleteDC(C.c_void_p(self._memdc))
            if self._hbmp:
                gdi32.DeleteObject(C.c_void_p(self._hbmp))
        except Exception:
            pass
        self._hwnd = None
