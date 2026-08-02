import os

files = [
    r"D:/work/AI_Blindbox/backend/tests/_qa_podcast_recheck.py",
    r"D:/work/AI_Blindbox/backend/tests/_qa_verify_fix.py",
    r"D:/work/AI_Blindbox/backend/tests/_qa_result.txt",
    r"D:/work/AI_Blindbox/backend/tests/_qa_podcast2.py",
    r"D:/work/AI_Blindbox/backend/tests/_qa_podcast_out.txt",
    r"D:/work/AI_Blindbox/backend/tests/_qa_cleanup.py",
]
log = []
for p in files:
    try:
        if os.path.exists(p):
            os.remove(p)
            log.append(f"removed: {p}")
        else:
            log.append(f"absent: {p}")
    except Exception as e:
        log.append(f"ERROR {type(e).__name__}: {p} -> {e}")
with open(r"D:/work/AI_Blindbox/backend/tests/_qa_cleanup_log.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(log) + "\n")
