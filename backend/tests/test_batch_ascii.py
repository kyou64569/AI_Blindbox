"""批处理脚本编码守卫：.cmd / .bat 必须是纯 ASCII。

为什么需要这个测试：
  cmd.exe 按 OEM codepage（中文系统=936/GBK）解析批处理文件。
  若文件里写了 UTF-8 中文，cmd 会把它解成乱码；乱码里出现的字节可能被
  当成命令分隔符，于是报出一堆莫名其妙的错：
      'xxx' 不是内部或外部命令，也不是可运行的程序
      错误: 无效参数/选项 - 'on'
  而且这类乱码错误**出现在错误的位置**，极难定位。

  曾经踩坑：stop_all.cmd 的 REM 注释写了中文，双击就报 7 行乱码错误。
  而 start_all.cmd 因为有 "keep this file ASCII-only" 的约定一直没事。

约定（本项目所有 .cmd/.bat 必须遵守）：
  1. 纯 ASCII：注释（REM）与 echo 文案一律英文
  2. 不加 UTF-8 BOM（cmd 会把 BOM 当成命令的一部分）
  3. 需要显示中文时，交给 Python 脚本（stop.py 等）输出，
     它内部走 UTF-8 重定向，不受 cmd codepage 影响
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _batch_files() -> list[Path]:
    """项目根下的所有 .cmd / .bat（后端与前端目录里的也一并检查）。"""
    files = sorted(PROJECT_ROOT.glob("*.cmd")) + sorted(PROJECT_ROOT.glob("*.bat"))
    for sub in ("backend", "frontend"):
        d = PROJECT_ROOT / sub
        if d.is_dir():
            files += sorted(d.glob("*.cmd")) + sorted(d.glob("*.bat"))
    return [f for f in files if f.is_file()]


def test_batch_scripts_exist():
    """至少要有启动/停止脚本，否则守卫形同虚设。"""
    names = {f.name.lower() for f in _batch_files()}
    assert "start_all.cmd" in names or "start_all.bat" in names, "找不到启动脚本"
    assert "stop_all.cmd" in names or "stop_all.bat" in names, "找不到停止脚本"


def test_batch_scripts_are_pure_ascii():
    """核心守卫：不允许任何非 ASCII 字节，也不允许 BOM。"""
    offenders = []
    for f in _batch_files():
        raw = f.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            offenders.append(f"{f.name}: 含 UTF-8 BOM（cmd 会把 BOM 当命令的一部分）")
        non_ascii = [(i, b) for i, b in enumerate(raw) if b > 127]
        if non_ascii:
            # 定位到行号，方便直接改
            lines = raw.split(b"\n")
            bad_lines = [i for i, ln in enumerate(lines, 1) if any(b > 127 for b in ln)]
            offenders.append(
                f"{f.name}: {len(non_ascii)} 个非 ASCII 字节，位于第 {bad_lines} 行"
            )
    assert not offenders, (
        "以下批处理含非 ASCII 字符，cmd.exe 在 GBK 代码页下会解析成乱码并报错：\n  "
        + "\n  ".join(offenders)
    )


def test_batch_scripts_have_no_stray_bom_midfile():
    """BOM 出现在文件中间通常是合并冲突残留，同样会让 cmd 解析错乱。"""
    for f in _batch_files():
        raw = f.read_bytes()
        assert b"\xef\xbb\xbf" not in raw[3:], f"{f.name}: 文件中部有 BOM 残留"
