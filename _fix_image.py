p = r"D:/work/AI_Blindbox/backend/app/pipeline.py"
s = open(p, encoding="utf-8").read()

img_def = s.index('client.images.generate')
start = s.rindex('def call(creds: dict) -> str:', 0, img_def)
end_marker = 'return _call_with_fallback(models, call, ctx="executor:image")'
end = s.index(end_marker, img_def) + len(end_marker)

clean = '''        def call(creds: dict) -> str:
            client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=180, max_retries=0)
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    resp = client.images.generate(
                        model=creds["model"],
                        prompt=plan,
                        size="1024x1024",
                        n=1,
                    )
                    # 兼容 url / b64_json 两种返回
                    item = resp.data[0]
                    if getattr(item, "url", None):
                        return item.url
                    if getattr(item, "b64_json", None):
                        # 存为 D 盘文件（体积较大，落 output，不落 C 盘）
                        import base64
                        out_dir = SETTINGS.output_dir
                        img_path = out_dir / f"{_slugify(plan)}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(base64.b64decode(item.b64_json))
                        return str(img_path)
                    return "[图片生成返回为空]"
                except APITimeoutError as e:
                    # 图像 API 延迟尖峰，重试（多数情况下重抽会落回正常区间）
                    last_err = e
                    time.sleep(3)
                    continue
                except Exception as e:
                    # 鉴权失败 / 参数错误 / 模型不存在等：不重试，直接抛出
                    last_err = e
                    break
            raise RuntimeError(
                f"[图片执行 Agent 调用失败] model={creds['model']}  base_url={creds['base_url']}\n"
                f"错误：{type(last_err).__name__}: {last_err}\n"
                "（该图像接口存在明显延迟抖动，属偶发；可直接重试，或临时切 Mock 模式验证链路。）"
            )
'''

s = s[:start] + clean + s[end:]
open(p, "w", encoding="utf-8").write(s)
print("rewritten image call; changed:", True)
