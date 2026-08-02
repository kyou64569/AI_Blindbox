import subprocess, sys, os

PY = r"C:/Users/asus/.workbuddy/binaries/python/versions/3.13.12/python.exe"
TEST = r"D:/work/AI_Blindbox/backend/tests/test_config_fallback.py"
RES = r"D:/work/AI_Blindbox/backend/tests/_qa_result.txt"

lines = []
lines.append("===== QA 独立复验：TTS mock 短路修复 =====\n")

# ---- 1) pytest via subprocess (capture in-process) ----
r = subprocess.run([PY, "-m", "pytest", TEST, "-v"], capture_output=True, text=True)
lines.append("----- pytest 输出 -----")
lines.append(r.stdout)
if r.stderr.strip():
    lines.append("----- pytest stderr -----")
    lines.append(r.stderr)
lines.append(f"PYTEST_RC={r.returncode}")
passed = r.stdout.count(" PASSED")
failed = r.stdout.count(" FAILED") + r.stdout.count(" ERROR")
lines.append(f"COUNT passed_line={passed} failed_line={failed}")

# ---- 2) end-to-end podcast mock re-check ----
env = dict(os.environ)
chk = (
    "import sys; sys.path.insert(0, r'D:/work/AI_Blindbox/backend');"
    "from app import pipeline, config;"
    "config.SETTINGS.raw['mock']=True;"
    "res=pipeline.run_blind_box('podcast');"
    "config.SETTINGS.raw['mock']=False;"
    "out=res.output;"
    "with open(out,'rb') as f: head=f.read(12);"
    "print('KIND='+res.output_kind);"
    "print('OUT='+out);"
    "print('ENDS_WAV='+str(out.endswith('.wav')));"
    "print('NEQ_PLAN='+str(out!=res.plan));"
    "print('WAV_HEAD='+repr(head));"
    "print('IS_WAV='+str(head[:4]==b'RIFF' and head[8:12]==b'WAVE'));"
    "print('NO_REAL_CLOUD='+str(('stepaudio' not in out) and out.endswith('.wav')));"
    "print('PODCAST_OK='+str(res.output_kind=='audio_file' and out.endswith('.wav') and out!=res.plan and head[:4]==b'RIFF'));"
)
r2 = subprocess.run([PY, "-c", chk], capture_output=True, text=True)
lines.append("\n----- podcast mock 全链路复验 -----")
lines.append(r2.stdout)
if r2.stderr.strip():
    lines.append("STDERR:")
    lines.append(r2.stderr)

with open(RES, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
