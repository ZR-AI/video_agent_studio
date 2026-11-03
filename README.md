# 视频 Agent 工作台 · 第一版

Python + LangGraph + FastAPI 的本地视频 agent。通过中文对话选择商品广告、人物口播或动作迁移，补充素材，生成计划；用户确认后执行付费生成。支持历史会话、后台任务、参考帧预览确认、进度查询、暂停及恢复、成片下载。

## 启动

首次从 GitHub 安装（Python 3.10+，本机需要 `ffmpeg` / `ffprobe`）：

```bash
git clone https://github.com/ZR-AI/video_agent_studio.git
cd video_agent_studio
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.example .env
# 在 .env 中填写 DEEPSEEK_API_KEY 和 RUNNINGHUB_API_KEY
./start.sh
```

请保留克隆目录名 `video_agent_studio`；它也是 Python 包名。口播所需纯函数和默认提示词已随仓库打包，单独克隆不需要旧 `fl2va` 源码；若旁边已有旧项目，会继续优先使用其提示词和非敏感工作流配置。商品及动作工具通过 HTTP 调用现有服务，需要在 `.env` 中配置可连接的 `PRODUCT_STUDIO_URL` 和 `MOTION_STUDIO_URL`。本仓库不包含这两个服务的源码、模型权重或任何运行数据。

已有本机工作区已安装独立的 `.venv`，可直接启动：

```bash
cd /home/linjing/disk_ssd/zr_work/Wan2.2-Animate-2
./video_agent_studio/start.sh
```

浏览器打开 <http://127.0.0.1:8010>。浏览器在其他电脑时，转发服务器 8010 端口：

```bash
ssh -N -L 8010:127.0.0.1:8010 用户名@服务器地址
```

服务默认仅监听本机，面向单用户。每个数据目录只运行一个进程/worker，进程锁会拒绝第二个实例。不要用 `uvicorn --workers` 或 `--reload` 执行生产任务。

新环境安装示例（Python 3.10+）：

```bash
python3 -m venv video_agent_studio/.venv
video_agent_studio/.venv/bin/python -m pip install -r video_agent_studio/requirements.lock
```

若系统 Python 缺少 ensurepip，可使用现有 Conda Python 创建 venv，或使用 uv：

```bash
uv venv --python /home/linjing/disk_ssd/zr_work/env/wananimate2_web/bin/python video_agent_studio/.venv
uv pip install --python video_agent_studio/.venv/bin/python -r video_agent_studio/requirements.lock
```

需要本机 `ffmpeg` / `ffprobe`。旧商品项目和动作项目继续作为独立服务运行：

```bash
./product_video_studio/start.sh
/home/linjing/disk_ssd/zr_work/env/wananimate2_web/bin/python wananimate2_runninghub_web/app.py
```

默认连接 8008 / 8001，工作台会显示服务是否可连接。口播直接在新后台执行，无需打开旧 fl2va 网页或启动 Cloudflare 开发服务器。

## 配置与凭证

默认读取 `../fl2va/web/.dev.vars` 中的 DeepSeek / RunningHub 配置；不复制、修改旧文件。优先级：进程环境变量 → 本项目 `.env` → KEY_SOURCE_FILE。可参考 `.env.example`。

单独克隆没有这个共享文件时，直接在本项目 `.env` 设置两个 API Key 即可。`.env`、`.venv/`、任务数据库、上传素材、成片和日志均不提交到 GitHub；`.env.example` 只有变量名及非敏感默认配置。

`VIDEO_AGENT_MODEL` 默认 `deepseek-chat`，负责文字需求与工具选择；原商品工具保留其视觉策划模型。主 agent 只接收素材角色和元数据，商品视觉理解由商品工具执行。

没有 DeepSeek Key，或 `VIDEO_AGENT_PLANNER=rules` 时，明确显示“本地规则规划”，支持离线检查与基础任务计划；真正的口播分段/商品策划仍需相应 API。DeepSeek 失败会报错，不会静默声称规划成功或自动生成。

`FL2VA_URL` 留空时直连 DeepSeek / RunningHub。默认从旧 `wrangler.toml` 读取主视频及 SeedVC 工作流 ID。`FL2VA_VIDEO_MODE` 必须匹配 `RUNNINGHUB_VIDEO_WORKFLOW_ID` 对应版本。第一版每个部署使用一个口播生成版本，不允许任务混用不同版本的云端 ID。

密钥只在执行器内存中使用，不写入对话、计划、检查点、任务记录、前端存储。外部 LangSmith tracing 默认禁用。POST 需要服务每次启动生成的 X-App-Token，浏览器会自动获取；这用于本地请求校验，不是多用户登录系统。

## 使用

1. 新建会话，上传素材并选择角色：商品图、人物参考图、驱动视频、尾帧或音色参考。
2. 发送需求。例如“用商品图制作10秒竖屏生活广告”；口播请写“口播文案：……完整原文……”；动作任务上传人物图和驱动视频。
3. 检查计划、素材缺项和费用提示。上传新素材后再发一句“使用新素材继续”，生成新计划。
4. 点击确认生成。任务保存到服务器，关闭网页不停止后台处理。
5. 含商品的口播先生成持商品参考图，停在预览确认，再继续生成视频。

支持的任务边界：

| 工具 | 第一版范围 |
|---|---|
| 商品广告 | 原广告/生活片、多视角特写，沿用原 H3 导演台、策划校验和恢复 |
| 人物口播 | 文案分段、原始人物参考帧、可选 Qwen 持商品合成、五种比例、串行生成、本地直接拼接、可选 SeedVC 换音色 |
| 动作迁移 | Wan 或 H3 action，独立/连续模式按原服务执行，可选原有抽帧质检与有限自动修复 |

口播第一版不包含旧网页的背景取色还原、paired 双分支、尾音加速及自定义转场；可在旧网页准备参考图后上传到新工作台。动作输出尺寸/比例由原工作流和驱动视频决定。跨三个工具的混合剪辑属于后续版本；一个计划选择一个工具。

## 恢复与付费边界

LangGraph 流程为 `understand → validate → review(interrupt) → enqueue`。计划及确认状态保存到 SQLite 检查点；确认后只创建本地作业，不直接在图节点内提交云任务。同一计划及 request_id 的重复确认均返回原作业。实际后台执行使用独立 SQLite 台账。

商品/动作 adapter 保存原服务的本地 job ID，恢复查询该任务。口播按 `composite / segment:1 / voice` 等步骤记录提交意图，取得云端 taskId 后立即落盘，下载和拼接失败可复用已有结果。

提交响应丢失或提交中服务崩溃时禁止自动重交。商品/动作创建响应未知时，应核对原工作台并补录其**本地任务 ID**；口播则补录对应步骤的 **RunningHub taskId**。原商品任务内部的云端提交未知，仍需去原工作台绑定云端 ID 后再回来继续。

暂停停止本地处理/轮询，不保证取消已接受的云任务，也不会退还已发生费用。服务重启后运行中任务标为“待继续”；未知提交保持核对要求。云端明确失败的口播段不会自动重新收费；需要新候选时新建计划。任务配置和输入绑定有指纹，不能混用历史分段。

“生成完成”和“质检通过”分开记录。达到修复上限仍有缺陷时显示需复核，保留成片。现有动作质检不能直接套用于带货/多镜头广告，第一版不跨项目复用此规则，也不承诺音频、口型或每个视频帧均已检查。

费用以供应商为准，工作台不伪造金额估计。确认多视角计划会先发生图片生成费用；动作自动质检与重跑另有消耗。

## 离线验证

```bash
cd .. # 从克隆仓库根目录回到其父目录，以包名运行测试
video_agent_studio/.venv/bin/python -m pytest video_agent_studio/tests -q
node --check video_agent_studio/static/app.js
```

测试使用模拟供应商，覆盖真实 LangGraph 计划确认、恢复和执行台账，以及三个 adapter 的提交防重与接口契约。不调用付费生成 API。真实云端生成的画质、云端插件版本和不同素材效果需通过样片验证。

## 文件与 API

`graph.py` 负责 agent 状态图；`planner.py` 负责 DeepSeek 规划和参数校验；`engine.py` 负责后台执行；`store.py` 是事务任务台账；`adapters/` 保留三个项目的执行边界；`static/` 为无构建前端。

本地数据位于 `data/`：`studio.sqlite3`、`checkpoints.sqlite3`、`assets/`、`jobs/<id>/`，均被 Git 忽略。不要删除仍在运行或等待恢复的任务目录。

主要 API：`GET /api/settings`、`POST /api/sessions`、`POST /api/assets`、`POST /api/sessions/{id}/messages`、`POST /api/plans/{id}/execute`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/pause`、`POST /api/jobs/{id}/resume`、`GET /api/jobs/{id}/artifacts/{index}`。详细输入见 FastAPI 的 `/docs`。
