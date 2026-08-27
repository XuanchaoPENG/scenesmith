# SceneSmith 场景生成、扩展功能与结果展示指南

本文档针对当前仓库 `/home/dex/桌面/scenesmith` 的实际配置。当前采用的是基础远程模式：LLM、图像生成、图像分割和 3D 几何生成均通过 API 调用，不在本机安装或运行 SAM3、SAM3D 等模型。

## 1. 当前已经能做什么

当前链路已经实际生成过一个完整的桌面场景，工作流如下：

```text
文本提示词（可选：顶层参考图片）
  → VLM 将参考图解析为对象、朝向、支撑和空间关系约束
  → LLM 设计房间和平面布局
  → 家具/墙饰/顶饰/桌面小物规划
  → 远程图像生成 API
  → 远程图像分割 API
  → 远程几何生成 API
  → 本地 GLB/SDF、碰撞体和物理属性处理
  → 物体摆放、堆叠、碰撞投影和物理沉降
  → Blender、Drake DMD、JSON 和 SceneEval 输出
```

已经使用并验证的能力包括：

- 单房间平面、墙、门窗和默认材质生成。
- 桌椅等大型家具生成和摆放。
- 墙面装饰与天花板灯具生成。
- 盘子、碗、书和瓶子等桌面小物生成。
- 参考图的结构化解析、文本约束注入，以及 Critic 多视图对比。
- 支撑面识别、排列、堆叠和碰撞检查。
- 最终物理投影及约 15 秒的物理沉降。
- Blender、Drake Directives、状态 JSON 和 SceneEval 导出。

## 2. 第一次运行前的准备

### 2.1 激活环境

```bash
cd /home/dex/桌面/scenesmith
conda activate scenesmith
```

快速确认关键依赖：

```bash
python -c "import bpy, pydrake; print('SceneSmith environment OK')"
```

如果以后需要重建基础环境，可使用 Python 3.11 创建环境，然后在仓库根目录执行 `pip install -e .`。当前环境已经配置完成，不需要重复安装。

### 2.2 配置 `.env`

仓库根目录的 `main.py` 会自动读取同目录下的 `.env`，不需要手动 `source` 才能运行 SceneSmith。

首次配置时：

```bash
cp .env.example .env
chmod 600 .env
```

需要填写的主要内容是：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`：兼容 OpenAI Responses API 的 LLM 服务。
- `SCENE_ENGINE_IMAGE_GENERATION_BASE_URL`：文本生成图片服务，当前对应 zimage。
- `SCENE_ENGINE_IMAGE_SEGMENTATION_BASE_URL`：图像分割服务。
- `SCENE_ENGINE_GEOMETRY_GENERATION_BASE_URL`：图片/掩码生成 3D 几何服务。

基础远程模式应保留这些设置：

```dotenv
SCENESMITH_GEOMETRY_MODE="remote"
SCENESMITH_GEOMETRY_BACKEND="remote"
SCENESMITH_IMAGE_GENERATION_BACKEND="scene_engine"
SCENESMITH_ENABLE_ARTICULATED="false"
SCENESMITH_ENABLE_THIN_COVERING="false"
SCENESMITH_USE_MATERIALS_RETRIEVAL="false"
SCENESMITH_DISABLE_BWRAP="true"
SCENESMITH_OPENAI_STRUCTURED_OUTPUT_MODE="json_object"
SCENESMITH_OPENAI_API_MODE="chat_completions"
OPENAI_AGENTS_DISABLE_TRACING="true"
```

不要把真实密钥写进本文档、命令历史、日志或 Git。`.env` 已被 Git 忽略，但仍应在提交前执行 `git status` 检查。

### 2.3 检查远程服务

需要手动检查服务时，可以临时导出 `.env` 变量：

```bash
set -a
source .env
set +a

curl -fsS "${SCENE_ENGINE_IMAGE_GENERATION_BASE_URL}${SCENE_ENGINE_IMAGE_GENERATION_HEALTH_PATH}"
curl -fsS "${SCENE_ENGINE_IMAGE_SEGMENTATION_BASE_URL}${SCENE_ENGINE_IMAGE_SEGMENTATION_HEALTH_PATH}"
curl -fsS "${SCENE_ENGINE_GEOMETRY_GENERATION_BASE_URL}${SCENE_ENGINE_GEOMETRY_GENERATION_HEALTH_PATH}"
```

三个命令都应正常返回。如果几何服务较慢，只要健康检查正常，不应因为单次生成耗时较长而立即中止。

## 3. 开始生成场景

### 3.1 最小命令

当前 LLM 服务使用 Chat Completions 的原生工具调用和 `json_object` 结构化输出。Agent 的 planner、designer 和 critic 共用同一组 `OPENAI_*` 配置，执行顺序保持原仓库逻辑，基础命令不覆盖 `max_critique_rounds`：

```bash
python main.py \
  +name=my_first_scene \
  floor_plan_agent.mode=room \
  'experiment.prompts=["A simple dining room with one rectangular wooden table, four chairs, ordinary walls, and a few small objects on the table."]' \
  experiment.num_workers=1
```

不要把 `max_critique_rounds=0` 当成“跳过 planner/critic”的开关；本仓库现在保留原始执行语义，即 planner 和最终评价流程仍由原 agent 工作流决定。

`+name` 只是运行名称；实际输出仍按日期和时间写入 `outputs/YYYY-MM-DD/HH-MM-SS/`。最新一次运行也可通过 `outputs/latest-run` 访问。

### 3.2 只生成桌面场景

SceneSmith 仍然需要一个房间作为容器，因此“桌面场景”推荐理解成：生成一个简单单房间，只放一张主桌，并把重点物体放到这张桌上。

提示词应明确包含：

1. 房间类型和主桌数量。
2. 桌面物体的精确清单。
3. 从下到上的支撑顺序。
4. 朝向、颜色、尺寸关系和接触关系。
5. 不允许出现的额外物体。

示例：

```bash
python main.py \
  +name=desktop_stack \
  floor_plan_agent.mode=room \
  'experiment.prompts=["A simple tabletop still-life scene in a modest dining room. Include exactly one plain rectangular dining table as the main furniture. At the center of the tabletop, create one vertical stack from bottom to top: a large flat round white dinner plate; a smooth white ceramic bowl placed upside down with its wide rim touching the plate and its rounded base facing upward; a thick closed tan-brown hardcover book lying flat on the inverted bowl; and a classic clear cola bottle standing upright on the book, filled with dark cola and closed by a red metal cap. Every layer must physically touch the layer below. Keep the stack centered and stable. Do not add any other objects on this table."]' \
  experiment.num_workers=1
```

提示词是语义约束，不是严格的机械约束。生成后必须检查朝向和接触关系。例如，“碗倒扣”虽然写进提示词，资产仍可能以默认正向姿态被堆叠；此类错误需要重新生成资产、显式修改姿态，或者给堆叠工具增加方向约束，不能只根据对象名称判断正确。

### 3.3 使用顶层参考图片

可以在启动时传入一张参考图。系统首先用当前多模态 LLM 提取结构化约束，再把它们追加到原场景提示词；每轮 Critic 还会把该图作为目标，与 `observe_scene` 生成的当前场景多视图对比。

使用当前桌面参考图：

```bash
python main.py \
  +name=reference_desktop \
  floor_plan_agent.mode=room \
  'experiment.reference_image_path=/home/dex/下载/ChatGPT Image 2026年8月21日 17_37_17.png' \
  'experiment.prompts=["Reconstruct a simple, physically stable tabletop scene matching the reference image. Use one plain table in a modest room and do not add unrelated tabletop objects."]' \
  experiment.num_workers=1
```

这不是从单张 RGB 图直接恢复精确几何，仍然使用 SceneSmith 原有的“文本规划 → 资产生成 → 摆放 → 物理处理”管线。图片额外提供语义、外观、朝向、计数、支撑和空间关系约束；Critic 做的是语义和结构对比，而非逐像素匹配。

一次运行传入多个 prompt 时，当前的同一张参考图会应用于每个 prompt。若每个场景需要不同图片，应分开启动多次实验。只想用图片强化生成提示词、不想在 Critic 中重复传图时，增加：

```bash
experiment.reference_image.compare_during_critique=false
```

### 3.4 单房间与多房间

单房间：

```bash
python main.py +name=one_room floor_plan_agent.mode=room \
  'experiment.prompts=["A compact home office with one desk and one chair."]'
```

多房间住宅：

```bash
python main.py +name=whole_house floor_plan_agent.mode=house \
  'experiment.prompts=["A small apartment with a living room, bedroom, kitchen, and bathroom."]'
```

多房间会生成更多资产，API 调用数量、耗时和失败概率都会明显增加。建议先用单房间验证提示词和服务，再切换到 `house`。

### 3.5 批量生成

可以直接传入多个提示词，也可以使用 CSV。CSV 格式如下：

```csv
scene_index,prompt
0,"A compact dining room with one table"
1,"A simple home office with one desk"
```

运行：

```bash
python main.py +name=batch_scenes \
  experiment.csv_path=/absolute/path/to/prompts.csv \
  experiment.num_workers=1
```

远程服务确认能够承受并发后，可以把 `experiment.num_workers` 调大。调试阶段建议保持为 `1`，避免多个 Blender 进程、端口和 API 请求交织在一起。

## 4. 生成阶段及断点续跑

流水线的真实顺序是：

| 阶段 | 配置值 | 主要产物 |
|---|---|---|
| 平面 | `floor_plan` | 房间、墙、地板、门窗 |
| 家具 | `furniture` | 桌椅、床、柜子等大型物体 |
| 墙面 | `wall_mounted` | 画、镜子、时钟、壁架等 |
| 顶面 | `ceiling_mounted` | 吊灯、吸顶灯、风扇等 |
| 小物 | `manipuland` | 桌面、柜面和容器中的小物体 |

只运行到家具阶段：

```bash
python main.py +name=base_room \
  experiment.pipeline.stop_stage=furniture \
  'experiment.prompts=["A simple dining room with one table and four chairs."]'
```

从已有实验分支，只重新生成小物体：

```bash
python main.py +name=new_tabletop_objects \
  experiment.pipeline.start_stage=manipuland \
  experiment.pipeline.resume_from_path=/home/dex/桌面/scenesmith/outputs/YYYY-MM-DD/HH-MM-SS
```

`resume_from_path` 会把所需检查点复制到新实验目录，不会覆盖原实验。这是修改桌面摆设时最值得使用的功能，因为不需要重新生成房间、桌椅、墙饰和灯具。

## 5. 输入和输出

### 5.1 输入

主要输入有四类：

- `.env`：模型/API 地址、密钥、超时、重试次数和功能开关。
- Hydra 参数：房间模式、流水线阶段、并发数和各 agent 配置。
- 场景提示词：直接通过 `experiment.prompts` 或 CSV 输入。
- 可选参考图：通过 `experiment.reference_image_path` 传入单张 PNG/JPEG/WebP 等 Pillow 可读图片。

参考图会被自动转写为结构化场景约束，不需要用户手工描述图片。为了指定房间范围、禁止项或图中不可见的需求，仍应同时提供一句简短文本 prompt。

### 5.2 输出目录

一次完整运行的核心结构如下：

```text
outputs/YYYY-MM-DD/HH-MM-SS/
├── experiment.log
├── resolved_config.yaml
├── reference_conditioning/       # 仅传入参考图时生成
│   ├── original.<ext>
│   ├── reference_image.png
│   ├── reference_scene_spec.json
│   └── reference_prompt.txt
└── scene_000/
    ├── package.xml
    ├── house_layout.json
    ├── combined_house/
    │   ├── house.blend
    │   ├── house.dmd.yaml
    │   ├── house_state.json
    │   └── sceneeval_state.json
    └── room_<room-name>/
        ├── generated_assets/
        ├── scene_renders/
        └── scene_states/final_scene/
            ├── scene.blend
            ├── scene.dmd.yaml
            ├── scene_state.json
            └── sceneeval_state.json
```

各文件用途：

| 文件 | 用途 |
|---|---|
| `house.blend` | 最适合在 Blender 中检查外观和制作展示渲染 |
| `house.dmd.yaml` | SceneSmith 原生 Drake Directives 仿真入口 |
| `house_state.json` | 对象语义、资产路径、组合关系和生成元数据 |
| `sceneeval_state.json` | SceneEval 兼容的对象与变换描述 |
| `resolved_config.yaml` | 本次运行的完整解析后配置，用于复现 |
| `experiment.log` | 排查 agent、API、资产生成和物理阶段错误 |
| `reference_scene_spec.json` | VLM 从参考图提取的对象、朝向、支撑和空间关系 |
| `reference_prompt.txt` | 实际追加到场景 prompt 的参考图约束，便于检查和复现 |

## 6. 如何查看和展示最终结果

### 6.1 Blender 外观展示

最终完整场景位于：

```text
outputs/YYYY-MM-DD/HH-MM-SS/scene_000/combined_house/house.blend
```

桌面有 Blender 文件关联时可运行：

```bash
xdg-open outputs/YYYY-MM-DD/HH-MM-SS/scene_000/combined_house/house.blend
```

也可以在 Blender 中使用 `File → Open` 打开。推荐的展示步骤：

1. 切换到 Material Preview 或 Rendered 模式检查材质。
2. 添加或调整 Camera，使目标物体位于画面中心。
3. 使用 Area Light 补光，避免白色物体和浅色背景融在一起。
4. 在 Eevee 中设置 512 或 1024 分辨率，按 `F12` 渲染。
5. 使用 `Image → Save As` 保存 PNG。

`scene_renders/` 中的图片主要是 agent 观察和调试图，通常包含标注、包围框或方向箭头，不一定适合作为最终展示图。

### 6.2 Drake/Meshcat 交互式仿真查看

完整场景中的 URI 使用 `package://scene/`，需要先设置包路径：

```bash
SCENE_DIR=/home/dex/桌面/scenesmith/outputs/YYYY-MM-DD/HH-MM-SS/scene_000
export ROS_PACKAGE_PATH="${SCENE_DIR}${ROS_PACKAGE_PATH:+:${ROS_PACKAGE_PATH}}"

python -m pydrake.visualization.model_visualizer \
  "${SCENE_DIR}/combined_house/house.dmd.yaml"
```

命令会启动 Meshcat，并在浏览器中显示场景。这里更适合检查：

- 物体坐标和层级。
- 碰撞几何与可视几何是否一致。
- 模型是否能被 Drake 正确解析。
- 可动关节是否存在以及活动方向是否正确。

如果只看某个房间，也可以加载其 `scene_states/final_scene/scene.dmd.yaml`。该文件使用绝对 `file://` 路径，一般不需要设置 `ROS_PACKAGE_PATH`。

### 6.3 保存物理仿真网页

当前 `experiment.projection.simulation.save_html=false`，因此没有保存 Meshcat HTML。需要调试或离线展示物理沉降时，可运行：

```bash
python main.py +name=scene_with_sim_html \
  experiment.projection.simulation.save_html=true \
  'experiment.prompts=["A simple room with one table and a cup on the table."]'
```

这类 HTML 更适合展示仿真过程；Blender PNG/视频更适合展示视觉质量。

### 6.4 当前桌面示例

已生成示例位于：

```text
/home/dex/桌面/scenesmith/outputs/2026-08-27/11-31-53
```

其中 `combined_house/house.blend` 和 `combined_house/house.dmd.yaml` 是流水线原始输出。由于原始堆叠把碗正放，后来额外生成了：

```text
room_dining_room/scene_states/final_scene/scene_bowl_inverted.blend
room_dining_room/scene_states/final_scene/preview_stack_bowl_inverted.png
combined_house/house_bowl_inverted.blend
```

这些 `*_bowl_inverted.blend` 是手动修正后的视觉场景；原始 DMD/JSON 尚未同步该手动修改，不能把修正版 Blender 和原始 DMD 当成完全一致的仿真状态。

## 7. 当前没有使用的功能，以及如何启用

| 功能 | 当前状态 | 作用 | 启用前提/方法 |
|---|---|---|---|
| 本地 SAM3/SAM3D | 未安装、未使用 | 本地分割和 image-to-3D | 准备模型仓库、权重和足够显存；将几何模式改为 `local`、backend 改为 `sam3d` |
| 本地 Hunyuan3D | 未使用 | 本地替代 3D 生成后端 | 安装对应依赖和权重，将几何模式改为 `local` 并选择 Hunyuan backend |
| AmbientCG 材质检索 | 关闭 | 为墙、地面和覆盖物检索 PBR 材质 | 下载材质、准备 embedding，再设置 `SCENESMITH_USE_MATERIALS_RETRIEVAL=true` |
| 可动关节物体 | 关闭 | 带门、抽屉或关节的柜子等 | 准备 ArtVIP 或 PartNet-Mobility SDF 与 embedding，再设置 `SCENESMITH_ENABLE_ARTICULATED=true` |
| 薄覆盖物 | 关闭 | 地毯、桌布、餐垫等薄片资产 | 需要材质库或可用的材质生成后端，再设置 `SCENESMITH_ENABLE_THIN_COVERING=true` |
| HSSD 检索 | 未使用 | 从室内资产库检索现有模型，减少 3D 生成 | 下载 HSSD 和预处理数据，把相应 agent 的 `asset_manager.general_asset_source` 设为 `hssd` |
| Objaverse/ObjectThor 检索 | 未使用 | 从 Objaverse 子集检索现有模型 | 下载并预处理数据，把相应 agent 的 `asset_manager.general_asset_source` 设为 `objaverse` |
| 多房间 house | 未使用 | 一次生成多个连通房间 | 设置 `floor_plan_agent.mode=house`；建议先保持单 worker |
| 多场景/多房间并行 | 未使用 | 提高批量生成吞吐 | 使用 CSV、调高 `experiment.num_workers`，或启用 `experiment.pipeline.parallel_rooms=true`；需要确认 API 和本机端口容量 |
| Robot Evaluation | 未使用 | 从操作任务生成场景、规划机器人动作并验证成功条件 | 使用 `scripts/robot_eval/`；需要机器人模型、策略或配套 planner 环境 |
| MuJoCo/USD 导出 | 未使用 | 将 DMD/SDF 场景迁移到其他仿真器 | 运行 `scripts/setup_mujoco_export.sh` 创建独立环境，再用 `scripts/export_scene_to_mujoco.py` |
| EmbodiChain 直接运行 | 尚未接入 | 用 EmbodiChain 作为后续仿真/任务执行层 | 需要在 EmbodiChain 中实现或接通 DMD/SDFormat loader，并注册 `scene` package 路径；当前 `pip install -e .` 只表示包可导入，不等于已经替换 SceneSmith 的仿真引擎 |

### 7.1 启用材质检索

```bash
python scripts/download_ambientcg.py --output data/materials
python scripts/compute_ambientcg_embeddings.py --materials-dir data/materials
```

然后修改 `.env`：

```dotenv
SCENESMITH_USE_MATERIALS_RETRIEVAL="true"
```

材质检索服务器会增加本机资源占用。在基础 API 模式下，它不是场景生成的必要条件。

### 7.2 启用 HSSD 或 Objaverse

准备好 README 所述数据后，可通过 Hydra 覆盖选择资产来源。例如只让家具使用 HSSD：

```bash
python main.py +name=hssd_furniture \
  furniture_agent.asset_manager.general_asset_source=hssd \
  manipuland_agent.asset_manager.general_asset_source=generated \
  'experiment.prompts=["A simple dining room with a table and chairs."]'
```

只让小物体使用 Objaverse：

```bash
python main.py +name=objaverse_small_objects \
  manipuland_agent.asset_manager.general_asset_source=objaverse \
  'experiment.prompts=["A simple kitchen with utensils on the counter."]'
```

这两个功能依赖本地资产和 CLIP 检索数据，不能只修改一个开关就直接运行。

### 7.3 启用可动关节物体

当前 `.env` 中的 `ARTICULATION_SERVER_BASE_URL` 只是预留变量，尚未自动替代 SceneSmith 的 ArtVIP/PartNet 检索链路。若使用仓库原生功能，应先按照 README 准备 ArtVIP 或 PartNet-Mobility 数据、SDF 和 embedding，然后设置：

```dotenv
SCENESMITH_ENABLE_ARTICULATED="true"
```

如果想直接调用云端 Articraft API，则还需要新增与现有远程几何 client 类似的 articulated adapter，仅填写 URL 不足以启用。

### 7.4 Planner/Critic 的结构化输出模式

当前服务的 Responses 接口会把工具调用作为普通 JSON 文本返回，而 Chat Completions 接口能够返回原生 `tool_calls`。因此当前方案使用同一个 API 和模型，只把 Agents SDK transport 配置为：

```dotenv
SCENESMITH_OPENAI_API_MODE="chat_completions"
SCENESMITH_OPENAI_STRUCTURED_OUTPUT_MODE="json_object"
```

这没有修改或绕过原仓库的 planner/designer/critic 执行流程。若以后更换为完整支持 Responses API、原生工具调用和 JSON Schema 的服务，可以设置：

```dotenv
SCENESMITH_OPENAI_STRUCTURED_OUTPUT_MODE="json_schema"
SCENESMITH_OPENAI_API_MODE="responses"
```

各 agent 的 critique 轮数继续使用配置文件默认值。更高的轮数通常会增加 API 调用、耗时和费用。

### 7.5 MuJoCo 和 USD

导出依赖与当前 `bpy` 环境存在 OpenUSD 冲突，因此必须使用单独环境：

```bash
./scripts/setup_mujoco_export.sh
source .mujoco_venv/bin/activate

python scripts/export_scene_to_mujoco.py \
  /home/dex/桌面/scenesmith/outputs/YYYY-MM-DD/HH-MM-SS/scene_000 \
  -o mujoco_export

python -m mujoco.viewer --mjcf=mujoco_export/scene.xml
```

需要 USD 时增加 `--usd`。该导出链路目前属于实验功能，应重新检查材质、关节和碰撞几何。

## 8. EmbodiChain 的衔接位置

SceneSmith 负责“生成场景”，EmbodiChain 更适合放在“加载场景、加入机器人、执行任务和记录结果”的后半段。推荐边界是：

```text
SceneSmith
  └─ scene_000/combined_house/house.dmd.yaml + SDF/GLB/纹理
       ↓ DMD/SDFormat loader
EmbodiChain
  └─ 仿真世界、机器人、控制策略、任务执行和评测
```

DMD/SDFormat loader 至少需要完成：

1. 解析 `house.dmd.yaml` 中的 `add_model`、`add_weld` 和自由体姿态。
2. 将 `package://scene/...` 映射到对应 `scene_000` 目录。
3. 加载每个 SDF 中的视觉网格、碰撞网格、惯量和关节。
4. 保持 SceneSmith 的米制坐标、Z-up 方向和对象命名。
5. 在手动编辑 Blender 后同步变换；Blender 文件不能代替 DMD 仿真状态。

在该 loader 完成前，建议使用 Drake Model Visualizer 验证原生 DMD，用 Blender 展示外观，不要直接宣称 SceneSmith 已经换成 EmbodiChain 仿真引擎。

## 9. 常见问题与检查顺序

### API 调用失败

1. 检查三个 `/health`。
2. 查看 `experiment.log` 中的 HTTP 状态码和重试信息。
3. 检查 `.env` 的 endpoint path 是否与服务端一致。
4. 几何生成可耗时数分钟，不要把 timeout 设得过短。

### LLM 重复调用但没有进展

确认正在使用：

```dotenv
SCENESMITH_OPENAI_STRUCTURED_OUTPUT_MODE="json_object"
```

如果 provider 仍不能完成 planner 或 critic 的结构化响应，应修复 provider 兼容性或更换服务，不应依赖 `max_critique_rounds=0` 绕过原 agent 工作流。

### 物体朝向或支撑错误

- 查看最终 Blender，而不是只看日志中的对象名称。
- 查看 DMD 中的旋转与相对 weld 变换。
- 对“倒扣、侧放、打开、贴墙”等要求做显式姿态验证。
- 手动改 Blender 只改变视觉结果；要进入仿真，还必须同步 DMD/JSON 和碰撞状态。

### 输出占用过大

确认最终结果不再需要中间调试文件后，可先查看清理脚本帮助：

```bash
python scripts/clean_scene_output.py --help
```

清理前应备份 `combined_house/`、`resolved_config.yaml` 和 `experiment.log`。不要在仍需断点续跑时删除中间状态。

## 10. 推荐的日常流程

1. 激活 `scenesmith` 环境并检查三个远程服务。
2. 先用 `room`、单 worker 和简短提示词做小规模验证。
3. 提示词明确列出对象、顺序、支撑、朝向和禁止项。
4. 记录终端打印的输出目录，或使用 `outputs/latest-run`。
5. 先看最终 Blender 外观，再用 Drake/Meshcat 检查 DMD 和碰撞结构。
6. 如果只修改桌面小物，使用 `resume_from_path` 从 `manipuland` 阶段分支。
7. 不要把手工修改后的 Blender 与未同步的 DMD 混作同一个仿真结果。
8. 保留默认 Critic 流程；确认基础链路稳定后，再逐项开启材质、关节资产、并行或其他仿真器导出。
