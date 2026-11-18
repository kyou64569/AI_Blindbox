"""AI 盲盒桌面版启动器（源码态 / PyInstaller frozen 态共用同一份代码）。

设计目标
--------
1. 双击即用：进程内自动拉起本地服务并用桌面窗口打开，无需手动开命令行 / 浏览器。
2. 与网页版功能 100% 一致：复用同一个 FastAPI app 与 frontend/serve.py 的 Handler。
   serve.py 负责静态页、/api 反代、SSE 流式转发、/output 的 Range/206 与 charset，
   这些能力 FastAPI 的 FileResponse 并不完全提供，所以不能用 StaticFiles 代替。
3. 不破坏网页版：不修改 start_all.cmd / start.py / serve.py / demo.html / main.py 路由。
   网页版仍走原来的「两个独立进程 + 浏览器」；本文件是纯新增入口。

运行形态
--------
  源码态（POC）：backend/.venv/Scripts/python.exe desktop/launcher.py
  frozen 态    ：desktop/dist/AIBlindBox/AIBlindBox.exe（由 build.cmd 产出）

  两者都在同一进程内起两个随机端口的监听：
    uvicorn.Config(app, port=0) 子线程       -> 后端
    serve.ReuseTCPServer(port=0) 子线程      -> 前端（/ -> demo.html）
    pywebview(WebView2) 主线程                -> 打开前端端口，关窗即停服务退出

  说明：物理上仍有本地 HTTP（18 个 API + SSE），但用户看不到服务、无需手动启动；
  真·零 HTTP 需要把 pipeline 搬进 GUI 进程，会与网页版分叉，不做。

启动动画（屏幕中央原生浮层）
----------------------------
  旧方案「先开 WebView2 窗口、再在窗口里播 CSS 动画」有三个问题：窗口出现前
  仍有空白、动画时长固定、动画与主窗口是同一个窗口。现在改成：
    1. 双击后**立刻**（首帧只解一张 PNG）用纯 Win32 分层窗口在屏幕正中显示
       透明浮层：无边框、置顶、不抢焦点、不进任务栏，卡片外透出桌面；
    2. 服务启动分阶段上报进度，浮层的开盒帧索引 = 归一化加载进度，
       **加载慢则盒子开得慢、加载快则开得快**（另加极缓的时间兜底防画面呆滞）；
    3. 同时在主线程预创建 hidden=True 的 WebView2 窗口，并在隐藏状态下预加载
       真实页面；三者齐备（服务 + 窗口 + 页面）才播「掀盖 → 光束 → 星芒 →
       淡出」终场；
    4. 闪光到 55% 时才 win.show() 显示主窗口，浮层继续淡出，无缝交接。
  帧由 desktop/make_splash_frames.py 预渲染，native_splash.py 负责窗口与调度。
  原生浮层不可用时自动回退旧 WebView2 内嵌动画页（splash.html + js_api 轮询）；
  --selftest / --no-window / --no-splash 仍走同步顺序，不受影响。

命令行
------
  --selftest           不开窗口，自检端点后退出（无 GUI 环境 / 打包验证用）
  --home PATH          覆盖数据根目录（settings.yaml / output / data / .cache）
  --no-window          用 Edge/Chrome 的 --app 独立窗口（WebView2 不可用时的降级）
  --no-splash          跳过启动动画，直接打开页面（调试 / 对照用）
  --window-timeout N   N 秒后自动关闭窗口（窗口冒烟验证用）
  --boot-delay N       人为放慢加载 N 秒（验证「开盒速度跟随加载」用）
  --allow-multi        跳过单实例锁（默认同一时间只允许一个桌面实例）

数据目录
--------
  源码态：不设 BLINDBOX_HOME 时解析到 backend/，与网页版共享配置、历史与产物。
  frozen：从 exe 目录向上找项目根；找到则共享 backend/（API Key / 历史全一致），
          找不到（exe 被拷到别处）则用 %LOCALAPPDATA%/AIBlindBox，
          并从 settings.yaml.example 初始化（首次需在配置页填 API Key）。

图标
----
  desktop/app.ico 一处生成、三处使用：
    1. build.spec 的 EXE(icon=...)   -> 资源管理器里 exe 的图标 / 任务栏分组图标
    2. webview.start(icon=...)       -> WinForms Form.Icon，即窗口左上角 + 任务栏按钮
    3. SetCurrentProcessExplicitAppUserModelID -> 让任务栏按本应用分组而非 python.exe
  ICO 由 desktop/make_icon.py 生成，随 PyInstaller datas 打进 _MEIPASS/desktop/。
  源码态直接从项目根 desktop/app.ico 读取；两者路径都经 resolve_icon() 解析。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

try:
    # 必须在任何窗口 API 之前调用；native_splash 也会兜底再调一次（幂等）
    from native_splash import ensure_dpi_aware
except Exception:  # 极端情况下（只单独拷走 launcher.py）不影响服务启动
    def ensure_dpi_aware() -> None:  # type: ignore[no-redef]
        pass

IS_FROZEN = bool(getattr(sys, "frozen", False))
_LOG_PATH: Path | None = None
_BOOT_T0 = time.time()      # 进程起点，用于精确核对「双击 -> 首帧」耗时


def _log(msg: str) -> None:
    """同时写控制台和日志文件；frozen --windowed 下 stdout 可能是 None。"""
    now = time.time()
    line = (f"{time.strftime('%H:%M:%S', time.localtime(now))}."
            f"{int((now % 1) * 1000):03d} [+{now - _BOOT_T0:5.2f}s] {msg}")
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _LOG_PATH is not None:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _resource_root() -> Path:
    """只读资源根：frozen 是 _MEIPASS，源码态是项目根。"""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parents[1]


RES = _resource_root()
BACKEND_DIR = RES / "backend" if (RES / "backend").is_dir() else RES
FRONTEND_DIR = RES / "frontend" if (RES / "frontend").is_dir() else RES

# 源码态直接跑 desktop/launcher.py 时 sys.path[0] 是 desktop/，不含后端包；
# frozen 态 app 包在 PYZ 里，插入这条路径也无害。
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _ensure_streams(home: Path) -> None:
    """PyInstaller --windowed 下 sys.stdout/stderr 可能为 None。

    uvicorn 与 SimpleHTTPRequestHandler.log_message 都会直接 sys.stderr.write，
    不补会导致每个请求抛 AttributeError；这里统一导向 service.log。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        fh = open(home / "service.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        fh = io.StringIO()
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def _detect_project_backend() -> Path | None:
    """frozen 态向上找项目根：同时存在 backend/settings.yaml 与 frontend/demo.html。"""
    exe_dir = Path(sys.executable).resolve().parent
    for cand in (exe_dir, *exe_dir.parents):
        if (cand / "backend" / "settings.yaml").is_file() and (cand / "frontend" / "demo.html").is_file():
            return cand / "backend"
    return None


def resolve_home(args: argparse.Namespace) -> Path:
    """确定数据根目录，并在 import app.config 之前把 BLINDBOX_HOME 设好。

    config.py 在模块导入时就执行 SETTINGS = load_settings()，因此这一步必须早于
    任何 app.* import；否则 frozen 态会把 settings.yaml 写进只读临时目录 _MEIPASS。
    """
    global _LOG_PATH
    if args.home:
        home = Path(args.home).expanduser().resolve()
    elif os.environ.get("BLINDBOX_HOME", "").strip():
        home = Path(os.environ["BLINDBOX_HOME"]).expanduser().resolve()
    elif not IS_FROZEN:
        # 与网页版逐字节一致：backend/app/config.py 的 parents[1] 就是这个目录
        home = Path(__file__).resolve().parents[1] / "backend"
    else:
        home = _detect_project_backend() or (
            Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "AIBlindBox"
        )

    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "settings.yaml"
    if not cfg.is_file():
        tpl = BACKEND_DIR / "settings.yaml.example"
        if not tpl.is_file():
            raise RuntimeError(f"找不到配置模板（打包布局有误）: {tpl}")
        shutil.copyfile(tpl, cfg)
        _log(f"首次启动，已从模板初始化配置: {cfg}")

    os.environ["BLINDBOX_HOME"] = str(home)
    _LOG_PATH = home / "desktop.log"
    _ensure_streams(home)
    _log(f"数据根目录: {home}  frozen={IS_FROZEN}  resources={RES}")
    return home


def start_backend(progress_cb=None):
    """子线程跑 uvicorn；传 app 对象而非 import string（PyInstaller 更可靠）。

    progress_cb(stage, p) 让原生浮层知道「重导入 / 服务监听」进行到哪一步，
    从而按真实加载速度调整开盒进度。
    """
    def tick(stage: str, p: float) -> None:
        if progress_cb:
            progress_cb(stage, p)

    tick("载入服务框架", 0.06)
    import uvicorn
    tick("载入应用模块", 0.14)
    # app.main 的导入通常要 2~4 秒，期间没有任何阶段回调，浮层会「卡住」。
    # 用一个后台心跳把这段耗时摊成平滑进度（0.14 -> 0.38），加载越慢，
    # 盒子开得越慢；导入结束立即停心跳并跳到真实阶段值。
    stop_ramp = threading.Event()

    def _import_ramp() -> None:
        p = 0.14
        while not stop_ramp.is_set() and p < 0.38:
            time.sleep(0.18)
            p = min(0.38, p + 0.012)
            tick("载入应用模块", p)

    threading.Thread(target=_import_ramp, daemon=True,
                     name="blindbox-import-ramp").start()
    try:
        from app.main import app  # PyInstaller 靠这条静态 import 收集整个 app 包
    finally:
        stop_ramp.set()
    tick("启动本地服务", 0.38)

    cfg = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,                 # 由 OS 分配，避开网页版 8000/5173，可同时运行
        loop="asyncio",         # 固定实现，避免动态导入 uvloop / httptools
        http="h11",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    # uvicorn 0.30.6 的 capture_signals() 已判断「非主线程直接 yield」，无需 monkeypatch
    t = threading.Thread(target=server.run, daemon=True, name="blindbox-uvicorn")
    t.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        if not t.is_alive():
            raise RuntimeError("uvicorn 线程提前退出，详见 service.log / desktop.log")
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn 30s 内未启动，详见 service.log / desktop.log")
    port = server.servers[0].sockets[0].getsockname()[1]
    tick("服务已监听", 0.56)
    return server, port


def start_frontend(be_port: int, output_dir: Path):
    """复用 frontend/serve.py 的 Handler，只覆盖两个模块级变量。

    - BACKEND    默认 8000，桌面版是随机端口，必须改；
    - OUTPUT_DIR 默认按 serve.py 的 __file__ 推到项目根，frozen 态 _MEIPASS 下不成立，
      统一改用后端的 settings.output_dir（网页版两者本来就指向同一目录）。
    """
    serve_py = FRONTEND_DIR / "serve.py"
    if not serve_py.is_file():
        raise RuntimeError(f"找不到前端服务脚本: {serve_py}")
    spec = importlib.util.spec_from_file_location("blindbox_frontend_serve", str(serve_py))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 frontend/serve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    mod.BACKEND = f"http://127.0.0.1:{be_port}"
    mod.OUTPUT_DIR = Path(output_dir).resolve()
    httpd = mod.ReuseTCPServer(("127.0.0.1", 0), mod.Handler)
    fe_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True, name="blindbox-frontend").start()
    return httpd, fe_port


def selftest(be_port: int, fe_port: int, output_dir: Path) -> int:
    """无窗口自检：覆盖静态页、vendor、API、后端健康检查、产物 Range/206。"""
    base = f"http://127.0.0.1:{fe_port}"
    ok = True

    def _get(url: str, label: str, headers: dict | None = None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()

    for path, label in (
        ("/", "首页 demo.html"),
        ("/vendor/preact.umd.js", "本地 vendor"),
        ("/api/modes", "模式列表"),
        ("/api/config", "配置读取"),
    ):
        try:
            status, _, _ = _get(base + path, label)
            good = status == 200
            _log(f"  {label:14} {path:26} -> {status} {'OK' if good else 'FAIL'}")
            ok &= good
        except Exception as e:
            _log(f"  {label:14} {path:26} -> ERR {e}")
            ok = False

    # 后端 / 必须仍是 health JSON：start.py 的 health_check 依赖它，不能被静态站抢走
    try:
        status, _, body = _get(f"http://127.0.0.1:{be_port}/", "后端健康检查")
        text = body.decode("utf-8", "replace")
        good = status == 200 and '"status"' in text
        _log(f"  {'后端健康检查':14} {'/':26} -> {status} {'OK' if good else 'FAIL'} {text[:60]}")
        ok &= good
    except Exception as e:
        _log(f"  {'后端健康检查':14} {'/':26} -> ERR {e}")
        ok = False

    # /output 的 Range/206：serve.py 独有，FastAPI 的 FileResponse 做不到
    probe = None
    try:
        out = Path(output_dir)
        files = [p for p in out.rglob("*") if p.is_file()]
        if files:
            rel = files[0].relative_to(out.resolve()).as_posix()
        else:
            probe = out / "__desktop_selftest__.bin"
            probe.write_bytes(b"0123456789")
            rel = probe.name
        url = base + "/output/" + urllib.parse.quote(rel)
        try:
            status, headers, data = _get(url, "产物 Range", {"Range": "bytes=0-3"})
            good = status == 206 and len(data) == 4 and bool(headers.get("Content-Range"))
        except urllib.error.HTTPError as e:
            status, headers, data, good = e.code, e.headers, b"", False
        _log(f"  {'产物 Range/206':14} {rel[:26]:26} -> {status} "
             f"{'OK' if good else 'FAIL'} Content-Range={headers.get('Content-Range')}")
        ok &= good
    except Exception as e:
        _log(f"  {'产物 Range/206':14} -> ERR {e}")
        ok = False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except Exception:
                pass

    _log("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _open_edge_app(url: str) -> bool:
    """降级：Edge/Chrome 的 --app 模式（无地址栏，像个独立窗口）。

    用子进程退出作为「窗口关闭」信号，这样服务生命周期仍与窗口绑定。
    """
    import subprocess
    cands: list[Path] = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            cands += [
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
    exe = next((str(p) for p in cands if p.is_file()), None) or (
        shutil.which("msedge") or shutil.which("chrome")
    )
    if not exe:
        return False
    profile = Path(tempfile.gettempdir()) / "AIBlindBoxEdgeProfile"
    try:
        proc = subprocess.Popen([
            exe, f"--app={url}", "--window-size=1360,900",
            f"--user-data-dir={profile}", "--no-first-run",
        ])
        proc.wait()
        return True
    except Exception as e:
        _log(f"Edge --app 启动失败: {e}")
        return False


def resolve_icon() -> str | None:
    """定位 app.ico：源码态在项目 root/desktop，frozen 态在 _MEIPASS/desktop。

    PyInstaller 的 datas 把 ico 放到包内 desktop/ 下；万一缺失还有两个兜底位置。
    找不到时返回 None，pywebview 会退回从 sys.executable 提取 PE 图标
    （frozen 态恰好就是我们打进 exe 的图标，源码态则是 Python 默认图标）。
    """
    candidates = (
        RES / "desktop" / "app.ico",
        Path(sys.executable).resolve().parent / "app.ico",
        Path(__file__).resolve().parent / "app.ico",
        RES / "app.ico",
    )
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def set_app_user_model_id() -> None:
    """让 Windows 任务栏把本进程识别为独立应用。

    必须在创建窗口之前调用：未设置时任务栏可能按宿主（python.exe / 打包 bootloader）
    分组并显示宿主图标；设置后固定/跳转列表与任务栏图标都指向本应用。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AI.BlindBox.Desktop.1"
        )
    except Exception as e:
        _log(f"设置 AppUserModelID 失败（不影响窗口图标）: {e}")


class BootState:
    """启动状态：原生浮层读 snapshot()；旧动画页的 js_api 读 state()。

    三个事件把「加载完成」拆细，保证浮层闪光之前主窗口与页面都已就绪：
      ready_event   两个本地服务已起且首页探活通过
      window_ready  隐藏的 WebView2 窗口已创建（.NET + WebView2 初始化完成）
      page_ready    真实页面已在隐藏窗口里预加载完毕
    all_ready 三者齐备才为真 —— 这才是浮层可以开始闪光终场的时刻。
    polls 是旧动画页「确实活着」的证据：为 0 说明 js_api 没注入，
    boot_services 的 watchdog 会用 load_url 兜底跳转，避免永久卡在动画页。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "loading"
        self.stage = "启动中"
        self.progress = 0.02
        self.url = ""
        self.error = ""
        self.polls = 0
        self.started_at = time.time()
        self.ready_event = threading.Event()       # 服务就绪
        self.window_ready = threading.Event()      # 隐藏窗口已创建
        self.page_ready = threading.Event()        # 真实页面已预加载

    def state(self) -> dict:
        with self._lock:
            self.polls += 1
            return {"status": self.status, "url": self.url, "error": self.error}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "stage": self.stage,
                "progress": self.progress,
                "url": self.url,
                "error": self.error,
                "all_ready": (self.status == "ready"
                              and self.window_ready.is_set()
                              and self.page_ready.is_set()),
            }

    def set_stage(self, stage: str, progress: float) -> None:
        with self._lock:
            self.stage = stage
            self.progress = max(self.progress, min(0.97, float(progress)))

    def set_ready(self, url: str) -> None:
        with self._lock:
            self.status, self.url = "ready", url
            self.progress, self.stage = 1.0, "加载完成"
        self.ready_event.set()

    def set_error(self, msg: str) -> None:
        with self._lock:
            self.status, self.error = "error", msg
            self.stage = "启动失败"
        self.ready_event.set()


def load_splash_html() -> str:
    """读取启动动画页；打包后位于 _MEIPASS/desktop/splash.html。"""
    cands = (RES / "desktop" / "splash.html", Path(__file__).resolve().parent / "splash.html")
    for p in cands:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    _log("警告: 找不到 splash.html，使用极简启动页（watchdog 会兜底跳转）")
    return ("<!doctype html><meta charset='utf-8'><body style='margin:0;background:#0f1115;"
            "color:#e6e8ec;font:16px sans-serif;display:grid;place-items:center;height:100vh'>"
            "AI 盲盒启动中…</body>")


def boot_services(state: BootState, runtime: dict, win=None,
                  boot_delay: float = 0.0) -> None:
    """后台 daemon 线程：分阶段上报进度，供原生浮层同步开盒速度。

    win 非 None 表示走「旧 WebView2 内嵌动画页」回退路径，此时仍启动 js_api
    轮询 watchdog；原生浮层路径不需要它，传 None。
    """
    last_log = {"stage": "", "p": -1.0}

    def progress(stage: str, p: float) -> None:
        state.set_stage(stage, p)
        # 导入心跳每 0.18s 调一次，别把日志刷爆：只在换阶段或跨 10% 时记录
        if stage != last_log["stage"] or p - last_log["p"] >= 0.10:
            _log(f"加载进度 {p:>4.0%}  {stage}")
            last_log["stage"], last_log["p"] = stage, p

    try:
        state.set_stage("准备启动", 0.02)
        if boot_delay > 0:
            _log(f"--boot-delay: 人为放慢加载 {boot_delay:.1f}s（验证动画同步）")
            step = boot_delay / 6
            for i in range(6):
                time.sleep(step)
                progress(f"模拟慢加载 {i + 1}/6", 0.04 + i * 0.025)
        server, be_port = start_backend(progress)
        runtime["server"] = server
        progress("读取配置", 0.62)
        from app import config
        output_dir = config.get_settings().output_dir
        httpd, fe_port = start_frontend(be_port, output_dir)
        runtime["httpd"] = httpd
        progress("校验端点", 0.78)
        url = f"http://127.0.0.1:{fe_port}/"
        # 轻量探活：确认前端反代真的能响应，再宣布「加载完成」
        with urllib.request.urlopen(url, timeout=10) as r:
            if r.status != 200:
                raise RuntimeError(f"首页探活返回 {r.status}")
        progress("服务就绪", 0.92)
        _log(f"backend={be_port}  frontend={fe_port}  url={url}")
        state.set_ready(url)
    except Exception:
        import traceback
        msg = traceback.format_exc(limit=6)
        _log("后台服务启动失败:\n" + msg)
        state.set_error(msg)
    finally:
        if win is not None and state.status == "ready":
            def _watchdog() -> None:
                time.sleep(3.0)
                _log(f"启动动画状态: js_api 轮询 {state.polls} 次, status={state.status}")
                if state.polls == 0:
                    _log("splash 未上报 js_api 轮询，改用 load_url 兜底跳转")
                    try:
                        win.load_url(state.url)  # winforms 内部 Invoke，跨线程安全
                    except Exception as e:
                        _log(f"兜底跳转失败: {e}")
            threading.Thread(target=_watchdog, daemon=True,
                             name="blindbox-splash-watchdog").start()


def open_splash_window(state: BootState, runtime: dict, timeout: int,
                       icon: str | None) -> None:
    """先显示动画窗口，服务在 daemon 线程并行启动；窗口关闭后返回。"""
    import webview
    win = webview.create_window(
        "AI 盲盒", html=load_splash_html(), js_api=state,
        width=1360, height=900, min_size=(1024, 700),
        background_color="#0F1115",   # 消除 WebView2 初始化时的白色闪屏
    )
    threading.Thread(target=boot_services, args=(state, runtime, win),
                     daemon=True, name="blindbox-boot").start()
    if timeout > 0:
        def _auto_close() -> None:
            time.sleep(timeout)
            try:
                win.destroy()
            except Exception:
                pass
        threading.Thread(target=_auto_close, daemon=True,
                         name="blindbox-autoclose").start()
    # icon 仍由 start 设置 -> WinForms Form.Icon（窗口左上角 + 任务栏）
    webview.start(icon=icon)


def open_edge_fallback(url: str) -> None:
    """Edge --app 降级路径（--no-window / WebView2 不可用时），不做启动动画。"""
    if _open_edge_app(url):
        return
    _log("Edge/Chrome 未找到，降级为默认浏览器（需手动结束本进程才会停服务）")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def bring_window_front(win) -> None:
    """把主窗口真正放到最前面。

    pywebview 的 win.show() -> Form.Activate() 从后台线程调用时，常被 Windows
    前台激活限制拒绝；而浮层是无焦点置顶窗口，销毁后焦点可能落回原来那个窗口，
    导致主窗口被盖住。TOPMOST -> NOTOPMOST 的窗口位置切换不受前台锁限制，
    效果等价于「置前但不永远悬浮」。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from webview.platforms import winforms
        bv = winforms.BrowserView.instances.get(win.uid)
        if bv is None:
            return
        hwnd = ctypes.c_void_p(int(bv.Handle.ToInt64()))
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        SWP = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
        u.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, SWP)  # TOPMOST
        u.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, SWP)  # NOTOPMOST
        u.SetForegroundWindow(hwnd)
        _log("主窗口已置前")
    except Exception as e:
        _log(f"主窗口置前失败（不影响功能）: {e}")


def run_native_path(state: BootState, runtime: dict, splash, icon: str | None,
                    timeout: int, boot_delay: float) -> None:
    """原生浮层主路径：隐藏预创建 WebView2 窗口，闪光到一半时才显示。

    pywebview 的 hidden=True 会先 Show() 再 Hide()，即 WebView2/.NET 已完整初始化
    但窗口不可见；show() / load_url() 内部走 WinForms Invoke，可跨线程安全调用
    （已用 hidden_webview_poc 实测）。这样「启动慢的那几秒」由浮层覆盖，
    而窗口本身已悄悄准备好，闪光淡出时不会出现空档。
    """
    import webview

    # 服务先起：与 .NET/WebView2 初始化并行，且早于 create_window，
    # 万一 create_window 抛错也不会卡住服务启动。
    threading.Thread(target=boot_services,
                     args=(state, runtime, None, boot_delay),
                     daemon=True, name="blindbox-boot").start()

    win = webview.create_window(
        "AI 盲盒",
        html="<!doctype html><meta charset='utf-8'>"
             "<body style='margin:0;background:#0F1115'></body>",
        hidden=True, width=1360, height=900, min_size=(1024, 700),
        background_color="#0F1115",
    )

    def controller() -> None:
        ok = False
        try:
            if not win.events.shown.wait(25):
                state.set_error("WebView2 隐藏窗口创建超时")
                return
            state.window_ready.set()
            _log("隐藏 WebView2 窗口已创建")

            if not state.ready_event.wait(45) or state.status != "ready":
                _log("服务未就绪或已失败，取消显示主窗口")
                return
            try:
                win.events.loaded.clear()
                win.load_url(state.url)
                win.events.loaded.wait(3.0)
                _log(f"主页面已在隐藏窗口预加载: {state.url}")
            except Exception as e:
                _log(f"隐藏窗口预加载失败（仍继续显示）: {e}")
            state.page_ready.set()

            if not splash.reveal_event.wait(25):
                _log("浮层未发出显示信号，直接显示主窗口")
            if state.status == "error":
                return
            win.show()
            ok = True
            _log("主窗口已显示")
            bring_window_front(win)
            # 浮层销毁时 Windows 可能把焦点还给之前的窗口，再置前一次
            def _refront() -> None:
                time.sleep(0.9)
                bring_window_front(win)
            threading.Thread(target=_refront, daemon=True,
                             name="blindbox-refront").start()
            if timeout > 0:
                time.sleep(timeout)
                try:
                    win.destroy()
                except Exception:
                    pass
        except Exception:
            import traceback
            _log("显示主窗口的控制器异常:\n" + traceback.format_exc(limit=5))
        finally:
            if not ok:
                # 关键：任何异常都必须销毁窗口，否则 webview.start() 永不返回
                try:
                    win.destroy()
                except Exception:
                    pass
                state.set_error("主窗口未能显示")

    threading.Thread(target=controller, daemon=True,
                     name="blindbox-controller").start()
    webview.start(icon=icon)


# 旧的「先起服务、再开 WebView2 窗口」路径已由 open_splash_window 取代：
# 现在窗口先显示动画，两个服务在后台 daemon 线程并行启动。
# open_splash_window 保留为「原生浮层不可用」时的回退路径。


def acquire_single_instance():
    """Windows 文件锁：避免双击两次起两个实例抢写同一份 settings.yaml。

    msvcrt.locking 要求文件至少 1 字节；进程退出时锁自动释放，不留残留文件锁状态。
    返回 fd 表示拿到锁，None 表示已有实例在跑。
    """
    if os.name != "nt":
        return -1
    import msvcrt
    p = Path(tempfile.gettempdir()) / "AIBlindBox.desktop.lock"
    fd = -1
    try:
        fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
        os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return fd
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        return None


def open_plain_window(url: str, timeout: int, icon: str | None) -> None:
    """--no-splash 调试路径：不开动画，直接加载真实页面。"""
    import webview
    win = webview.create_window(
        "AI 盲盒", url, width=1360, height=900, min_size=(1024, 700),
        background_color="#0F1115",
    )
    if timeout > 0:
        def _auto_close() -> None:
            time.sleep(timeout)
            try:
                win.destroy()
            except Exception:
                pass
        threading.Thread(target=_auto_close, daemon=True,
                         name="blindbox-autoclose").start()
    webview.start(icon=icon)


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 盲盒桌面版启动器")
    ap.add_argument("--selftest", action="store_true", help="不开窗口，自检端点后退出")
    ap.add_argument("--home", default="", help="覆盖数据根目录")
    ap.add_argument("--no-window", action="store_true", help="用 Edge --app 窗口（降级）")
    ap.add_argument("--no-splash", action="store_true", help="跳过启动动画（调试用）")
    ap.add_argument("--window-timeout", type=int, default=0, help="N 秒后自动关闭窗口")
    ap.add_argument("--boot-delay", type=float, default=0.0,
                    help="人为放慢加载 N 秒（验证开盒速度跟随加载）")
    ap.add_argument("--allow-multi", action="store_true", help="跳过单实例锁")
    args = ap.parse_args()

    # DPI 感知必须早于任何窗口 API：浮层坐标/尺寸按物理像素算
    if not args.selftest:
        ensure_dpi_aware()

    home = resolve_home(args)
    if not args.selftest:
        set_app_user_model_id()
    lock = None
    if not args.selftest and not args.allow_multi:
        lock = acquire_single_instance()
        if lock is None:
            _log("已有桌面实例在运行，本次启动退出")
            return 0

    runtime: dict = {"server": None, "httpd": None}
    try:
        state = BootState()
        icon = None if args.selftest else resolve_icon()
        if not args.selftest:
            _log(f"窗口图标: {icon or '(缺省，回退到进程图标)'}")

        # 自检 / Edge 降级 / --no-splash：保持「先起服务、再开窗口」的同步顺序
        if args.selftest or args.no_window or args.no_splash:
            server, be_port = start_backend()
            runtime["server"] = server
            from app import config
            output_dir = config.get_settings().output_dir
            httpd, fe_port = start_frontend(be_port, output_dir)
            runtime["httpd"] = httpd
            url = f"http://127.0.0.1:{fe_port}/"
            _log(f"backend={be_port}  frontend={fe_port}  url={url}")
            if args.selftest:
                return selftest(be_port, fe_port, output_dir)
            if args.no_window:
                open_edge_fallback(url)
            else:
                open_plain_window(url, args.window_timeout, icon)
            return 0

        # 主路径：先把原生浮层打上屏（首帧只解一张 PNG），之后才做重活
        frames = RES / "desktop" / "splash_frames"
        splash = None
        if (frames / "manifest.json").is_file():
            try:
                from native_splash import NativeSplash
                splash = NativeSplash(state, frames, log=_log)
                if not splash.start():
                    _log("原生浮层首帧未上屏，回退 WebView2 内嵌动画页")
                    splash.close()
                    splash = None
            except Exception as e:
                _log(f"原生浮层不可用，回退内嵌动画页: {type(e).__name__}: {e}")
                splash = None
        else:
            _log("未找到 splash_frames，回退 WebView2 内嵌动画页")

        if splash is None:
            # 回退：旧的 WebView2 内嵌动画页（splash.html + js_api 轮询）
            try:
                open_splash_window(state, runtime, args.window_timeout, icon)
            except Exception as e:
                _log(f"WebView2 启动动画窗口失败: {type(e).__name__}: {e}")
                state.ready_event.wait(40)
                if state.status == "ready":
                    open_edge_fallback(state.url)
                else:
                    _log("后台服务未就绪，无法降级打开；详见 desktop.log")
            return 0

        try:
            run_native_path(state, runtime, splash, icon,
                            args.window_timeout, args.boot_delay)
        except Exception as e:
            _log(f"原生浮层窗口路径失败: {type(e).__name__}: {e}")
            state.ready_event.wait(40)
            if state.status == "ready":
                open_edge_fallback(state.url)
        finally:
            splash.close()
        return 1 if state.status == "error" else 0
    except Exception:
        import traceback
        _log("FATAL:\n" + traceback.format_exc())
        return 1
    finally:
        server, httpd = runtime["server"], runtime["httpd"]
        if server is not None:
            try:
                server.should_exit = True
            except Exception:
                pass
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        _log(f"服务已停止 (home={home})")


if __name__ == "__main__":
    sys.exit(main())
