"""Stop AI Blind Box services.
1. Kill the launcher (start.py) first to prevent auto-restart.
2. Then kill any remaining processes on ports 8000 & 5173.
"""
import subprocess
import sys


def kill_port(port: int) -> None:
    """Kill all processes listening on the given port."""
    killed = 0
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], timeout=5
        ).decode("gbk", errors="ignore")

        for line in out.splitlines():
            if f":{port} " not in line or "LISTENING" not in line:
                continue
            parts = line.strip().split()
            pid_str = parts[-1].strip()
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                print(f"  [STOPPED] port {port} PID {pid}")
                killed += 1
            else:
                err = result.stderr.decode("gbk", errors="ignore").strip()
                print(f"  [FAILED] port {port} PID {pid}: {err}")
    except Exception as e:
        print(f"  [ERROR] scanning port {port}: {e}")
        return

    if killed == 0:
        print(f"  [SKIP]   port {port}: no process found")


def kill_launcher() -> None:
    """Kill the start.py launcher process to stop the auto-restart loop.

    Tries wmic first (Win10), falls back to PowerShell Get-CimInstance
    (Win11 24H2+ where wmic is deprecated).
    """
    pids: list[int] = []

    # 1) Try wmic (Win10)
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where",
             "name='python.exe' and commandline like '%%start.py%%'",
             "get", "processid"],
            timeout=10
        ).decode("ascii", errors="ignore")
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except Exception:
        pass

    # 2) Fallback: PowerShell Get-CimInstance (Win11+ where wmic is removed)
    if not pids:
        try:
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |"
                " Where-Object { $_.CommandLine -like '*start.py*' } |"
                " Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                timeout=10,
            ).decode("ascii", errors="ignore")
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
        except Exception:
            pass

    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
        print(f"  [STOPPED] launcher (start.py) PID {pid}")


def main() -> int:
    print("Stopping AI Blind Box services...")
    print()

    # 1) Kill the launcher first — this prevents auto-restart
    kill_launcher()

    # 2) First pass: kill port listeners
    import time
    time.sleep(1)
    kill_port(8000)
    kill_port(5173)

    # 3) Second pass: kill any remaining launchers and listeners
    #    (some may have restarted between passes)
    time.sleep(1)
    kill_launcher()
    time.sleep(0.5)
    kill_port(8000)
    kill_port(5173)

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
