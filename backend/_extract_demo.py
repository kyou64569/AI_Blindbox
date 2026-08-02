import os, re

print("cwd:", os.getcwd())
print("backend exists:", os.path.isdir(r"D:/work/AI_Blindbox/backend"))

src = open(r"D:/work/AI_Blindbox/frontend/demo.html", encoding="utf-8").read()
blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
print("num plain <script> blocks:", len(blocks))
for i, b in enumerate(blocks):
    print(f"  block {i}: len={len(b)} hasApp={'function App' in b}")

main = next((b for b in blocks if "function App" in b), None)
if not main:
    print("NO MAIN SCRIPT")
else:
    out = r"D:/work/AI_Blindbox/backend/_demo_main.js"
    open(out, "w", encoding="utf-8").write(main)
    print("wrote", out, "bytes", len(main))
