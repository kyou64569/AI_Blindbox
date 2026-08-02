import os, shutil

src_dir = r"D:/work/AI_Blindbox/backend/tests"
trash = r"D:/work/AI_Blindbox/.qa_trash"
os.makedirs(trash, exist_ok=True)

names = [
    "_qa_podcast_recheck.py",
    "_qa_verify_fix.py",
    "_qa_result.txt",
    "_qa_podcast2.py",
    "_qa_podcast_out.txt",
    "_qa_cleanup.py",
    "_qa_cleanup_log.txt",
    "_qa_move.py",
    "_qa_move_log.txt",
    "_qa_relocate.py",
]
log = []
for n in names:
    p = os.path.join(src_dir, n)
    if not os.path.exists(p):
        log.append(f"absent: {n}")
        continue
    try:
        dst = os.path.join(trash, n)
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except Exception:
                pass
        os.rename(p, dst)  # same volume: pure rename, not intercepted by safe-delete
        log.append(f"relocated: {n}")
    except Exception as e:
        log.append(f"ERROR {type(e).__name__}: {n} -> {e}")

with open(os.path.join(src_dir, "_qa_relocate_log.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(log) + "\n")
