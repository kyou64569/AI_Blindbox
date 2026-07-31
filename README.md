# AI 盲盒（AI Blind Box）

一个多 Agent 提示词管道驱动的「盲盒」应用：用户选模式（图片 / 视频 / 小说 / 代码），点击开盲盒，
经「创意 Agent（高温度）→ 规划 Agent（低温度）→ 执行 Agent（按模式分发到多模态/文本/CLI）」随机生成成品。
随机性由 **twist pool 种子注入** 驱动，而非仅靠温度。

> 详细设计见 [AI盲盒-详细设计.md](./AI盲盒-详细设计.md)

## 目录结构（均在 D 盘，体积大的文件不落 C 盘）

```
D:\work\AI_Blindbox\
├─ backend\              # FastAPI 后端 + 管道编排（venv 在 backend/.venv）
│  ├─ app\
│  │  ├─ config.py       # 配置加载 + 大文件路径管理（默认 D 盘）
│  │  ├─ pipeline.py     # 盲盒管道：随机种子 → 创意 → 规划 → 执行
│  │  └─ main.py         # FastAPI 路由
│  ├─ settings.yaml      # 全局配置（模型/温度/twist pool/模式）
│  └─ requirements.txt
├─ frontend\             # React + Vite 工程（生产栈）
│  ├─ src\               # jsx 组件（App / ModeSelector / ResultPanel / ConfigPanel）
│  ├─ demo.html          # 免构建演示页（React/preact + 本地 vendor），无需 npm install
│  ├─ serve.py           # 免构建静态服务器 + /api 代理
│  └─ vite.config.js
├─ output\               # 生成产物（spec.md 等），D 盘
├─ .cache\               # 缓存日志，D 盘
└─ data\                 # 用户上传，D 盘
```

## 快速启动（两种前端）

### A. 免构建演示（最快，无需 npm install）
已验证可用。前端 `frontend/demo.html` 通过 `frontend/vendor/` 下的本地 preact/htm 运行，**无需联网、不依赖任何 CDN**（断网/被墙也能正常打开页面）。
```bash
# 1) 启动后端（D 盘 venv，注意不用裸 python）
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) 启动免构建前端（静态 + 代理）
backend\.venv\Scripts\python.exe frontend\serve.py
# 打开 http://127.0.0.1:5173
```

### B. 生产栈（React + Vite）
```bash
cd frontend
npm install            # 需联网；装到 frontend/node_modules（D 盘）
npm run dev            # http://127.0.0.1:5173 ，Vite 代理 /api -> 8000
```

## 一键启动（推荐）

双击 `start_all.cmd`（Windows 原生）：它会先后拉起后端(8000)与前端(5173)，并自动自检。
启动后浏览器打开 **http://127.0.0.1:5173**。

> **崩溃自动重启**：前后端各由一个「守护脚本」（`backend_run.cmd` / `frontend_run.cmd`）包着，
> 进程意外退出后会自动重新拉起（前端间隔 3 秒、后端 5 秒），并把输出写入 `.cache/*.log`。
> 因此窗口被误关、进程被回收都不会再导致「进不去」；重跑 `start_all.cmd` 时若端口已在监听会自动跳过，不会互相抢端口刷日志。

## 配置大模型（手动）
编辑 `backend/settings.yaml`：
- `global.default_*`：默认模型 / base_url / api_key（留 `<USER_KEY>` 走 mock）。
- `providers.image` / `providers.video`：图像/视频模型（本期用免费 Agnes，OpenAI 兼容）。
- 前端「配置」页也可可视化编辑并保存（写回 settings.yaml，仅限 D 盘项目目录）。

> mock 模式（`settings.yaml` 中 `mock: true`）：无真实 key 时本地规则产出，便于先跑通流程。

## 注意事项
- **大文件路径**：所有体积大的文件（产物 / 缓存 / 上传）默认落在 D 盘项目目录，绝不写 C 盘。
  可在 `settings.yaml` 的 `paths` 调整，但有安全检查拦截写入 C 盘敏感目录。
- 代码模式重档（code-cli 调 Claude Code/Codex）本期**仅预留接口**，未实现。
