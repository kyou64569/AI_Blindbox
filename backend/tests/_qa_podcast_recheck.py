import sys
sys.path.insert(0, r"D:/work/AI_Blindbox/backend")
from app import pipeline, config

config.SETTINGS.raw["mock"] = True
res = pipeline.run_blind_box("podcast")
config.SETTINGS.raw["mock"] = False

print("kind=", res.output_kind)
print("output=", res.output)
print("ends_wav=", res.output.endswith(".wav"))
print("neq_plan=", res.output != res.plan)
with open(res.output, "rb") as f:
    head = f.read(12)
print("wav_head=", head)
print("is_valid_wav=", head[:4] == b"RIFF" and head[8:12] == b"WAVE")
print("not_real_cloud=", "stepaudio" not in res.output and res.output.endswith(".wav"))
print("OK" if (res.output_kind == "audio_file" and res.output.endswith(".wav")
               and res.output != res.plan and head[:4] == b"RIFF") else "FAIL")
