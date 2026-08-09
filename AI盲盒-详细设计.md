# AI 盲盒（AI Blind Box）详细设计文档

> 版本：v1.0
> 日期：2026-07-27
> 状态：方案设计（待评审）

---

## 1. 文档目的

本文档将「AI 盲盒」产品的需求讨论结论固化为可落地的技术设计，覆盖：

- 产品形态与核心机制
- 多 Agent 管道架构（含温度/人设策略）
- 盲盒随机性（惊喜感）的工程化实现
- 模式配置规范（图片 / 视频 / 小说 / 代码）
- 代码模式的轻/重两档设计
- 2-Agent 与 3-Agent 管道模式开关
- 技术选型与配置结构
- 端到端流程、揭示 UX、风险与落地路线

---

## 2. 产品定位与目标

### 2.1 一句话定义
用户选择一个「模式」（图片 / 视频 / 小说 / 代码等），点击「开启盲盒」，系统经由一条多 Agent 管道，随机生成一份**有惊喜感**的创意成品。

### 2.2 核心体验目标
1. **惊喜感**：每次开盒结果不同且有趣，而非千篇一律。
2. **确定性可控**：随机可复现（存 seed 可重抽），不翻车。
3. **低门槛**：所有大模型由用户手动配置，产品本身不绑定任何供应商。
4. **可扩展**：新增模式不改代码，仅加配置。

### 2.3 设计原则
- **管道即配置**：模式、Agent 行为、随机池全部可配置，代码只做编排。
- **单模型起步，多模型可选**：默认复用同一底座模型，仅靠参数+人设区分 Agent；执行 Agent 天然可指定多模态模型。
- **惊喜来自「种子」，而非仅温度**：温度只管发散，随机性由「种子池」保证。
- **轻量优先**：代码模式先做文档档，CLI 档作为进阶开关。

---

## 3. 系统总体架构

系统是一条串行的提示词管道，整体分为四层：

```
[用户交互层]  选模式 + 开盲盒 + 看结果
      │
[编排层]     管道控制器（读配置、串 Agent、管随机种子、分发模式）
      │
[智能层]     Agent1 创意 → Agent2 规划 → Agent3 执行（按模式分发）
      │
[模型/工具层] 文本模型 / 图像模型 / 视频模型 / CLI 工具
```

管道主链路（v1 标准模式，3 Agent）：

```
用户选模式+开盲盒
       │
       ▼
[随机种子注入]  ← twist pool（主题/风格/约束/反转）
       │
       ▼
[Agent1 创意]  temp≈0.9，天马行空的概念描述（概念卡素材）
       │
       ▼
[Agent2 规划]  temp≈0.2，把概念收敛为固定格式 prompt / spec
       │
       ▼
[Agent3 执行]  按模式分发：
       ├─ 图片 → 图像模型
       ├─ 视频 → 视频模型
       ├─ 小说 → 文本模型
       └─ 代码 → 轻档：spec.md / 重档：CLI（Claude Code / Codex）
       │
       ▼
[成品 + 概念卡]  揭示展示
```

---

## 4. 盲盒随机性机制（核心）

盲盒的「随机」不能依赖 `temperature` 单点——高温只让输出发散，不保证「每次不同且有趣」。采用**随机种子注入**：

### 4.1 Twist Pool（随机池）
维护若干维度的候选词，开盒时随机抽 1~2 个注入创意 Agent 的 system prompt：

| 维度 | 作用 | 示例 |
|------|------|------|
| 主题池 theme | 决定题材方向 | 赛博朋克废墟、深海城邦、唐代市井 |
| 风格池 style | 决定视觉/语感 | 水墨写真、蒸汽波、冷峻纪实 |
| 约束池 constraint | 强制出现的元素 | 必须出现一只橘猫、仅用三种颜色 |
| 反转池 twist | 制造出乎意料 | 主角其实是 AI、结局是开场 |

### 4.2 种子可复现
- 每次开盒生成一个 `seed`（如 `theme_03+style_11+twist_02`）。
- 记录 seed 与最终成品的映射，用户可「用同一 seed 重抽」。
- 配置项：`randomness: on/off`、`pool_weights`、`per_open_draw: 1~2`。

---

## 5. Agent 设计

所有 Agent 抽象为统一配置对象：

```yaml
agent:
  model: <model_name>          # 可覆盖全局模型
  base_url: <openai_compatible_url>
  api_key: <user_provided>
  temperature: <float>
  max_tokens: <int>
  system_prompt: <persona>
```

### 5.1 Agent1 创意（Creative）
- **温度**：0.8 ~ 1.0（高发散）
- **人设**：脑洞制造机，敢于打破常规，产出「概念」而非成品。
- **输入**：模式系统提示词 + 抽中的随机种子。
- **输出**：一段自由文本「概念描述」（同时作为揭示页的「概念卡」文案）。
- **关键**：它不需要结构化，越野生越好；结构化交给 Agent2。

### 5.2 Agent2 规划（Planner）
- **温度**：0.1 ~ 0.3（低发散、高稳定）
- **人设**：严谨的格式化工程师，只做「收敛与翻译」。
- **输入**：Agent1 的概念描述 + 本模式的「规划模板」。
- **输出**：**固定格式**的 prompt / spec（图片/视频给精炼 prompt；代码给 spec.md 草稿）。
- **价值**：把散文翻译成执行模型要吃干净的输入。图片/视频模型对此高度依赖。
- **成本**：可用最便宜、最快的小模型，几乎不增加开销。

### 5.3 Agent3 执行（Executor）
- **温度**：依执行类型而定（图像/视频模型通常有自身参数；文本 0.7~0.9）。
- **职责**：按当前模式把 Agent2 的输出发给对应模型/工具，拿回成品。
- **多模态必然多模型**：图片→图像模型、视频→视频模型，天然是用户单独配置的模型。
- **代码模式特殊**（见第 7 节）。

---

## 6. 管道模式开关（2-Agent / 3-Agent）

规划 Agent 是最「可裁」的一环。提供开关：

| 模式 | 链路 | 适用 |
|------|------|------|
| 精简模式（2 Agent） | 创意 → 执行 | 执行模型 tolerant（可接受松散描述）；省一次调用、低延迟低成本 |
| 标准模式（3 Agent） | 创意 → 规划 → 执行 | 图片/视频/代码-CLI 等需要严格结构化输入；稳定性优先（默认） |

- 精简模式下，创意 Agent 被要求**一段式输出**：先脑暴，末尾按固定格式给出 prompt（中等温度 + 强格式指令）。
- 默认推荐：**标准模式**。

---

## 7. 代码模式：轻档 / 重档

代码模式与其它模式不同——它的「成品」可以是文档，也可以是真工程。

### 7.1 轻档（默认）：生成 spec.md
- Agent2 直接产出结构化需求文档：技术栈建议、目录结构、模块拆解、关键接口、验收标准。
- 这就是盲盒结果，用户拿走自行开发。
- **优点**：零风险、零额外成本、最贴合盲盒「给蓝图」的本质。

### 7.2 重档（进阶开关）：调用 CLI 直接生成
- **决策（Q2）**：本期（v1）**仅预留接口，不实现**。管道与配置中保留 `code-cli` 类型与 `code_cli` 配置位，但不接 CLI 调用逻辑；待用户后续决定再落地。
- 开启后，把 spec 喂给 **Claude Code CLI / Codex CLI** 在**沙箱**内产出脚手架。
- **工程注意**：
  - 以子进程方式 spawn，捕获流式输出，设置超时与出错处理。
  - 必须处理 CLI 自身的鉴权。
  - **安全**：CLI 有执行命令、改文件权限，必须在沙箱/受限目录运行，默认关闭。
  - 成本高、耗时长（可能跑数分钟到数十分钟），需异步任务 + 进度展示。
- **默认关闭**，开启时明确提示风险。

---

## 8. 模式配置规范

模式不写死在代码，而是「配置模板」。字段设计：

| 字段 | 说明 |
|------|------|
| `id` | 模式标识（image/video/novel/code） |
| `name` | 展示名 |
| `creative_prompt` | 注入创意 Agent 的模式系统提示词 |
| `planner_template` | 规划 Agent 的输出格式模板（决定产出结构） |
| `executor_type` | `image` / `video` / `text` / `code-spec` / `code-cli` |
| `output_handler` | 成品如何接收与展示（图片 URL / 视频文件 / 文本 / md 文件） |
| `model_override` | 可选，覆盖该模式执行 Agent 的模型 |

各模式要点：
- **图片**：`executor_type=image`，规划产出精炼英文 prompt（可带负面词、尺寸、风格）。
- **视频**：`executor_type=video`，规划产出镜头/分镜描述。
- **小说**：`executor_type=text`，规划产出大纲+正文要求，执行直接出文。
- **代码**：`executor_type=code-spec`（默认）或 `code-cli`（进阶）。

---

## 9. 技术选型建议

### 9.1 后端（编排层 + 智能层）
- **推荐：Python + FastAPI**（异步、轻量，适合串行/可并行 LLM 调用）。
- 备选：Node.js（若团队更熟 JS）。
- 统一用 **OpenAI 兼容 SDK**（`openai` 库，按 Agent 配置 `base_url`/`api_key`），从而支持任意供应商。

### 9.2 前端（交互层）

三种候选形态对比：

| 形态 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| 原生单页（HTML/JS） | 零构建、最快上线、改完即看；盲盒 UI 简单时足够 | 状态/表单一多就难维护；配置 UI 需手写 | 想极速验证、UI 极简 |
| React（Vite） | 组件化、状态管理清晰，最适合「可视化配置 UI」；生态成熟 | 需构建工具链（Vite/Node）；体积略大 | 需要配置 UI、后期要扩展（推荐） |
| 桌面壳（Electron/Tauri） | 可离线、直接读写本地文件（spec.md / CLI 沙箱更顺）；可分发成 App | 打包复杂、体积大（Electron 尤甚）；Tauri 需 Rust 环境 | 想要原生分发 / 深度本地集成 |

**决策（Q1，已锁定）：采用 React + Vite 单页 + 可视化配置 UI**。后端已是 FastAPI，前端用 React 组件化管理模式切换、配置表单、结果展示等带状态的交互；桌面壳（Electron/Tauri）暂缓，除非后续要做离线/原生分发。原生单页仅在想极速验证演示时可临时采用。

核心页面：模式选择 + 开盒按钮 + 结果揭示（概念卡 + 成品）+ 配置页。

### 9.2.1 本地大文件存储约束（重要）
- 用户环境约束：**下载/生成的体积较大文件（模型权重、缓存、配置附件、生成产物等）默认放在项目目录内，避免写入系统盘（含用户目录 AppData、桌面、文档等）**。
- 具体约定：
  - 配置主目录：项目根（`backend/settings.yaml` 的 `paths` 可配置）。
  - 大文件/缓存目录：`.cache/`（模型缓存、依赖缓存）、`output/`（生成图片/视频/代码产物）、`data/`（用户上传/导入），均相对项目根解析。
  - 仅允许放在项目内的小文件：极小的纯文本配置（`settings.yaml`）、前端构建产物（若用户接受；否则也放项目目录）。
  - 实现层：后端所有写大文件路径统一拼接 `.cache`/`.output`/`.data`；给出路径配置项，默认指向项目目录内，便于迁移。

### 9.3 模型调用抽象
- 每个 Agent 一次 `chat.completions` 调用（或对应多模态接口）。
- 配置驱动：模型、温度、key 全部来自配置，不硬编码。

### 9.4 图像 / 视频
- 通过各自供应商 API（OpenAI Images / Stability / 本地 SD 服务 / 视频生成 API）。
- 抽象为 `image_provider` / `video_provider`，配置 `base_url`/`key`/`model`。
- **决策（Q5）**：图像/视频模型由用户手动配置，本期使用免费的「Agnes」模型，经 OpenAI 兼容接口接入；配置抽象保证可随时替换为其它模型。

### 9.5 CLI 集成（代码重档）
- `subprocess` 调用 `claude` / `codex`，stdout 流式回传，超时与沙箱隔离。

### 9.6 配置存储
- **推荐：`settings.yaml`**（或 JSON），存放：全局/各 Agent 模型配置、twist pool、模式定义。
- 敏感信息（api_key）建议本地存储、不入库。

### 9.7 可视化配置 UI（决策 Q3：需要）
- 提供网页表单编辑 `global / agents / randomness / modes / providers`，保存回 `settings.yaml`（或原生存储镜像）。
- 降低手动改 YAML 门槛：api_key、模型名、twist pool 词条的可视化增删改。
- 与 9.2 推荐的 React 前端天然契合。

---

## 10. 配置结构示例（YAML）

```yaml
# settings.yaml —— AI 盲盒全局配置

global:
  default_model: "gpt-4o-mini"        # 可被各 Agent 覆盖
  default_base_url: "https://api.openai.com/v1"
  default_api_key: "<USER_KEY>"
  pipeline_mode: "standard"           # standard(3Agent) | lite(2Agent)

agents:
  creative:
    model: "gpt-4o"
    temperature: 0.9
    system_prompt: |
      你是一个脑洞制造机。敢于打破常规，产出天马行空的概念描述。
      你会收到随机种子，必须把它们编织进创意里。
  planner:
    model: "gpt-4o-mini"              # 可 cheapest
    temperature: 0.2
    system_prompt: |
      你是严谨的格式化工程师。把创意概念收敛为固定格式的输出，不要发挥。
  executor:
    temperature: 0.8

randomness:
  enabled: true
  per_open_draw: 2
  pool:
    theme: ["赛博朋克废墟", "深海城邦", "唐代市井", "末日温室"]
    style: ["水墨写真", "蒸汽波", "冷峻纪实", "童话绘本"]
    constraint: ["必须出现一只橘猫", "仅用三种颜色", "含一段独白"]
    twist: ["主角其实是 AI", "结局即开场", "无声胜有声"]

modes:
  - id: image
    name: 图片
    creative_prompt: "构思一张令人惊艳的插画概念。"
    planner_template: "输出英文 image prompt + 负面词 + 尺寸。"
    executor_type: image
    output_handler: image_url
  - id: video
    name: 视频
    creative_prompt: "构思一段短视频的创意。"
    planner_template: "输出分镜描述 + 风格 + 时长。"
    executor_type: video
    output_handler: video_file
  - id: novel
    name: 小说
    creative_prompt: "构思一个短篇故事概念。"
    planner_template: "输出大纲 + 正文写作要求。"
    executor_type: text
    output_handler: text
  - id: code
    name: 代码
    creative_prompt: "构思一个有趣的小项目创意。"
    planner_template: "输出技术栈/目录/模块拆解/验收标准。"
    executor_type: code-spec        # 或 code-cli（进阶）
    output_handler: spec_md
    code_cli:
      enabled: false                # 默认关
      tool: "claude"                # claude | codex
      sandbox: true

providers:
  image:
    base_url: "<IMAGE_API>"
    api_key: "<USER_KEY>"
    model: "dall-e-3"
  video:
    base_url: "<VIDEO_API>"
    api_key: "<USER_KEY>"
    model: "video-model-x"
```

---

## 11. 端到端流程（时序）

1. 用户选模式 M，点击「开盲盒」。
2. 编排层生成 `seed`，从 twist pool 抽词，组装创意 Agent 的 system prompt。
3. 调用 **Agent1 创意** → 得到概念文本 `C`（保存为概念卡）。
4. 若 `pipeline_mode=standard`：调用 **Agent2 规划**（喂 `C` + 模式模板）→ 得到结构化 `P`。
   若 `lite`：Agent1 直接产出结构化 `P`。
5. 调用 **Agent3 执行**：
   - image/video：转发 `P` 到对应 provider，取回文件 URL。
   - novel：转发 `P` 到文本模型，取回正文。
   - code：
     - `code-spec`：保存 `P` 为 `spec.md`。
     - `code-cli`：异步 spawn CLI，流式回传，产出脚手架（沙箱内）。
6. 揭示页展示「概念卡 C + 成品」。

---

## 12. 揭示与展示 UX

- **概念卡**：展示 Agent1 的天马行空描述（盲盒「我抽到了什么」的爽点）。
- **成品区**：图片/视频直接预览；小说全文展示；代码给出 `spec.md` 下载或 CLI 产物目录。
- **重抽**：展示所用 `seed`，支持「同 seed 重抽」与「换 seed 再抽」。
- **模式切换**：顶部模式选择，开盒按钮醒目。

---

## 13. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 高温导致创意 incoherent | 由低温度规划 Agent 收敛；概念卡允许野生，但成品 prompt 必结构化 |
| 随机结果翻车/无意义 | twist pool 限定在合理语义域；规划 Agent 兜底过滤 |
| 代码 CLI 安全风险 | 默认关闭、沙箱运行、限时、仅限受限目录 |
| 多模型配置复杂 | 统一 OpenAI 兼容抽象；默认单模型复用 |
| 视频/图像成本高 | 限制分辨率/时长；可异步 + 队列 |
| 盲盒不够随机 | 种子池驱动，而非仅靠温度 |

---

## 14. 落地路线图

1. **阶段一**：打通「图片模式」标准管道（创意→规划→图像模型），验证惊喜感；搭 React+Vite 前端 + FastAPI 后端骨架。
2. **阶段二**：接入 twist pool，把随机从玄学变可控；加概念卡揭示。
3. **阶段三**：复用管道加「小说 / 视频」模式（均用免费 Agnes 模型手动配置）。
4. **阶段四**：代码模式实现 `code-spec` 档（生成 spec.md）；`code-cli` 重档**仅留接口**，暂不实现。
5. **阶段五**：2-Agent 精简模式开关；**可视化配置 UI** 落地（对应 Q3）。

---

## 15. 决策记录（基于 2026-07-27 用户答复）

| # | 问题 | 决策 |
|---|------|------|
| 1 | 前端形态 | **已锁定：React + Vite 单页 + 可视化配置 UI**（见 9.2）；桌面壳暂缓 |
| 2 | 代码重档 | 本期**仅预留接口**，不实现（见 7.2） |
| 3 | 配置方式 | **带可视化配置 UI**（见 9.7） |
| 4 | 账号体系 | **暂不需要**（无账号 / 无历史记录） |
| 5 | 图像/视频模型 | 用户手动配置，本期用免费「Agnes」模型（OpenAI 兼容接口，见 9.4） |

> 待用户就 Q1 前端形态最终确认后，本文档 9.2 与路线图可锁定。

---

## 16. 已落地实现骨架（2026-07-27）

按本设计已搭建可运行骨架，全部位于项目根目录内，体积大的文件（venv/产物/缓存/上传）默认放在项目目录内、避免写入系统盘。

### 16.1 已验证
- 后端 FastAPI 启动正常，管道（image/video/novel/code 四模式）在 mock 模式下跑通，code 模式 spec 文件正确落盘到 `output/`（项目产物目录）。
- 免构建前端 `demo.html`（React/preact + CDN）通过 `serve.py` 静态服务 + `/api` 代理，与后端端到端联通（已 curl 验证开盲盒返回完整概念卡/规划/成品）。

### 16.2 代码结构
- `backend/app/config.py`：配置加载 + 大文件路径管理（默认 D 盘，含 C 盘敏感目录安全拦截）。
- `backend/app/pipeline.py`：盲盒管道（随机种子 → 创意 → 规划 → 执行），OpenAI 兼容调用 + mock 兜底。
- `backend/app/main.py`：FastAPI 路由（modes/config/open）。
- `backend/settings.yaml`：全局配置（含 twist pool、模式、providers、mock 开关）。
- `frontend/src/*`：React(Vite) 生产栈组件（App / ModeSelector / ResultPanel / ConfigPanel）。
- `frontend/demo.html` + `frontend/serve.py`：免构建演示入口，无需 npm install。

### 16.3 启动方式
- 免构建（已验证）：先后启动 `uvicorn app.main:app` 与 `frontend/serve.py`，访问 `http://127.0.0.1:5173`。
- 生产栈：前端 `npm install && npm run dev`（Vite 代理 `/api` 到 8000）。

### 16.4 待补（下一轮）
- 接入真实 Agnes 模型（替换 settings 中占位 base_url/key）。
- 图像/视频执行器真实调用（pipeline `_run_executor` 的 TODO）。
- novel 执行器再调一次文本模型生成正文（当前 mock 用规划输出充当）。
- 前端 npm 安装在本执行沙箱异常（环境限制），用户本地可正常安装。

## 17. 本轮更新（2026-07-27 晚）：三个模型配置 + 图片模式打通

- **三个 Agent 独立模型配置**：`agents.creative / agents.planner / agents.executor` 各自拥有
  `model / base_url / api_key / temperature`，留空则回退 `global` 默认值（已补全，原仅有 global 一个）。
- 执行 Agent 调多模态时由 `providers.image / providers.video` 覆盖凭据。
- 前端配置页（React 工程版 + 免构建 demo.html）新增三个 Agent 分组输入框 + providers 分组 + mock 开关。
- **图片模式链路打通**：`_run_executor` 的 image 分支已接入真实 OpenAI 兼容 `images.generate`
  （支持 url / b64_json 两种返回；b64 落盘 D 盘 output/）。mock 下返回明确占位文案引导填真实 key。
- 新增 `/output` 静态路由（serve.py）与前端 img 预览，真实生成的图片可在浏览器直接查看。
- 修复：配置保存热更新未同步 config 模块单例（pipeline 读取源）——新增 `config.set_settings` 修复。
- 当前 mock=true 时图片模式展示占位提示；用户填好 `providers.image` 真实 base_url/api_key/model 并关闭 mock 后，图片即真实生成并可预览。

## 18. 本轮更新（2026-07-27 晚）：填 base_url/api_key 后自动获取模型列表

- **新增后端接口 `POST /api/models`**：接收 `base_url` + `api_key`，调用 OpenAI 兼容 `/models` 返回模型 id 列表。
  - 优先用 `openai` 库的 `client.models.list()`（兼容更稳）。
  - 失败回退到原生 `GET {base_url}/models`（带 Bearer 头、15s 超时）。
  - 缺参返回 400，连接/鉴权错误返回友好 502（已 curl 验证错误路径）。
- **前端配置页（React 工程版 `ConfigPanel.jsx` + 免构建 `demo.html`）全面改造**：
  - 每个模型配置组（全局默认 / Agent1 / Agent2 / Agent3 / 图片 / 视频）改为「base_url + api_key + 模型输入框 + 获取模型列表按钮」。
  - 模型字段用 `<input list=datalist>`：填好 base_url/key 后点「获取模型列表」拉取下拉，用户可手选也可手填。
  - 字段名映射：全局默认用 `default_model/default_base_url/default_api_key`，其余组用 `model/base_url/api_key`（已做 keyMap 兼容）。
- 启动约束：本机沙箱对 localhost 代理有网络限制，前端演示服务器 `serve.py` 需以**非沙箱**方式启动（`dangerouslyDisableSandbox`）才能正常代理 `/api` 到后端；用户在本机正常终端启动不受影响。

---

## 19. 本轮更新（2026-08-08）：代码模式「环境体积约束」体系 + 独立工具池 + 主题去重

### 19.1 代码模式定位（与用户确认）
- 代码模式**只生成 spec.md**（可直接交给编程 Agent 如 Claude Code/OpenCode 迭代开发），不直接产出代码。
- 创意可宏大，但 spec **不得引导安装重型环境**（引擎/美术工具链/数据库/CUDA/Docker 等），避免多抽几个盲盒硬盘告急。
- 愿景：用户没想法时开盲盒拿灵感 + 可落地的轻量蓝图。

### 19.2 技术栈环境黑名单（`modes[code].blocked_env` + `pipeline.py`）
- 默认 17 项：unreal/ue5/unity/godot/blender/substance/fmod/wwise/mysql/sql server/postgres/postgresql/oracle/cuda/docker/android studio/xcode。
- 流程：`run_blind_box` 对 code-spec/code-cli 在落盘前扫描 spec 文本 → 命中则**带警告重试 planner 一次**（prompt 要求"保留创意、改用轻量载体"）→ 重试仍命中则在 spec 顶部插入**环境体积警告头**，并透传 `result.env_warning` 到前端展示红色警告。
- 误报防护：扫描前先剔除 spec 中复述【技术栈环境约束】的章节（`_strip_env_constraint_section`），"模型复述禁令清单"不再误伤；真实选型集中在【技术栈】等章节，不受影响。
- 前端：揭示页/历史页 spec 展示区新增**环境成本卡**（从 spec 提取【环境依赖清单】【磁盘占用预估】）+ 警告条。

### 19.3 code 模式独立工具池（`modes[code].pool`）
- 与全局 `randomness.pool` 互不影响；`draw_seed(mode)` 优先取 mode 级池。
- 与全局池能力一致：前端配置页可**手动增删、🎲 生成单条（/api/generate-seed 带 mode_id）、一键整体刷新（/api/seed/refresh-all 带 mode_id）**。
- 池内容偏"实用工具"方向：theme=本地文件处理/效率笔记/数据转换/个人记账/时间管理/创作辅助/图片处理/文本工具；constraint=本地持久化/一键导出/离线可用/快捷键/批量/中文；twist=拖拽导入/彩蛋/撤销/模板/主题切换。

### 19.4 代码模式 planner 模板（最终章节）
技术栈环境约束（禁重型环境、推荐 Web 单文件＞Python 少量 pip＞Node 少量 npm、依赖 ≤5、磁盘 ≤1GB）→ 项目名称 → 创意概念 → 技术栈 → 目录结构（**单文件或 ≤10 文件小工程**）→ **目标用户与用途** → **功能闭环**（数据如何保存/导出/复用）→ 模块拆解 → **图片素材需求**（无法程序化绘制的元素才列图片，注明规格 PNG 透明底；否则"全部 Canvas 程序化"）→ 环境依赖清单 → 磁盘占用预估 → MVP 定义 → 验收标准（只留核心流程）。

### 19.5 种子主题近期去重（全局生效）
- `draw_seed` 的 theme 维度排除最近 3 次用过的主题（`output/recent_themes.json` 持久化，D 盘），避免"总抽到潮汐碑林"式扎堆；其余维度仍纯随机。

### 19.6 前端配置页下拉修复
- 模型/音色选择从 `<input list=datalist>` 改为**有列表时用 `<select>`**（可随时改选）+ 值不在列表时显示「自定义」手填框，修复"选中后无法再改选"。

### 19.7 仓库卫生
- `.gitignore` 新增 `/_*`、`/backend/_*`、`/backend/tests/_qa_*` 临时文件规则；已移除 56 个调试产物跟踪（本地文件保留）。`backend/settings.yaml` 含真实 key，确认从未入库（含 git 历史）。

---

*本文档基于 2026-07-27 的方案讨论整理，作为 AI 盲盒 v1 的设计基线；后续实现以本文件字段规范为准，最新实现状态见第 19 节。*
