"""免构建前端静态服务器 + /api 反向代理到 FastAPI(8000)。

用法：
  backend/.venv/Scripts/python.exe serve.py
默认 http://127.0.0.1:5173 ，托管本目录（含 demo.html）并把 /api 转发到后端。

环境变量（由 start.py / start_all.sh 在端口被占用自动换端口后传入）：
  BLINDBOX_FE_PORT=5174   前端监听端口（默认 5173）
  BLINDBOX_BE_URL=http://127.0.0.1:8001   后端地址（默认 8000）

特性：
  - ThreadingHTTPServer：浏览器并行加载 demo.html + vendor 多个文件时不再串行卡顿。
  - 端口被占用时自动 +1 更换（不杀进程、不无限等待），最多尝试 10 个端口。
  - 进程异常退出后由启动脚本(run_frontend.cmd)自动重启。
"""
from __future__ import annotations

import http.server
import os
import re
import socket
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = os.environ.get("BLINDBOX_BE_URL", "http://127.0.0.1:8000").strip()
PORT = int(os.environ.get("BLINDBOX_FE_PORT", "5173").strip())
MAX_PORT_TRIES = 10
# 生成产物目录（项目 output/），通过 /output 暴露给前端预览
OUTPUT_DIR = (Path(__file__).resolve().parents[1] / "output").resolve()


def _find_free_port(base: int) -> int:
    """从 base 开始找第一个可监听端口（占用则 +1），找不到回退 base。"""
    for p in range(base, base + MAX_PORT_TRIES):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return base





class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._proxy("POST")
        else:
            self.send_error(405)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy("GET")
        elif self.path.startswith("/output/"):
            self._serve_output(send_body=True)
        else:
            # 默认首页指向 demo.html
            if self.path in ("/", "/index.html"):
                self.path = "/demo.html"
            super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/output/"):
            self._serve_output(send_body=False)
        else:
            if self.path in ("/", "/index.html"):
                self.path = "/demo.html"
            super().do_HEAD()

    def _serve_output(self, send_body: bool):
        """服务 /output/ 目录下的生成产物；音频/视频需要 Content-Length + Accept-Ranges。"""
        # 文件名可能含中文（按提示词命名），浏览器会做 URL 编码，这里还原回来
        # 顺序很关键：先在「原始未解码」路径上剥掉查询串/锚点，再 unquote。
        # 反过来做会把文件名里合法的 %23 解成 # 后当成锚点分隔符截断 ——
        # 本项目产物名按提示词生成，确实存在含 # 的文件（如 result_#_《头条…》.json）。
        raw = self.path[len("/output/"):].split("?", 1)[0].split("#", 1)[0]
        rel = urllib.parse.unquote(raw)
        # 绝对路径 / 带盘符的输入会让 `OUTPUT_DIR / rel` 直接跳出 OUTPUT_DIR
        if not rel or Path(rel).is_absolute() or Path(rel).drive or rel[0] in "/\\":
            self.send_error(404)
            return
        target = (OUTPUT_DIR / rel).resolve()
        # 必须用 relative_to 做「目录归属」判断，不能用字符串 startswith：
        # OUTPUT_DIR = ...\AI_Blindbox\output 时，同级的 ...\AI_Blindbox\output_backup_xxx
        # 字符串前缀同样匹配 —— 本项目就真实存在 output_backup_20260730_224242 目录，
        # 因此 /output/../output_backup_20260730_224242/xxx 曾可被读出。
        try:
            target.relative_to(OUTPUT_DIR)
        except ValueError:
            self.send_error(404)
            return
        # 只服务普通文件；目录会在后面 stat().st_size / open() 处炸成 500
        if not target.is_file():
            self.send_error(404)
            return

        ctype = self.guess_type(str(target))
        # 文本/JSON 补充 charset=utf-8，避免浏览器按系统默认编码(GBK)解析 UTF-8 导致中文乱码
        if ctype.startswith("text/") or ctype == "application/json":
            if "charset" not in ctype:
                ctype += "; charset=utf-8"

        fs = target.stat().st_size
        rng = self.headers.get("Range")
        if rng:
            # 仅处理单区间 bytes=begin-end（浏览器音频/视频 seek 需要）
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start_s, end_s = m.groups()
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else fs - 1
                if start >= fs or (end_s and end >= fs):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{fs}")
                    self.end_headers()
                    return
                end = min(end, fs - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{fs}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if send_body:
                    with open(target, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                return
        # 完整文件：带 Content-Length，让浏览器音频/视频能正确显示进度条
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(fs))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if send_body:
            with open(target, "rb") as f:
                self.wfile.write(f.read())

    def _proxy(self, method: str):
        url = BACKEND + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        try:
            resp = urllib.request.urlopen(req, timeout=720)
            data = resp.read()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data = e.read()
            status = e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except urllib.error.URLError as e:
            # 后端暂时不可用/抖动：返回 502 而非抛异常（避免单点故障拖垮前端进程）
            msg = ("后端服务暂不可用 (%(reason)s)。请确认后端已启动：" % {"reason": e.reason}
                   if hasattr(e, "reason") else "后端服务暂不可用。")
            data = msg.encode("utf-8")
            status = 502
            ctype = "text/plain; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        # API 数据禁止浏览器/中间缓存：配置、模式列表、开盒结果都可能随时变化，
        # 缓存旧响应会导致「改了配置刷新页面却不生效」的假故障。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        # 折叠健康检查等噪音，仅记录真实访问
        if self.path.startswith("/api/health"):
            return
        super().log_message(fmt, *args)


class ReuseTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True  # client threads won't block shutdown


def _create_server() -> "ReuseTCPServer":
    """创建服务器；端口被占用时自动 +1 更换（不杀进程、不无限等待）。"""
    global PORT
    chosen = _find_free_port(PORT)
    if chosen != PORT:
        sys.stderr.write(
            f"[warn] 端口 {PORT} 被占用，已自动更换为 {chosen}\n"
        )
        sys.stderr.flush()
        PORT = chosen
    return ReuseTCPServer(("127.0.0.1", PORT), Handler)


if __name__ == "__main__":
    httpd = _create_server()
    print(f"AI 盲盒前端演示: http://127.0.0.1:{PORT}  (代理 -> {BACKEND})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("收到中断信号，前端服务退出。")
    finally:
        httpd.server_close()
