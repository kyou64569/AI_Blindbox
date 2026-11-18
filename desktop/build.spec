# -*- mode: python ; coding: utf-8 -*-
"""AI 盲盒桌面版 PyInstaller spec（基于 6.22 官方模板改造）。

构建：
  backend\\.venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean desktop\\build.spec
或双击 desktop\\build.cmd；设 BLINDBOX_ONEFILE=1 则产出单文件 exe。

产物：
  onedir  -> desktop\\dist\\AIBlindBox\\AIBlindBox.exe
  onefile -> desktop\\dist\\AIBlindBox.exe

图标：desktop/app.ico 由 desktop/make_icon.py 生成（9 个尺寸，32bpp DIB）。
  - EXE(icon=...) 写入 PE 图标资源 -> 资源管理器里 exe 的图标、任务栏分组图标
  - 同时放进 datas -> launcher 用 webview.start(icon=...) 设置窗口左上角与任务栏图标
  .ico 必须是标准 DIB 条目：pywebview 在 Windows 用 .NET System.Drawing.Icon(path) 读取。

为什么要手动带这些 datas：PyInstaller 没有 pywebview / pythonnet / clr_loader 的 hook，
而 pywebview 用 os.path.dirname(__file__)/'js' 与 /'lib/<dll>' 定位资源，pythonnet 与
clr_loader 也按包内相对路径加载 .NET 程序集。缺任何一个，frozen 态的 WebView2 窗口
会起不来（launcher 会自动降级到 Edge --app 模式，但那不是内嵌窗口）。
"""
import os
import sys
from pathlib import Path

import clr_loader
import pythonnet
import webview
from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
ICON = SPEC_DIR / "app.ico"
FRAMES = SPEC_DIR / "splash_frames"

if not ICON.is_file():
    raise SystemExit(
        f"缺少图标 {ICON}\n"
        f"请先运行: {BACKEND / '.venv' / 'Scripts' / 'python.exe'} {SPEC_DIR / 'make_icon.py'}"
    )
if not (FRAMES / "manifest.json").is_file():
    raise SystemExit(
        f"缺少启动动画帧 {FRAMES}\n"
        f"请先运行: {BACKEND / '.venv' / 'Scripts' / 'python.exe'} {SPEC_DIR / 'make_splash_frames.py'}"
    )

# collect_submodules('app') 需要后端包在 sys.path 上
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ONEFILE = os.environ.get("BLINDBOX_ONEFILE", "") == "1"
APP_NAME = "AIBlindBox"

WEBVIEW_DIR = Path(webview.__file__).resolve().parent
PYTHONNET_DIR = Path(pythonnet.__file__).resolve().parent
CLR_DIR = Path(clr_loader.__file__).resolve().parent

datas = [
    # 前端：demo.html 用相对路径引 vendor/*.js；serve.py 由 launcher 动态加载
    (str(FRONTEND / "demo.html"), "frontend"),
    (str(FRONTEND / "vendor"), "frontend/vendor"),
    (str(FRONTEND / "serve.py"), "frontend"),
    # 首次启动（exe 脱离项目目录时）用来初始化 settings.yaml
    (str(BACKEND / "settings.yaml.example"), "backend"),
    # 窗口左上角 / 任务栏图标 + 启动动画页：launcher 从 _MEIPASS/desktop/ 读取
    (str(ICON), "desktop"),
    (str(SPEC_DIR / "splash.html"), "desktop"),
    # 屏幕中央原生浮层动画帧：首帧在双击后立刻上屏，不能延迟加载
    (str(FRAMES), "desktop/splash_frames"),
    # pywebview 的 JS 注入脚本与 WebView2/WinForms 互操作程序集
    (str(WEBVIEW_DIR / "js"), "webview/js"),
    (str(WEBVIEW_DIR / "lib"), "webview/lib"),
    # pythonnet 与 clr_loader 的 .NET 运行时（无官方 hook，必须手工收集）
    (str(PYTHONNET_DIR / "runtime"), "pythonnet/runtime"),
    (str(CLR_DIR / "ffi" / "dlls" / "amd64"), "clr_loader/ffi/dlls/amd64"),
    (str(CLR_DIR / "ffi" / "dlls" / "x86"), "clr_loader/ffi/dlls/x86"),
]

hiddenimports = []
# uvicorn 的 loop/protocol 是按字符串动态导入的；app 包整体静态扫描补一遍
for pkg in ("app", "uvicorn", "edge_tts", "openai", "multipart", "clr_loader"):
    hiddenimports += collect_submodules(pkg)
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    # pywebview 的 Windows 后端（WebView2 + WinForms）；不 collect 全平台避免 cocoa/gtk 噪音
    "webview",
    "webview.guilib",
    "webview.util",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.win32",
    "webview.platforms.mshtml",
    "clr",
    "pythonnet",
    # launcher 在 main() 里延迟 import，静态扫描可能漏掉，显式声明
    "native_splash",
    "yaml", "httpx", "anyio", "starlette", "fastapi",
    "PIL.Image", "PIL.ImageChops", "PIL.ImageDraw", "PIL.ImageFilter",
]
hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(BACKEND), str(ROOT), str(SPEC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest", "IPython"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        icon=str(ICON),
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        icon=str(ICON),
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )
