# AI 盲盒（AI Blind Box）

一个多 Agent 提示词管道驱动的「盲盒」应用：用户选模式（图片 / Logo / 表情包 / 视频 / 小说 / 代码 / 小游戏 / 播客 / 塔罗 / 歌词 …… 共 **34 种**），点击开盲盒，
经「创意 Agent（高温度）→ 规划 Agent（低温度）→ 执行 Agent（按模式分发到多模态 / 文本 / 本地生成）」随机生成成品。
随机性由 **twist pool 种子注入** 驱动（而非仅靠温度），主题维度带**近期去重**，避免连续抽到同一主题。

> 详细设计见 [AI盲盒-详细设计.md](./AI盲盒-详细设计.md)

## 成品展示

以下是几次实际开盒的产物，覆盖换装捏脸 / 表情包 / 朋友圈三种模式：

| 换装捏脸 | 表情包 | 朋友圈 |
|:---:|:---:|:---:|
| ![](docs/images/gallery/04-character-cool.png) | ![](docs/images/gallery/05-meme-girl.png) | ![](docs/images/gallery/06-moments-warrior.png) |
| `04-character-cool.png` | `05-meme-girl.png` | `06-moments-warrior.png` |

## 特性一览

- **3-Agent 管道**：创意（发散）→ 规划（收敛为固定格式）→ 执行（按模式分发），每个 Agent 可独立配置模型 / base_url / key / 温度
- **异步开盒 + 进度反馈**：`POST /api/open` 立即返回 `task_id`，前端轮询 `GET /api/task/{id}` 展示「创意生成中 → 规划生成中 → 成品生成中」进度条与已等待秒数；支持**取消**（`POST /api/task/{id}/cancel`，阶段间生效）
- **批量开盒**：`POST /api/open-batch` 一次创建 1-6 个任务，前端并排展示进度与结果；**全局并发闸门**（同时最多 2 个任务执行，超出的排队）防止打爆模型 TPM 配额
- **多模型按序 fallback**：每个 Agent / Provider 可配多个模型，前一个失败自动用下一个（模型卡片可增删、排序）
- **34 种模式**：图片 / Logo 设计 / 表情包 / 四格漫画 / 播客(短中长) / 视频 / 微小说 / 诗歌 / 角色卡 / 起名 / 谜题 / 代码盲盒 / 小游戏 / 歌词创作 / 封面设计 / 广告文案 / 插画故事 / 歌词+哼唱 / 对话 / 世界观 / 冷笑话 / 创意食谱 / 社交文案 / 塔罗占卜 / 八卦新闻 / 平行宇宙 / AI 算命 / 恐怖一句话 / 对比图 / 朋友圈 / 趣味测试 / 密码暗号 / 换装捏脸 / 视频分镜
- **自定义模式**：前端「配置 → 模式管理 →＋ 新建模式」，表单校验（id 唯一 / executor_type / output_handler 枚举白名单），保存后立即可用
- **种子池管理**：全局 4 维随机池（theme/style/constraint/twist）可手动增删、🎲 大模型生成单条、一键整体刷新；**代码模式另有独立「工具池」**
- **代码模式专有**：
  - 产出结构化 `spec.md`（技术栈约束 / 目标用户与用途 / 功能闭环 / 环境依赖清单 / 磁盘占用预估 / MVP 定义 / 图片素材需求 / 验收标准）
  - **技术栈环境黑名单**：spec 若选择重型环境（UE5 / Unity / Blender / MySQL / CUDA / Docker 等 17 项可配），自动带警告重试规划一次，重试仍命中则在 spec 顶部插入环境体积警告
  - 前端揭示页展示「环境成本卡」（依赖清单 + 磁盘占用预估），命中黑名单时显示红色警告
- **结果归档**：每次开盒自动落盘 `output/result_*.json`（含 seed / 概念卡 / 规划 / 成品 / 所用模型 / **token 用量** / 收藏 / 评分），前端有历史面板，支持「用此 seed 重抽」
- **历史管理**：搜索（`?q=` 关键词）、按模式筛选、只看收藏、**收藏 / 评分**（1-5 星，就地写回归档）、**下载 ZIP 导出**（归档 JSON + 成品文件打包）
- **开盒统计**：`GET /api/stats` 聚合总开盒 / 成功率 / 模式分布 / 模型使用 / 平均评分 / Token 累计与预估成本（按 `global.tokens_price` ¥/千token）
- **Token 用量追踪**：contextvar 作用域累加整个管道（创意/规划/执行/重试）的模型 token，随归档落盘，前端结果区 / 历史 / 统计页展示
- **移动端适配**：≤640px / ≤380px 两档断点，模式网格 / 操作区 / 统计卡响应式
- **全免构建可用**：前端 `demo.html` 本地 vendor 运行，不依赖 CDN

## 目录结构（大文件默认放在项目目录内，路径可在 settings.yaml 的 paths 调整）

```
AI_Blindbox\
├─ backend\              # FastAPI 后端 + 管道编排（venv 在 backend/.venv）
│  ├─ app\
│  │  ├─ config.py       # 配置加载 + 大文件路径管理（默认项目目录内）
│  │  ├─ pipeline.py     # 主编排：run_blind_box（创意 → 规划 → 执行 → 归档）+ 兼容导出层
│  │  ├─ models.py       # BlindBoxResult / FallbackExhausted / TaskCancelled
│  │  ├─ token_usage.py  # contextvar token 用量追踪
│  │  ├─ tools.py        # 纯工具：slugify / 结构化块提取 / 黑名单扫描 / 高度注入等
│  │  ├─ agents.py       # 模型调用层：凭据解析 / fallback / mock
│  │  ├─ seed.py         # 种子池：draw_seed / generate_pool_seed / 锚定检测
│  │  ├─ executors.py    # 执行器：text / image / game / tts / story / song 等
│  │  ├─ archive.py      # 结果归档 + 远程产物下载
│  │  └─ main.py         # FastAPI 路由 + 异步任务系统（任务表 / 并发闸门 / 进度轮询）
│  ├─ settings.yaml      # 全局配置（模型/温度/种子池/模式/黑名单）——含真实 API key，**已被 gitignore**
│  ├─ settings.yaml.example  # 配置占位符示例（<YOUR_XXX_HERE>），唯一入库的配置文件
│  ├─ requirements.txt
│  └─ tests\             # 回归测试（test_*.py）
├─ frontend\             # React + Vite 工程（生产栈）
│  ├─ src\               # jsx 组件（App / ModeSelector / ResultPanel / ConfigPanel / HistoryPanel 等）
│  ├─ demo.html          # 免构建演示页（preact + 本地 vendor），无需 npm install
│  ├─ serve.py           # 免构建静态服务器 + /api 代理（API 响应带 no-store 防缓存）
│  └─ vite.config.js
├─ output\               # 生成产物（spec.md / 图片 / 音频 / result_*.json 归档）
├─ .cache\               # 守护进程日志
├─ data\                 # 用户上传
├─ docs\                 # 设计文档（system_design.md、mermaid 图）
├─ start_all.cmd / start.py / stop.py / stop_all.cmd   # 一键启停
└─ backend_run.cmd / frontend_run.cmd                  # 守护脚本（崩溃自愈）
```

## 快速启动（两种前端）

### A. 免构建演示（最快，无需 npm install）

前端 `frontend/demo.html` 通过 `frontend/vendor/` 下的本地 preact/htm 运行，**无需联网、不依赖任何 CDN**。
```bash
# 1) 启动后端（注意：不要用系统裸 python，用项目 venv）
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) 启动免构建前端（静态 + 代理）
backend\.venv\Scripts\python.exe frontend\serve.py
# 打开 http://127.0.0.1:5173
```

### B. 生产栈（React + Vite）

```bash
cd frontend
npm install            # 需联网；装到 frontend/node_modules
npm run dev            # http://127.0.0.1:5173 ，Vite 代理 /api -> 8000
```

## 一键启动（推荐）

双击 `start_all.cmd`：先后拉起后端(8000)与前端(5173)，并自动自检。启动后浏览器打开 **http://127.0.0.1:5173**。

> **崩溃自动重启**：前后端各由守护脚本（`backend_run.cmd` / `frontend_run.cmd`）包裹，
> 进程意外退出后自动重新拉起（前端间隔 3 秒、后端 5 秒），输出写入 `.cache/*.log`。
> 重跑 `start_all.cmd` 时若端口已监听会自动跳过，不会互相抢端口。
> 端口被占用时自动 +1 更换（后端 8000-8019 / 前端 5173-5192）。

> **开发模式（Vite HMR）**：Git Bash 下 `DEV_MODE=1 ./start_all.sh` 且 `frontend/node_modules` 存在时，
> 前端自动改用 Vite dev server（热更新）；缺失时回退 `serve.py`（demo.html）。

## 配置大模型

### 方式一：前端「配置」页（推荐）
可视化编辑模型 / key / 种子池 / 代码工具池 / 自定义模式，保存后写回 `backend/settings.yaml`。要点：
- **模型选择**：填好 base_url / api_key 后点「获取模型列表」拉取该供应商可用模型，从下拉选择（列表外的模型可通过「自定义」输入）
- **多模型 fallback**：每个 Agent / Provider 可添加多张模型卡片（增删 / 排序），前一个失败自动用下一个
- **随机种子池**：全局 4 维池 + **代码模式独立工具池**（code 专用），均支持手动增删、🎲 生成新词条、一键刷新
- **自定义模式**：「＋ 新建模式」表单（id 唯一、executor_type / output_handler 枚举校验），保存后立即可用
- **成本估算**：`global.tokens_price` 每千 token 单价（¥），历史 / 统计页按此换算预估成本

### 方式二：直接编辑 `backend/settings.yaml`
- `global.default_*`：默认模型 / base_url / api_key（各 Agent / Provider 留空时回退）
- `agents.creative / planner / executor`：三个 Agent 各自模型配置（温度：创意 ~0.9、规划 ~0.2、执行 ~0.8）
- `providers.image / video / tts / game`：多模态模型（OpenAI 兼容）
- `randomness.pool`：全局种子池；`modes[code].pool`：代码模式独立工具池
- `modes[code].blocked_env`：代码模式技术栈黑名单（默认 17 项重型环境关键词）
- `mock: true`：无真实 key 时本地规则产出，便于先跑通流程

> `settings.yaml` 含真实 API key，已被 `.gitignore` 忽略，**不会上传到 git**；入库的只有占位符示例 `settings.yaml.example`。
> 两个文件的**模式（modes）定义保持一致**：数量、id、名称、创意提示词、规划模板均同步；差异仅在凭据（example 用 `<YOUR_XXX_HERE>` 占位符）与运行配置（真实模型列表 / 种子池 / 路径）。

## 代码模式说明

代码模式（id=`code`）**只生成 spec.md**（一份可直接交给编程 Agent 如 Claude Code / OpenCode 使用的项目需求蓝图），不直接产出代码。spec 包含：
技术栈环境约束、项目名称、创意概念、技术栈、目录结构（单文件或 ≤10 文件小工程）、目标用户与用途、功能闭环、模块拆解、图片素材需求、环境依赖清单、磁盘占用预估、MVP 定义、验收标准。

两道保护机制，防止抽出的 spec"开发成本爆炸"：
1. **独立工具池**：代码盲盒从独立种子池抽取（本地文件处理 / 数据转换 / 个人记账 / 时间管理 / 创作辅助 等实用工具方向），与全局"意象池"互不影响
2. **技术栈环境黑名单**：spec 若出现重型环境关键词（`blocked_env`，默认含 unreal/unity/godot/blender/mysql/cuda/docker 等），自动带警告重试规划一次；重试仍命中则在 spec 顶部插入**环境体积警告**，前端揭示页显示红色警告 + 环境成本卡（依赖 / 磁盘预估）

## 后端 API 摘要

| 端点 | 说明 |
|------|------|
| `GET  /api/modes` | 模式列表（含各模式模板 / 独立池 / 黑名单配置） |
| `POST /api/open` | **异步**开盲盒：`{mode, seed?}` → 立即返回 `{task_id, status}` |
| `GET  /api/task/{task_id}` | 轮询任务状态：`{status, stage, stage_label, progress(0-1), elapsed, result?, error?}` |
| `POST /api/task/{task_id}/cancel` | 取消任务（阶段间生效，排队中可直接退出） |
| `POST /api/open-batch` | 批量开盒：`{mode, count(1-6), seed?}` → 返回 N 个 `task_id`（并发受全局闸门限制） |
| `GET  /api/history` | 历史记录（按时间倒序，支持 `?mode=` 过滤与 `?q=` 关键词搜索） |
| `POST /api/history/{rid}/favorite` | 收藏 / 取消收藏 `{favorite: bool}`（就地写回归档） |
| `POST /api/history/{rid}/rating` | 评分 1-5 或清除 `{rating: 1-5 \| null}` |
| `GET  /api/export/{rid}/zip` | 按归档 id 打包下载 ZIP（归档 JSON + 成品文件） |
| `GET  /api/stats` | 开盒统计（总数 / 成功率 / 模式 / 模型 / 评分 / 收藏 / token / 成本） |
| `GET  /api/config` | 读取配置；`POST` 保存（写回 settings.yaml 并热更新） |
| `POST /api/models` | 按 base_url + api_key 获取该供应商模型列表 |
| `POST /api/generate-seed` | 为某维度生成一条新种子（`{dimension, mode_id?}`，mode_id 指定时用该模式独立池） |
| `POST /api/seed/refresh-all` | 一键刷新种子池（`{mode_id?}`，缺省刷全局） |
| `GET  /api/tts/voices` | 列出可用 TTS 音色 |
| `GET  /output/{path}` | 静态访问生成产物（图片 / 音频 / spec / 归档 JSON） |
| `GET  /` | 健康检查（返回 status + mock 开关状态） |

## 注意事项

- **异步任务**：`/api/open` 与 `/api/open-batch` 均返回 `task_id`，前端轮询进度；任务表为内存存储（上限 200 自动裁剪），进程重启后运行中的任务丢失（前端轮询 404 → 提示重试）。
- **并发闸门**：全局同时最多 **2 个**开盒任务执行（真实模型 TPM 配额脆弱，批量全并发实测会触发 429）；超出任务保持「排队中」直到有空位。
- **大文件路径**：产物 / 缓存 / 上传等大文件默认放在项目目录内（可在 `settings.yaml` 的 `paths` 调整，支持相对路径，基于项目根解析）；建议不要放到系统盘，配置里有安全检查拦截写入系统盘敏感目录。
- **代码模式重档（code-cli 调 Claude Code / Codex）本期仅预留接口，未实现**；当前产出 spec.md，由用户自行用编程 Agent 开发。
- **主题去重**：最近 3 次用过的主题不会立即重复出现，记录在 `output/recent_themes.json`（持久化，随产物目录走）。
- **git 安全**：`backend/settings.yaml`、`.env`、venv、产物目录均被 `.gitignore` 忽略；`_` 前缀的临时调试文件约定不入库。
