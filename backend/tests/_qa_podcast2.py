import sys
sys.path.insert(0, r"D:/work/AI_Blindbox/backend")
from app import pipeline, config

config.SETTINGS.raw["mock"] = True
res = pipeline.run_blind_box("podcast")
config.SETTINGS.raw["mock"] = False

out = res.output
with open(out, "rb") as f:
    head = f.read(12)

print("KIND=" + res.output_kind)
print("OUT=" + out)
print("ENDS_WAV=" + str(out.endswith(".wav")))
print("NEQ_PLAN=" + str(out != res.plan))
print("WAV_HEAD=" + repr(head))
print("IS_WAV=" + str(head[:4] == b"RIFF" and head[8:12] == b"WAVE"))
print("NO_REAL_CLOUD=" + str(("stepaudio" not in out) and out.endswith(".wav")))
ok = (res.output_kind == "audio_file" and out.endswith(".wav")
      and out != res.plan and head[:4] == b"RIFF")
print("PODCAST_OK=" + str(ok))
