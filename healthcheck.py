"""启动后自检：探测 8000/5173，挂了就把对应日志打出来。

用法（在 start_all.cmd 中调用）：
  backend/.venv/Scripts/python.exe healthcheck.py
"""
from __future__ import annotations

import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache")

SERVICES = [
    ("后端 API (8000)", 8000, "backend.log"),
    ("前端演示 (5173)", 5173, "frontend.log"),
]


def probe(name: str, port: int, logfile: str) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        print(f"[OK]   {name} 已启动 -> http://127.0.0.1:{port}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name} 未启动 ({e})")
        log = os.path.join(CACHE, logfile)
        if os.path.exists(log):
            print(f"  ---- {logfile} (最近 40 行) ----")
            text = open(log, encoding="utf-8", errors="ignore").read().strip()
            lines = text.splitlines()[-40:]
            print("\n".join(lines))
        else:
            print(f"  (无日志文件 {log})")
        return False


def main() -> int:
    print("=" * 48)
    print("AI 盲盒 启动自检")
    print("=" * 48)
    all_ok = True
    for name, port, logfile in SERVICES:
        if not probe(name, port, logfile):
            all_ok = False
    if not all_ok:
        print("\n[排查建议]")
        print("  1) 看上方日志里的报错（通常是依赖缺失或端口被占用）。")
        print("  2) 手动跑：backend\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
        print("            backend\\.venv\\Scripts\\python.exe frontend\\serve.py")
        print("  3) 端口被占用可改 settings.yaml / serve.py 里的端口，或先结束占用进程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
