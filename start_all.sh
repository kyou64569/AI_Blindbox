#!/usr/bin/env bash
# AI 盲盒 —— 一键启动 / 停止 / 守护 脚本
#
# 用法（在项目根目录下运行，需 Git Bash 或 WSL）：
#   ./start_all.sh           启动前后端（已运行则跳过），非阻塞
#   ./start_all.sh watch     启动并守护：前端进程丢失会自动重启（Ctrl+C 退出守护，服务继续）
#   ./start_all.sh stop      停止服务（按端口精确杀，干净）
#   ./start_all.sh restart   重启
#   ./start_all.sh status    查看端口/进程状态
#
# 开发模式（Vite HMR）：DEV_MODE=1 ./start_all.sh
#   - frontend/node_modules 存在时前端走 Vite dev server（热更新）
#   - 缺失时自动回退 serve.py（免构建 demo.html）
#
# 说明：用 nohup/setsid 脱离终端，正常关闭终端后服务仍在；若仍丢失，用 watch 模式守护。

set -u

# 项目根基于脚本所在目录推导（不写死盘符，便于迁移/他人部署）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
VENV_PY="$ROOT/backend/.venv/Scripts/python.exe"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
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

# 动态分配端口：从 base 开始找第一个空闲端口（占用则 +1，最多 20 个）
# 仅用于「启动新实例」；stop/status/restart 必须用 find_running_port 找在用端口。
assign_port() {
  "$VENV_PY" - "$1" <<'PY' 2>/dev/null
import socket, sys
base = int(sys.argv[1])
for p in range(base, base + 20):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        continue
    finally:
        s.close()
else:
    print(base)
PY
}

# 探测「已在运行」的端口：列出 [base, base+20) 内所有正在监听的端口（空格分隔）。
# stop/status/restart 用它，避免误操作到一个空闲端口上（否则停不掉、状态永远显示未运行）。
# 返回全部而非第一个，因为历史上可能因端口漂移残留多个实例，stop 需要一次清干净。
find_running_ports() {
  "$VENV_PY" - "$1" <<'PY' 2>/dev/null
import socket, sys
base = int(sys.argv[1])
found = []
for p in range(base, base + 20):
    s = socket.socket(); s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", p)); found.append(p)
    except Exception:
        pass
    finally:
        s.close()
print(" ".join(str(p) for p in found))
PY
}

# 端口变量按子命令区分求值：start/watch 需要空闲端口，stop/status/restart 需要在用端口。
resolve_ports_for_start() {
  BPORT=$(assign_port 8000)
  FPORT=$(assign_port 5173)
  if [ "$BPORT" != "8000" ]; then echo "  [port] 8000 被占用，后端使用 $BPORT"; fi
  if [ "$FPORT" != "5173" ]; then echo "  [port] 5173 被占用，前端使用 $FPORT"; fi
}

# BPORTS / FPORTS 为在用端口列表（可能为空字符串）；BPORT / FPORT 取第一个用于展示。
resolve_ports_for_manage() {
  BPORTS=$(find_running_ports 8000)
  FPORTS=$(find_running_ports 5173)
  BPORT=${BPORTS%% *}; BPORT=${BPORT:-8000}
  FPORT=${FPORTS%% *}; FPORT=${FPORT:-5173}
}

# 按端口精确杀死占用进程（Windows 用 netstat.exe 解析 PID，兼容 Git Bash）
# 强杀前先校验该进程命令行属于本项目，避免误杀恰好占用 8000/5173 的其它应用。
kill_port() {
  "$VENV_PY" - "$1" "$ROOT" <<'PY' 2>/dev/null
import subprocess, re, sys, os
port = int(sys.argv[1])
root = sys.argv[2] if len(sys.argv) > 2 else ""
# 项目归属标记：目录名 + 关键脚本名（Git Bash 的 /d/... 与 Windows 的 D:\... 都能命中目录名）
markers = [m for m in (os.path.basename(root.rstrip("/\\")), "app.main:app", "serve.py") if m]


def _cmdline(pid: int) -> str:
    """取进程命令行；wmic 优先，Win11 24H2+ 回退 PowerShell。取不到返回空串。"""
    try:
        raw = subprocess.run(
            ["wmic", "process", "where", "processid=%d" % pid, "get", "commandline"],
            capture_output=True, timeout=10).stdout
        txt = raw.decode("gbk", errors="ignore").strip()
        if txt and "CommandLine" in txt:
            return txt
    except Exception:
        pass
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"ProcessId=%d\""
              " | Select-Object -ExpandProperty CommandLine" % pid)
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=10).stdout
        return raw.decode("gbk", errors="ignore").strip()
    except Exception:
        return ""


try:
    raw = subprocess.run(["netstat.exe", "-ano"], capture_output=True, timeout=10).stdout
except Exception:
    try:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10).stdout
    except Exception as e:
        print("  (netstat 不可用，跳过端口清理:", e, ")"); sys.exit(0)
# Windows 中文版 netstat 输出为 GBK，避免 UTF-8 解码报错
out = raw.decode("gbk", errors="ignore")
killed, skipped = [], []
seen = set()
for line in out.splitlines():
    if (":%d " % port) in line and "LISTENING" in line:
        m = re.search(r"(\d+)\s*$", line.strip())
        if not m:
            continue
        pid = int(m.group(1))
        if pid == 0 or pid in seen:
            continue
        seen.add(pid)
        cl = _cmdline(pid)
        # 命令行可读且不含本项目标记 -> 视为他人进程，跳过（不强杀）
        if cl and not any(mk and mk in cl for mk in markers):
            skipped.append(pid)
            continue
        try:
            # Windows 用 taskkill 强杀（signal.SIGKILL 在 Windows 不存在）
            subprocess.run(["taskkill.exe", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
            killed.append(pid)
        except Exception as e:
            print("  杀 PID", pid, "失败:", e)
if killed:
    print("  已终止占用端口", port, "的本项目进程:", sorted(set(killed)))
if skipped:
    print("  跳过端口", port, "上非本项目的进程:", sorted(set(skipped)), "(如确需释放请手动处理)")
if not killed and not skipped:
    print("  未发现占用端口", port, "的进程")
PY
}

# 启动单个服务（剥离终端，写 PID 仅供 status 展示；stop/watch 不依赖其精确性）
# $7 可选：环境变量前缀（如 "BLINDBOX_FE_PORT=5174 BLINDBOX_BE_URL=http://127.0.0.1:8001"）
# $8 可选：解释器（默认 $VENV_PY；Vite dev 传 node 的绝对路径）
launch() {
  local name=$1 dir=$2 cmdargs=$3 pidfile=$4 log=$5 port=$6 envp=${7:-} runner=${8:-"$VENV_PY"}
  if [ "$(port_listening $port)" = "yes" ]; then
    echo "[$name] 端口 $port 已被占用，跳过（如需接管请先 stop）。"
    return 0
  fi
  echo "[$name] 启动中… (端口 $port)"
  if command -v setsid >/dev/null 2>&1; then
    ( cd "$dir" && setsid env $envp "$runner" $cmdargs > "$log" 2>&1 & echo $! > "$pidfile" )
  else
    ( cd "$dir" && nohup env $envp "$runner" $cmdargs > "$log" 2>&1 & echo $! > "$pidfile" )
  fi
  sleep 3
  if [ "$(port_listening $port)" = "yes" ]; then
    echo "[$name] 已启动 (日志: $log)"
  else
    echo "[$name] 启动失败，最近日志："; tail -n 20 "$log" 2>/dev/null
  fi
}

start_backend()  { launch backend  "$BACKEND_DIR"  "-m uvicorn app.main:app --host 127.0.0.1 --port $BPORT"  "$BPID" "$BLOG" "$BPORT"; }
# 前端开发模式：设置了 DEV_MODE=1 且 frontend/node_modules 存在时，启动 Vite dev server（HMR）；
# 否则回退到免构建版 serve.py（托管 demo.html）。两种模式下前端都用自动分配的 FPORT。
# Vite 用 node 解释器跑（launch 第 8 参传 node 绝对路径），与 Python 的 serve.py 区分开。
start_frontend() {
  if [ "${DEV_MODE:-0}" = "1" ] && [ -d "$FRONTEND_DIR/node_modules" ] && [ -f "$FRONTEND_DIR/node_modules/vite/bin/vite.js" ]; then
    NODE_BIN="$(command -v node 2>/dev/null || true)"
    if [ -n "$NODE_BIN" ]; then
      launch frontend "$FRONTEND_DIR" \
        "node_modules/vite/bin/vite.js --port $FPORT --strictPort" \
        "$FPID" "$FLOG" "$FPORT" "BLINDBOX_BE_URL=http://127.0.0.1:$BPORT" "$NODE_BIN"
      return
    fi
    echo "  [dev] 未找到 node，回退到 serve.py（demo.html）"
  elif [ "${DEV_MODE:-0}" = "1" ]; then
    echo "  [dev] DEV_MODE=1 但 frontend/node_modules 缺失，回退到 serve.py（demo.html）"
  fi
  launch frontend "$FRONTEND_DIR" "serve.py"  "$FPID" "$FLOG" "$FPORT" "BLINDBOX_FE_PORT=$FPORT BLINDBOX_BE_URL=http://127.0.0.1:$BPORT"
}

stop_service() {
  local name=$1 port=$2 pidfile=$3
  echo "[$name] 停止中 (端口 $port)…"
  kill_port "$port"
  rm -f "$pidfile" 2>/dev/null || true
}

cmd=${1:-start}
BPORTS=""; FPORTS=""
case "$cmd" in
  start|watch)
    resolve_ports_for_start ;;
  stop|status|restart)
    # 管理类子命令必须作用在「在用端口」上；restart 停完后会再次按空闲端口分配
    resolve_ports_for_manage ;;
  *)
    BPORT=8000; FPORT=5173 ;;
esac

# 停掉一组在用端口上的服务；端口列表为空时明确提示，而不是去操作一个空闲端口
stop_all() {
  local any=no p
  for p in $FPORTS; do stop_service frontend "$p" "$FPID"; any=yes; done
  for p in $BPORTS; do stop_service backend  "$p" "$BPID"; any=yes; done
  if [ "$any" = "no" ]; then
    echo "未发现运行中的服务（8000-8019 / 5173-5192 均无监听）。"
    rm -f "$FPID" "$BPID" 2>/dev/null || true
  fi
}

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
    stop_all
    echo "已停止服务。"
    ;;
  restart)
    stop_all
    sleep 1
    # 停完后端口已释放，重新按空闲端口分配，避免沿用刚被 kill 的端口时 TIME_WAIT 抢不到
    resolve_ports_for_start
    start_backend
    start_frontend
    echo "------------------------------------------------------------"
    echo "  前端: http://127.0.0.1:$FPORT   (后端: http://127.0.0.1:$BPORT)"
    ;;
  status)
    echo "[backend]  在用端口: ${BPORTS:-无}   pid 文件: $(cat "$BPID" 2>/dev/null || echo -)"
    echo "[frontend] 在用端口: ${FPORTS:-无}   pid 文件: $(cat "$FPID" 2>/dev/null || echo -)"
    if [ -n "$FPORTS" ]; then
      for p in $FPORTS; do echo "  前端入口: http://127.0.0.1:$p"; done
    fi
    ;;
  *)
    echo "用法: $0 {start|watch|stop|restart|status}"; exit 1 ;;
esac
