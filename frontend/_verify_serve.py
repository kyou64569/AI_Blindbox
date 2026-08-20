"""临时验证：serve.py 的 /output 路径归属判断是否真的挡住越权读取。"""
import sys
import urllib.parse
from pathlib import Path

OUTPUT_DIR = (Path(__file__).resolve().parents[1] / "output").resolve()


def old_check(path: str):
    """修复前的判定逻辑（字符串 startswith）。"""
    rel = urllib.parse.unquote(path[len("/output/"):])
    target = (OUTPUT_DIR / rel).resolve()
    allowed = str(target).startswith(str(OUTPUT_DIR)) and target.exists()
    return allowed, target


def new_check(path: str):
    """修复后的判定逻辑（relative_to + 绝对路径拦截 + is_file）。"""
    raw = path[len("/output/"):].split("?", 1)[0].split("#", 1)[0]
    rel = urllib.parse.unquote(raw)
    if not rel or Path(rel).is_absolute() or Path(rel).drive or rel[0] in "/\\":
        return False, None
    target = (OUTPUT_DIR / rel).resolve()
    try:
        target.relative_to(OUTPUT_DIR)
    except ValueError:
        return False, target
    return target.is_file(), target


# 找一个真实存在于 output_backup_* 里的文件，构造越权用例
sibling = None
for d in OUTPUT_DIR.parent.iterdir():
    if d.is_dir() and d.name.startswith("output") and d.name != "output":
        for f in d.rglob("*.md"):
            sibling = f
            break
    if sibling:
        break

cases = []
if sibling:
    rel = sibling.relative_to(OUTPUT_DIR.parent)
    cases.append(("同级 output_backup 越权(startswith 绕过)", "/output/../" + str(rel).replace("\\", "/")))
cases += [
    ("上跳读 settings.yaml", "/output/../backend/settings.yaml"),
    ("URL 编码上跳", "/output/..%2Fbackend%2Fsettings.yaml"),
    ("绝对路径", "/output//etc/passwd"),
    ("Windows 盘符", "/output/C:/Windows/win.ini"),
    ("目录本身", "/output/."),
    ("空路径", "/output/"),
]

fails = 0
print("%-38s %-8s %-8s" % ("用例", "修复前", "修复后"))
print("-" * 60)
for name, p in cases:
    a, _ = old_check(p)
    b, _ = new_check(p)
    tag = "OK" if not b else "!! 仍可读 !!"
    if b:
        fails += 1
    print("%-38s %-8s %-8s %s" % (name, "可读" if a else "已挡", "可读" if b else "已挡", tag))

# 正常文件必须仍然可读，避免过度收紧。浏览器会对中文/#/空格做百分号编码，
# 所以这里按浏览器实际发出的形式（quote 后）构造请求路径。
normals = list(OUTPUT_DIR.glob("result_*.json"))[:4]
# 特意挑一个名字里含 # 的，覆盖「剥锚点顺序」这个坑
hashy = next((f for f in OUTPUT_DIR.glob("*#*") if f.is_file()), None)
if hashy and hashy not in normals:
    normals.append(hashy)
print()
for normal in normals:
    encoded = "/output/" + urllib.parse.quote(normal.name)
    b, _ = new_check(encoded)
    label = normal.name[:34] + ("…" if len(normal.name) > 34 else "")
    print("%-38s %s" % ("正常产物可读: " + label, "PASS" if b else "FAIL"))
    if not b:
        fails += 1
if normals:
    b2, _ = new_check("/output/" + urllib.parse.quote(normals[0].name) + "?v=1")
    print("%-38s %s" % ("带查询串仍可读", "PASS" if b2 else "FAIL"))
    if not b2:
        fails += 1

print("\n结果: %s" % ("全部通过" if fails == 0 else "%d 项失败" % fails))
sys.exit(1 if fails else 0)
