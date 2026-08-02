import sys, json
sys.path.insert(0, r"D:/work/AI_Blindbox/backend")
from app import pipeline, config

log = []
log.append("import OK")
log.append(f"syms: resolve={hasattr(pipeline,'_resolve_models')} fallback={hasattr(pipeline,'_call_with_fallback')} exc={hasattr(pipeline,'FallbackExhausted')} norm={hasattr(config,'_normalize_section')}")

# T1 check: load_settings normalized executor to models=[]
exec_sec = config.get_settings().agents.get("executor")
log.append(f"executor.models = {json.dumps(exec_sec.get('models'))}")
cre = config.get_settings().agents.get("creative")
log.append(f"creative.models[0].model = {cre.get('models',[{}])[0].get('model') if cre.get('models') else None}")

# T3 _resolve_models: empty field fallback
sec = {"base_url": "B", "api_key": "K", "models": [{"model": "m"}]}
g = {"base_url": "GB", "api_key": "GK"}
out = pipeline._resolve_models(sec, g)
log.append(f"_resolve_models group-level fallback: {json.dumps(out)}")
sec2 = {"base_url": "B", "api_key": "K", "models": [{"model": "m2", "base_url": "", "api_key": ""}]}
out2 = pipeline._resolve_models(sec2, g)
log.append(f"_resolve_models card empty -> group: {json.dumps(out2)}")
sec3 = {"models": [{"model": "mx", "base_url": "", "api_key": ""}]}
out3 = pipeline._resolve_models(sec3, g)
log.append(f"_resolve_models global fallback: {json.dumps(out3)}")

# T4 _executor_creds empty fallback chain (image+video top + global)
creds = pipeline._executor_creds("text")
log.append(f"_executor_creds(text) chain models: {[c['model'] for c in creds]}")

open(r"D:/work/AI_Blindbox/_verify.txt", "w", encoding="utf-8").write("\n".join(log))
