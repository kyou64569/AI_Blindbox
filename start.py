"""AI Blind Box - unified launcher (robust version)

Starts backend (uvicorn) and frontend (serve.py), monitors health,
auto-restarts on crash. Ctrl+C to stop all services.
"""
import os
import sys
import time
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
VENV_PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"


def log(msg: str) -> None:
    """Print with timestamp, flush immediately so user sees it."""
    ts = time.strftime("%H:%M:%S")
    line = f"{ts}  {msg}"
    print(line, flush=True)


def kill_port(port: int) -> None:
    """Kill any process listening on given port."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], timeout=5
        ).decode("gbk", errors="ignore")
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.strip().split()
                pid = parts[-1].strip()
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=5)
                    log(f"killed old PID {pid} on port {port}")
    except Exception:
        pass


def health_check(port: int) -> bool:
    """Check if a service is responding on given port."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> None:
    log("=" * 50)
    log("  AI Blind Box Launcher")
    log("=" * 50)

    # Pre-flight
    if not VENV_PY.exists():
        log(f"FATAL: venv not found at {VENV_PY}")
        log("Please create venv and install deps (see README).")
        input("Press Enter to exit...")
        sys.exit(1)

    # Clean up old processes
    log("cleaning up old processes on ports 8000, 5173...")
    kill_port(8000)
    kill_port(5173)
    time.sleep(1)

    # Set up log files
    CACHE.mkdir(parents=True, exist_ok=True)
    backend_log = CACHE / "backend.log"
    frontend_log = CACHE / "frontend.log"

    # --- Start backend ---
    log("starting backend on port 8000...")
    be_fh = open(backend_log, "wb")
    be_proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND_DIR),
        stdout=be_fh,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    log(f"backend PID: {be_proc.pid}")
    time.sleep(3)

    # --- Start frontend ---
    log("starting frontend on port 5173...")
    fe_fh = open(frontend_log, "wb")
    fe_proc = subprocess.Popen(
        [str(VENV_PY), "serve.py"],
        cwd=str(FRONTEND_DIR),
        stdout=fe_fh,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    log(f"frontend PID: {fe_proc.pid}")
    time.sleep(2)

    # --- Wait and health check ---
    log("waiting for services (up to 20s)...")
    be_ok = fe_ok = False
    for attempt in range(7):
        time.sleep(3)
        if not be_ok:
            be_ok = health_check(8000)
        if not fe_ok:
            fe_ok = health_check(5173)
        status = f"[{attempt+1}/7] backend={'OK' if be_ok else '...'} frontend={'OK' if fe_ok else '...'}"
        log(status)
        if be_ok and fe_ok:
            break

    # --- Report ---
    if be_ok and fe_ok:
        log("=" * 50)
        log("  All services ready!")
        log("  Frontend: http://127.0.0.1:5173")
        log("  Backend:  http://127.0.0.1:8000")
        log("=" * 50)
        log("opening browser...")
        import webbrowser
        try:
            webbrowser.open("http://127.0.0.1:5173")
        except Exception:
            log("could not open browser, please open manually")
    else:
        if not be_ok:
            log("WARNING: backend failed to start. Check .cache/backend.log")
        if not fe_ok:
            log("WARNING: frontend failed to start. Check .cache/frontend.log")

    log("press Ctrl+C to stop all services")
    log("")

    # --- Monitor loop ---
    be_restart_delay = 1.0
    fe_restart_delay = 1.0
    try:
        while True:
            time.sleep(2)

            # Check backend
            ret = be_proc.poll()
            if ret is not None:
                log(f"backend exited (code {ret}), restarting in {be_restart_delay:.0f}s...")
                time.sleep(be_restart_delay)
                be_restart_delay = min(be_restart_delay * 2, 30.0)
                try:
                    be_fh.close()
                except Exception:
                    pass
                be_fh = open(backend_log, "ab")
                be_proc = subprocess.Popen(
                    [str(VENV_PY), "-m", "uvicorn", "app.main:app",
                     "--host", "127.0.0.1", "--port", "8000"],
                    cwd=str(BACKEND_DIR),
                    stdout=be_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                log(f"backend restarted (PID {be_proc.pid})")
            else:
                be_restart_delay = 1.0

            # Check frontend
            ret = fe_proc.poll()
            if ret is not None:
                log(f"frontend exited (code {ret}), restarting in {fe_restart_delay:.0f}s...")
                time.sleep(fe_restart_delay)
                fe_restart_delay = min(fe_restart_delay * 2, 30.0)
                try:
                    fe_fh.close()
                except Exception:
                    pass
                fe_fh = open(frontend_log, "ab")
                fe_proc = subprocess.Popen(
                    [str(VENV_PY), "serve.py"],
                    cwd=str(FRONTEND_DIR),
                    stdout=fe_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                log(f"frontend restarted (PID {fe_proc.pid})")
            else:
                fe_restart_delay = 1.0

    except KeyboardInterrupt:
        log("shutting down...")
        # Kill child process tree: terminate, then force-kill if still alive
        for proc in [fe_proc, be_proc]:
            if proc.poll() is None:  # still running
                try:
                    proc.terminate()
                except Exception:
                    pass
        time.sleep(2)
        for proc in [fe_proc, be_proc]:
            if proc.poll() is None:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(proc.pid)],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass
        time.sleep(1)
        log("all services stopped.")
        print()

    finally:
        # Close file handles
        for fh in [be_fh, fe_fh]:
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")
        sys.exit(1)
