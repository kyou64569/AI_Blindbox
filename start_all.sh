#!/usr/bin/env bash
# AI 盲盒 —— 一键启动 / 停止 / 守护 脚本
#
# 用法（在项目根目录 D:\work\AI_Blindbox 下运行，需 Git Bash 或 WSL）：
#   ./start_all.sh           启动前后端（已运行则跳过），非阻塞
#   ./start_all.sh watch     启动并守护：前端进程丢失会自动重启（Ctrl+C 退出守护，服务继续）
#   ./start_all.sh stop      停止服务（按端口精确杀，干净）
#   ./start_all.sh restart   重启
#   ./start_all.sh status    查看端口/进程状态
#
# 说明：用 nohup/setsid 脱离终端，正常关闭终端后服务仍在；若仍丢失，用 watch 模式守护。

set -u

ROOT="/d/work/AI_Blindbox"
VENV_PY="$ROOT/backend/.venv/Scripts/python.exe"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
BPORT=8000
FPORT=5173
RUNDIR="$ROOT/.run"
BPID="$RUNDIR/backend.pid"
FPID="$RUNDIR/frontend.pid"
BLOG="$RUNDIR/backend.log"
FLOG="$RUNDIR/frontend.log"

mkdir -p "$RUNDIR"

# 检测端口是否已被监听（用 python，避免依赖 nc/lsof）
port_listening() {
  "$VENV_PY" - "$1" <<'PY' 2>/dev/null
import socket, sys
p = int(sys.argv[1])
s = socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1", p)); print("yes")
except Exception:
    print("no")
finally:
    s.close()
PY
}

# 按端口精确杀死占用进程（Windows 用 netstat.exe 解析 PID，兼容 Git Bash）
kill_port() {
  "$VENV_PY" - "$1" <<'PY' 2>/dev/null
import subprocess, re, sys
port = int(sys.argv[1])
try:
    raw = subprocess.run(["netstat.exe", "-ano"], capture_output=True, timeout=10).stdout
except Exception:
    try:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10).stdout
    except Exception as e:
        print("  (netstat 不可用，跳过端口清理:", e, ")"); sys.exit(0)
# Windows 中文版 netstat 输出为 GBK，避免 UTF-8 解码报错
out = raw.decode("gbk", errors="ignore")
killed = []
for line in out.splitlines():
    if (":%d " % port) in line and "LISTENING" in line:
        m = re.search(r"(\d+)\s*$", line.strip())
        if m:
            pid = int(m.group(1))
            if pid == 0:
                continue
            try:
                # Windows 用 taskkill 强杀（signal.SIGKILL 在 Windows 不存在）
                subprocess.run(["taskkill.exe", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
                killed.append(pid)
            except Exception as e:
                print("  杀 PID", pid, "失败:", e)
if killed:
    print("  已终止占用端口", port, "的进程:", sorted(set(killed)))
else:
    print("  未发现占用端口", port, "的进程")
PY
}

# 启动单个服务（剥离终端，写 PID 仅供 status 展示；stop/watch 不依赖其精确性）
launch() {
  local name=$1 dir=$2 cmdargs=$3 pidfile=$4 log=$5 port=$6
  if [ "$(port_listening $port)" = "yes" ]; then
    echo "[$name] 端口 $port 已被占用，跳过（如需接管请先 stop）。"
    return 0
  fi
  echo "[$name] 启动中…"
  if command -v setsid >/dev/null 2>&1; then
    ( cd "$dir" && setsid "$VENV_PY" $cmdargs > "$log" 2>&1 & echo $! > "$pidfile" )
  else
    ( cd "$dir" && nohup "$VENV_PY" $cmdargs > "$log" 2>&1 & echo $! > "$pidfile" )
  fi
  sleep 3
  if [ "$(port_listening $port)" = "yes" ]; then
    echo "[$name] 已启动 (日志: $log)"
  else
    echo "[$name] 启动失败，最近日志："; tail -n 20 "$log" 2>/dev/null
  fi
}

start_backend()  { launch backend  "$BACKEND_DIR"  "-m uvicorn app.main:app --host 127.0.0.1 --port $BPORT"  "$BPID" "$BLOG" "$BPORT"; }
start_frontend() { launch frontend "$FRONTEND_DIR" "serve.py"                                                          "$FPID" "$FLOG" "$FPORT"; }

stop_service() {
  local name=$1 port=$2 pidfile=$3
  echo "[$name] 停止中 (端口 $port)…"
  kill_port "$port"
  rm -f "$pidfile" 2>/dev/null || true
}

cmd=${1:-start}
case "$cmd" in
  start)
    start_backend
    start_frontend
    echo "------------------------------------------------------------"
    echo "  前端: http://127.0.0.1:$FPORT   (后端: http://127.0.0.1:$BPORT)"
    echo "  提示: 如需前端掉线自动重启，改用 ./start_all.sh watch"
    ;;
  watch)
    start_backend
    start_frontend
    echo "------------------------------------------------------------"
    echo "  [watch] 守护中：前端端口 $FPORT 掉线将自动重启 (Ctrl+C 退出守护，服务继续运行)"
    while true; do
      if [ "$(port_listening $FPORT)" != "yes" ]; then
        echo "[watch] $(date '+%H:%M:%S') 检测到前端掉线，自动重启…"
        start_frontend
      fi
      sleep 5
    done
    ;;
  stop)
    stop_service frontend "$FPORT" "$FPID"
    stop_service backend  "$BPORT" "$BPID"
    echo "已停止服务。"
    ;;
  restart)
    stop_service frontend "$FPORT" "$FPID"
    stop_service backend  "$BPORT" "$BPID"
    sleep 1
    start_backend
    start_frontend
    echo "------------------------------------------------------------"
    echo "  前端: http://127.0.0.1:$FPORT"
    ;;
  status)
    echo "[backend]  port $BPORT: $(port_listening $BPORT)   pid: $(cat "$BPID" 2>/dev/null || echo -)"
    echo "[frontend] port $FPORT: $(port_listening $FPORT)   pid: $(cat "$FPID" 2>/dev/null || echo -)"
    ;;
  *)
    echo "用法: $0 {start|watch|stop|restart|status}"; exit 1 ;;
esac
