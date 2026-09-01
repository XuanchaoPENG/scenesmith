# SceneSmith Desktop 场景生成指南

本文档以当前仓库中的 `desktop_scene_generation` 实现为准。Desktop 模式用于生成“单张桌子 + 主桌面上的小物体”，它是一条独立的两阶段流水线，不会先生成完整房间，也不会执行 floor plan、墙面、顶面或普通家具布局阶段。

如果目标是完整室内或多房间住宅，请继续使用默认的 `indoor_scene_generation`；本文主要说明 desktop 模式。

## 1. 当前工作流概览

```text
文本 prompt（可选：一张参考图）
  → 参考图 VLM 分析，生成结构化约束并追加到 prompt
  → 创建 4 m × 4 m 的无墙物理工作室和隐藏支撑地面
  → LLM 将 prompt 拆成桌子规格、必选/可选物体、风格和布局约束
  → 生成并在原点放置唯一一张桌子
  → 从桌子网格提取支撑面，只保留最高区域内最大的近水平主桌面
  → Planner / Designer / Critic 生成、摆放并多视图检查桌面小物
  → 单桌局部投影与物理仿真
  → 全场景碰撞投影与物理沉降，移除掉落小物
  → 硬性结构校验
  → 保存 GLB/SDF、状态 JSON、Drake DMD、Blender 和校验报告
```

### 1.1 与 indoor 模式的关键区别

| 项目 | Desktop | Indoor |
|---|---|---|
| 实验选择 | `experiment=desktop_scene_generation` | 默认 `experiment=indoor_scene_generation` |
| 场景范围 | 一张桌子及其桌面物体 | 单房间或多房间 |
| 主流程 | 桌子 → 桌面小物 | 平面 → 家具 → 墙面/顶面 → 小物 |
| 墙、门窗、椅子等 | 不生成 | 按 prompt 生成 |
| 可用支撑面 | 唯一主桌面 | 多家具、多支撑面 |
| 最终输出 | `scene_states/final_desktop_scene/` | 房间状态及 `combined_house/` |
| SceneEval 导出 | 默认关闭 | 默认开启 |
| 阶段续跑 | 当前未接入 | 支持检查点分支和续跑 |

无墙工作室仍有一个供 Drake 物理计算使用的地面，但地面属于 `room_geometry`，不计入 desktop 的场景对象。最终结构校验只允许一张目标桌和桌面小物。

## 2. 运行前准备

### 2.1 激活环境

```bash
cd /home/dex/桌面/scenesmith
conda activate scenesmith
python -c "import bpy, pydrake; print('SceneSmith environment OK')"
```

若需重建环境，请使用 Python 3.11，并在仓库根目录执行 `pip install -e .`。

### 2.2 配置 `.env`

`main.py` 会自动加载仓库根目录的 `.env`：

```bash
cp .env.example .env
chmod 600 .env
```

需要填写：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`：兼容 OpenAI API 的 LLM/VLM 服务。
- `SCENE_ENGINE_IMAGE_GENERATION_BASE_URL`：文本生成图片服务。
- `SCENE_ENGINE_IMAGE_SEGMENTATION_BASE_URL`：图片分割服务。
- `SCENE_ENGINE_GEOMETRY_GENERATION_BASE_URL`：图片/掩码生成 3D 几何服务。

当前基础远程配置为：

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

不要把真实密钥写进文档、命令历史、日志或 Git。提交前应检查 `git status`。

### 2.3 检查远程服务

```bash
set -a
source .env
set +a

curl -fsS "${SCENE_ENGINE_IMAGE_GENERATION_BASE_URL}${SCENE_ENGINE_IMAGE_GENERATION_HEALTH_PATH}"
curl -fsS "${SCENE_ENGINE_IMAGE_SEGMENTATION_BASE_URL}${SCENE_ENGINE_IMAGE_SEGMENTATION_HEALTH_PATH}"
curl -fsS "${SCENE_ENGINE_GEOMETRY_GENERATION_BASE_URL}${SCENE_ENGINE_GEOMETRY_GENERATION_HEALTH_PATH}"
```

几何生成可能耗时数分钟；健康检查正常时，不要因单次请求较慢而过早中止。

## 3. 生成 Desktop 场景

### 3.1 最小命令

必须显式选择 desktop 实验：

```bash
python main.py \
  +name=desktop_stack \
  experiment=desktop_scene_generation \
  'experiment.prompts=["Create a sparse tabletop still life: a white plate on the table, an upside-down white bowl on the plate, a closed brown book on the bowl, and one sealed glass cola bottle standing on the book. Preserve this exact bottom-to-top stack and add no other objects."]' \
  experiment.num_workers=1
```

`+name` 是运行名称；Hydra 仍会把结果写入 `outputs/YYYY-MM-DD/HH-MM-SS/`，并更新 `outputs/latest-run` 软链接。

Desktop 模式不需要设置 `floor_plan_agent.mode`。即使传入该参数，也不会执行平面生成。

### 3.2 Prompt 写法

建议明确给出：

1. 桌子的外观和尺寸需求；若不指定，则使用默认的浅色木质长方桌，默认尺寸约为 `1.4 × 0.8 × 0.75 m`。
2. 必选物体及精确数量。
3. 可选物体，或者明确写“不要添加其他物体”。
4. 支撑、堆叠、接触、朝向和从下到上的顺序。
5. 相对尺寸、颜色、材质、桌面位置和希望保留的空白区域。

例如：

```text
Use exactly one rectangular light-wood table. On its main tabletop include exactly
four items and no extras. At the center, stack from bottom to top: one large white
dinner plate; one white ceramic bowl upside down with its rim touching the plate;
one closed tan hardcover book lying flat; and one sealed clear cola bottle standing
upright. Keep every adjacent layer in contact and keep the rest of the table empty.
```

Desktop 会把 prompt 解析为桌子描述、桌子尺寸、必选/可选物体、风格和布局说明。解析失败会重试，全部失败后使用默认桌子并把原 prompt 作为必选物体要求。

默认 `atomic_asset_requests=true` 会关闭小物资产路由器的复合拆分，尽量保持“一次语义请求对应一个资产”。例如密封可乐瓶及瓶内可见液体应被视为一个资产，而不是瓶子和液体两个场景物体。堆叠工具仍可能把多个成员组合成一个复合场景对象，校验报告会通过 `num_members` 统计其真实成员数。

Prompt 和参考图提供的是语义约束，不保证精确几何重建。尤其应在最终 Blender 和多视图渲染中人工检查“倒扣、侧放、开口方向、层间接触”等姿态。

### 3.3 使用参考图

```bash
python main.py \
  +name=desktop_reference \
  experiment=desktop_scene_generation \
  'experiment.reference_image_path=/absolute/path/to/reference.png' \
  'experiment.prompts=["Reconstruct the tabletop objects and their support relationships. Use one plain table and add no unrelated objects."]' \
  experiment.num_workers=1
```

参考图只分析一次：VLM 会抽取对象、数量、外观、朝向、支撑与空间关系，保存结构化结果，并把文本约束追加到每个 prompt。Manipuland Critic 还会把原图作为视觉目标与当前多视图观察进行比较。

如只想把参考图转成 prompt 约束，不想在 Critic 中重复附图：

```bash
experiment.reference_image.compare_during_critique=false
```

同一次运行有多个 prompt 时，同一张参考图会作用于全部 prompt。不同场景需要不同参考图时，应分开运行。

### 3.4 CSV 与并行生成

CSV 必须包含 `scene_index,prompt`：

```csv
scene_index,prompt
0,"A writing desk with one laptop, one notebook, and one pen"
3,"A dining table with one plate, one cup, and no other objects"
```

```bash
python main.py \
  +name=desktop_batch \
  experiment=desktop_scene_generation \
  experiment.csv_path=/absolute/path/to/prompts.csv \
  experiment.num_workers=1
```

CSV 会覆盖 YAML 中的 `prompts`，目录编号沿用 `scene_index`，例如生成 `scene_000/` 和 `scene_003/`。确认 API、GPU、Blender 端口和内存能够承受后，可提高 `num_workers`；每个场景在隔离进程中执行，一项失败不会让其他正在执行的场景立即崩溃，但任务结束时会汇总失败并抛出错误。

## 4. 两阶段内部流程

### 4.1 阶段一：唯一桌子

1. 创建默认 `4 × 4 m`、无墙、带隐藏地面的 studio。
2. LLM 将场景 prompt 解析为 `DesktopSceneSpec`。
3. 资产链路执行图片生成、分割、3D 几何生成和本地 GLB/SDF/碰撞体处理。
4. 如果后端意外返回多个桌子候选，只保留占地面积最大的一个。
5. 将桌子固定在世界原点，并写入 desktop 标记及小物约束元数据。
6. 保存 `scene_states/desktop_after_table/` 状态快照。该快照目前不能直接用于续跑。

### 4.2 阶段二：主桌面小物

1. 只选择带 `desktop_target` 标记的桌子；数量不是 1 会报错。
2. 从桌子网格提取全部支撑面。
3. 过滤倾角大于 `5°` 的表面；Desktop 主桌面选择阶段不再额外设置最小面积限制。
4. 在最高表面向下 `0.05 m` 的候选带内选择面积最大的表面，并锁定为唯一主桌面。
5. 关闭上下文家具选择，不暴露地面、架子、抽屉或次级表面。
6. Planner 调度 Designer 生成/排列/堆叠物体；Critic 通过多视图和状态工具检查结果并给出迭代意见。
7. 对该桌的局部子问题执行碰撞投影和物理仿真；默认局部仿真为 5 秒。

### 4.3 最终物理处理与校验

当 `experiment.projection.enabled=true` 且 `experiment.projection.final.enabled=true` 时：

- 桌子被焊接固定，小物参与碰撞投影。
- 可按配置执行最终物理沉降；默认仿真 15 秒。
- 桌子不会因掉落检测被删除；掉到地面或明显下落的小物默认会被移除。
- 投影未收敛只记录警告，随后仍会执行结构校验。

`desktop_validation.json` 检查以下硬性条件：

- 恰好一个带 `desktop_target` 标记的桌子，且场景中只有这一件家具。
- `scene.objects` 中没有墙、墙饰、顶饰或地面对象。
- 已锁定唯一 `desktop_surface_id`。
- 至少存在一个桌面小物。
- 每个小物都有支撑面放置信息，且父表面就是锁定的主桌面。

校验失败时报告仍会先写盘，然后该场景以 `RuntimeError` 结束。当前校验不验证物体语义是否与 prompt 完全一致，也不验证精确朝向、数量、层间接触或美观程度；这些仍需查看渲染和状态文件。

## 5. 配置项

主要 desktop 配置位于 `configurations/experiment/desktop_scene_generation.yaml`：

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `desktop.studio.width_m` / `length_m` | `4.0` / `4.0` | 物理工作室尺寸 |
| `desktop.studio.floor_thickness_m` | `0.1` | 隐藏支撑地面厚度 |
| `desktop.table.default_dimensions_m` | `[1.4, 0.8, 0.75]` | prompt 未给尺寸时的桌子尺寸 |
| `desktop.prompt_analysis.max_retries` | `3` | prompt 结构化解析重试次数 |
| `desktop.target_surface.max_tilt_degrees` | `5.0` | 主桌面最大倾角 |
| `desktop.target_surface.top_height_tolerance_m` | `0.05` | 最高候选带厚度 |
| `desktop.objects.atomic_asset_requests` | `true` | 禁用小物复合拆分路由器 |
| `experiment.projection.*` | 见基础配置 | 最终投影和物理沉降 |
| `manipuland_agent.per_furniture_postprocessing.*` | 见小物配置 | 单桌局部投影和仿真 |

配置文件中 `desktop.target_surface.policy`、`allow_optional_objects`、`max_count`、`open_area_ratio`、`allow_floor_placement` 以及 `desktop.validation.*` 目前只是预留字段，尚未被 desktop 执行代码读取。不要把它们当作硬约束；精确数量应写进 prompt，并以最终报告和渲染为准。

同样，desktop 的 `pipeline.start_stage=desktop_table`、`stop_stage=desktop_objects`、`resume_from_path` 当前仅为共享启动代码保留，`_generate_single_scene` 每次都会完整运行桌子和小物两个阶段。当前不支持通过这些字段跳过阶段或从 desktop 检查点续跑。

`pipeline.parallel_rooms` 对 desktop 没有实际并行对象，应保持 `false`。`sceneeval_export.enabled` 虽然继承自共享配置且在 desktop YAML 中默认为 `false`，但当前 desktop 执行函数没有调用 SceneEval exporter；只把它改成 `true` 也不会产生 SceneEval 文件。

## 6. 输出目录

一次成功的 desktop 运行核心结构如下：

```text
outputs/YYYY-MM-DD/HH-MM-SS/
├── experiment.log
├── resolved_config.yaml
├── reference_conditioning/                 # 仅使用参考图时
│   ├── original.<ext>
│   ├── reference_image.png
│   ├── reference_scene_spec.json
│   └── reference_prompt.txt
└── scene_000/
    ├── action_log.json                        # 发生已记录场景操作时
    ├── scene.log
    ├── desktop_validation.json
    ├── studio_geometry/desktop_studio.sdf
    ├── generated_assets/
    │   ├── furniture/{images,geometry,sdf,debug}/
    │   └── manipuland/{images,geometry,sdf,debug}/
    ├── scene_renders/
    ├── scene_states/
    │   ├── desktop_after_table/
    │   │   ├── scene_state.json
    │   │   └── scene.dmd.yaml
    │   ├── desktop_pre_projection/          # 启用最终投影时
    │   │   ├── scene_state.json
    │   │   └── scene.dmd.yaml
    │   └── final_desktop_scene/
    │       ├── scene_state.json
    │       ├── scene.dmd.yaml
    │       ├── scene.blend                   # Blender 导出成功时
    │       └── bpy_visualization/            # Blender 导出时生成的检查图
    └── table_<id>_{designer,critic}*.db      # Agent 会话与摘要，文件集可能变化
```

最重要的文件：

| 文件 | 用途 |
|---|---|
| `desktop_validation.json` | 结构校验结果、错误、警告和对象计数 |
| `final_desktop_scene/scene.blend` | Blender 导出成功时的最终视觉检查和展示文件 |
| `final_desktop_scene/scene.dmd.yaml` | Drake 调试入口，使用绝对 `file://` 资产路径 |
| `final_desktop_scene/scene_state.json` | 对象语义、变换、组合关系、支撑面和资产路径 |
| `generated_assets/` | 生成图片、GLB、SDF、碰撞几何和调试图 |
| `scene_renders/` | Planner/Designer/Critic 的观察图和中间状态 |
| `resolved_config.yaml` | 本次运行的完整解析配置，用于复现 |
| `scene.log` / `experiment.log` | 分别排查单场景和实验级错误 |

Desktop 当前不会组装 `combined_house/house.blend`、`house.dmd.yaml` 或 `house_state.json`，也不会默认生成 SceneEval 文件。查看 desktop 结果时不要沿用 indoor 的 `combined_house/` 路径。

## 7. 查看结果

### 7.1 Blender

```bash
xdg-open outputs/latest-run/scene_000/scene_states/final_desktop_scene/scene.blend
```

建议检查：

- 桌上是否只有要求的物体，数量是否正确。
- 倒扣、直立、平放等朝向是否正确。
- 堆叠顺序、层间接触和相对尺寸是否合理。
- 物体是否因物理沉降发生明显滑移、穿透或掉落。

`scene_renders/` 和 `bpy_visualization/` 主要用于 agent 观察与调试，可能包含固定相机或标注，不一定适合直接作为最终展示图。

### 7.2 Drake / Meshcat

Desktop 的最终 DMD 使用绝对资产路径，一般无需设置 `ROS_PACKAGE_PATH`：

```bash
python -m pydrake.visualization.model_visualizer \
  outputs/latest-run/scene_000/scene_states/final_desktop_scene/scene.dmd.yaml
```

这里适合检查碰撞几何、对象变换和 Drake 是否能正确解析模型。

当前 desktop 只接通了单桌局部仿真的 HTML 保存：

```bash
python main.py \
  +name=desktop_sim_html \
  experiment=desktop_scene_generation \
  manipuland_agent.per_furniture_postprocessing.simulation.save_html=true
```

文件会写入 `scene_000/simulation/per_furniture/<table-id>_simulation.html`。`experiment.projection.simulation.save_html` 虽存在于共享配置中，但 desktop 的最终物理调用目前没有传入 HTML 输出路径，因此打开该字段不会保存最终仿真网页。

## 8. 常见问题

### 选成了完整房间模式

如果输出中出现 `house_layout.json`、`room_<name>/` 或 `combined_house/`，通常是漏写了：

```bash
experiment=desktop_scene_generation
```

### 找不到合格主桌面

日志出现 `No desktop support surface satisfies...` 时，通常是生成的桌面网格不够平、面积太小或被装饰结构抬高。优先重新生成更简单的平顶桌；必要时再调整 `desktop.target_surface.*` 阈值。

### 校验报告有效，但语义或姿态仍错误

结构校验只保证“一张桌、一个主桌面、所有小物都登记在该桌面”。它不会证明碗确实倒扣、瓶子确实密封、堆叠接触正确或对象清单完全匹配。必须查看 Blender、多视图渲染和 `scene_state.json`。

### 物体在物理阶段消失

默认局部和最终仿真都会移除明显掉落的小物。对比：

- `scene_states/desktop_pre_projection/`：最终投影前状态。
- `scene_states/final_desktop_scene/`：最终物理处理后状态。
- `scene.log` 中的 `Removed fallen desktop objects` 记录。

### 想只重做小物，不重新生成桌子

当前 desktop 尚未接入 `resume_from_path`，不能通过 `pipeline.start_stage` 实现。`desktop_after_table` 可用于人工调试，但还不是正式的续跑入口。

### API 调用失败

1. 检查三个健康端点。
2. 查看 `experiment.log` 和 `scene_000/scene.log` 中的 HTTP 状态码与重试记录。
3. 检查 `.env` 的 endpoint path 是否与服务端一致。
4. 几何生成超时应结合服务健康状况判断，不要把 timeout 设得过短。

### 输出占用过大

生成资产的碰撞网格和调试图可能较大。确认不再需要中间文件后，先查看清理脚本说明：

```bash
python scripts/clean_scene_output.py --help
```

清理前应保留最终 `scene_state.json`、DMD、Blender 及其引用的 SDF/网格/纹理；只复制 `.blend` 或 `.dmd.yaml` 不一定得到可迁移的完整场景。
