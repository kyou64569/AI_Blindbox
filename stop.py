"""Stop AI Blind Box services.
1. Kill the launcher (start.py) first to prevent auto-restart.
2. Then kill any remaining processes on ports 8000 & 5173.

安全设计：只杀「属于本项目」的进程 —— 通过命令行里是否包含本脚本所在目录
（AI_Blindbox 项目根）来判定，避免误杀其他项目/应用的 python/node 进程。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _project_marker() -> str:
    """项目路径的 Windows 形式（小写，用于大小写不敏感的包含匹配）。"""
    return ROOT.replace("/", "\\").lower()


def _query_processes() -> dict[int, str]:
    """枚举本机 python/pythonw/node 进程 -> {pid: commandline}。

    只用这一个数据源（PowerShell Get-CimInstance，Win10/11 均可用），
    后续无论 kill launcher 还是 kill port 都基于它做归属判定。
    """
    result: dict[int, str] = {}
    try:
        ps_cmd = (
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
            "Name='pythonw.exe' or Name='node.exe'\" | "
            "ForEach-Object { Write-Output ($_.ProcessId.ToString() + '|' + $_.CommandLine) }"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            timeout=15,
        ).decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  [ERROR] 无法枚举进程: {e}")
        return result

    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pid_s, _, cmd = line.partition("|")
        if pid_s.isdigit():
            result[int(pid_s)] = cmd
    return result


def _belongs_to_project(cmd: str) -> bool:
    """命令行里是否引用本项目路径（大小写不敏感）。"""
    return _project_marker() in cmd.lower()


def _kill_pid(pid: int, label: str) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        print(f"  [ERROR] kill {label} PID {pid}: {e}")
        return False
    if result.returncode == 0:
        print(f"  [STOPPED] {label} PID {pid}")
        return True
    err = result.stderr.decode("gbk", errors="ignore").strip()
    print(f"  [FAILED] {label} PID {pid}: {err}")
    return False


def kill_port(port: int, processes: dict[int, str]) -> None:
    """Kill processes listening on `port` —— 但仅限归属本项目的进程。

    netstat 只给出端口上的 PID；PID 必须能在 processes 里查到、且命令行
    包含项目路径才会被杀。查不到/不属于项目的进程一律跳过，避免误杀
    其他占用了 8000/5173 的应用。
    """
    port_pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], timeout=5
        ).decode("gbk", errors="ignore")
        for line in out.splitlines():
            if f":{port} " not in line or "LISTENING" not in line:
                continue
            parts = line.strip().split()
            pid_str = parts[-1].strip()
            if pid_str.isdigit():
                port_pids.append(int(pid_str))
    except Exception as e:
        print(f"  [ERROR] scanning port {port}: {e}")
        return

    if not port_pids:
        print(f"  [SKIP]   port {port}: no process found")
        return

    killed = 0
    for pid in sorted(set(port_pids)):
        cmd = processes.get(pid)
        if cmd is None:
            print(f"  [SKIP]   port {port} PID {pid}: 非 python/node 进程，跳过")
            continue
        if not _belongs_to_project(cmd):
            print(f"  [SKIP]   port {port} PID {pid}: 非本项目进程，跳过")
            continue
        if _kill_pid(pid, f"port {port}"):
            killed += 1
    if killed == 0 and not any(pid in processes for pid in port_pids):
        print(f"  [INFO]   port {port}: 仅检测到非本项目进程，未做任何操作")


def kill_launcher(processes: dict[int, str]) -> int:
    """Kill start.py launcher processes —— 仅限命令行同时含 'start.py' 且属于本项目。

    旧实现只匹配 'start.py' 子串，会把任意目录下同名脚本一起杀掉；现在收窄。
    """
    killed = 0
    for pid, cmd in processes.items():
        if "start.py" not in cmd.lower():
            continue
        if not _belongs_to_project(cmd):
            continue
        if _kill_pid(pid, "launcher (start.py)"):
            killed += 1
    if killed == 0:
        print("  [SKIP]   launcher (start.py): 未发现本项目启动进程")
    return killed


def main() -> int:
    print("Stopping AI Blind Box services...")
    print()

    # 枚举一次进程表，后续所有归属判断共用
    processes = _query_processes()
    if not processes:
        print("  [WARN]  进程枚举为空（PowerShell 不可用？）。将跳过归属校验，"
              "仅当端口上的 PID 能被枚举到时才会尝试停止。")
        print()

    # 1) Kill the launcher first — this prevents auto-restart
    kill_launcher(processes)

    # 2) First pass: kill port listeners
    import time
    time.sleep(1)
    kill_port(8000, processes)
    kill_port(5173, processes)

    # 3) Second pass: kill any remaining launchers and listeners
    #    (some may have restarted between passes)
    time.sleep(1)
    processes = _query_processes()  # 重新枚举，进程可能已重启/换 PID
    kill_launcher(processes)
    time.sleep(0.5)
    kill_port(8000, processes)
    kill_port(5173, processes)

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
