# AI 资产工作台（ai-asset-studio）

本地优先、零第三方依赖的 AI 美术资产管理系统，外加一条「小说 → 角色卡 → 多视角设定图 / 精灵动画切图」的游戏资产生产线。

后端是 Python 3.10+ 标准库（HTTP + SQLite + 自写 PNG 编解码），前端是原生 HTML/CSS/JS，服务只监听 `127.0.0.1`。不上传任何图片、提示词或密钥。

## 快速开始

```bash
git clone https://github.com/ax2/ai-asset-studio.git
cd ai-asset-studio
cp config.example.json config.json        # 按需改 asset_root / api_proxy
python server.py                           # http://127.0.0.1:8777
```

命令行入口：`python tools/generate.py --list-styles`、`--style retro-film-diary --count 10`（默认 Dry run，加 `--execute` 才真实调用 API）。

离线回归自检：`python tools/selftest.py`，覆盖 PNG 编解码、小说特征提取、锚点版本、四视图包与精灵包导出，全程不调用 API、不花钱，结束自动清理测试产物。

## 密钥与网络

- 在「连接设置」粘贴 API key，勾选「记住到本机」会用 Windows DPAPI 加密写入 `%APPDATA%\ai-asset-studio\credentials.bin`，与当前 Windows 账号绑定，重启自动生效。该文件在仓库之外，`.gitignore` 也不会收录。
- **明文写入 `config.json`**：连接设置页点「明文写入 config.json」，或直接编辑项目根目录的 `config.json` 加 `"api_key": "sk-..."`。该文件已被 `.gitignore` 排除，重启服务自动生效，不依赖界面粘贴。⚠️ 明文就是明文：任何能读取该目录的程序、备份/云同步工具都能看到；误提交到公开仓库必须立刻撤销并重建密钥。界面会实时显示 `.gitignore` 是否真的排除了它。**不要把密钥写进被提交的 `config.example.json`**，那只是空模板。
- 生效优先级（显式优先）：会话内存密钥 > 本机 DPAPI 加密密钥 > `config.json` 明文 > 环境变量 `OPENAI_API_KEY`。任何接口都不回显密钥本体，只返回脱敏片段；日志也不打印。
- 「验证连接」只请求只读的 `GET /v1/models`，不产生费用。
- 需要代理时在 `config.json` 里写 `"api_proxy": "http://127.0.0.1:1080"`（HTTP 代理，不是 `https://` 也不是 `socks5://`），或在连接设置页临时改，仅当前进程生效。
- `api_base` 可指向兼容网关。

## 资产库能力

| 能力 | 说明 |
| --- | --- |
| 风格配方 | JSON style bible：分块提示词、变量槽位、负面词、色板、评估规则、单次预算上限 |
| 提示词 | 查看 / 编辑 / 按分块重建 / 存版本 / 应用版本 / 一键复现 |
| 管理 | 搜索、筛选、评分、标签、收藏、精选 / 淘汰、备注、详情抽屉 |
| 生成 | 批量生成、Dry run 演示图、成本预估、单次与月度预算硬上限、实时生成进度 |
| 交付 | ZIP 素材包（图片 + manifest.json + prompts.txt）、按批次同步到资产盘、元数据完整性检查 |
| 海量图片 | SQLite/WAL 索引、SQL 下推筛选排序分页、无限滚动、标准库 PNG 缩略图缓存 |

资产根目录默认 `D:/gdrive.photo/ai-assets`（Google Drive 同步盘的本地映射目录），可用 `AI_STUDIO_ASSET_ROOT` 覆盖。

## 游戏资产生产线

### 小说 → 角色卡 → 可复现锚点

- 粘贴小说正文，有 key 时走 `chat/completions` 结构化提取，失败或无 key 自动降级为本地启发式（中文姓氏 + 动作词识别人名，词典抽取外貌特征）。
- 每条特征保留**原文证据**和 `explicit / inferred / missing` 置信度，必须人工核对后再保存。
- 保存后生成固定、带版本号的 **Character Anchor**：一段逐字复用的英文人物设定 + 一致性规则 + 构图规则。改 profile 自动重建锚点并 bump `anchor_version`；手工编辑锚点则保留手工文本。版本变了说明设定改过，旧图与新图不要混用。
- 指定一张已有 PNG 作为参考图后，复现和逐视图生成走 `/v1/images/edits`，这是保持人物长相与画风最有效的手段。支持 `input_fidelity: high` 的模型会自动带上；`gpt-image-2` 等新模型不支持该参数（也不支持 `background: transparent`），系统会按 API 报错逐个去掉后重试，参考图仍然生效。

### 人物多视角包

`styles/character-turnaround.json` + `studio/turnaround.py`，输出 `turnaround.png`、四张独立视图 PNG、`views.json`、`character-card.json`、`preview.html`、`README.txt` 和 ZIP。

- `sheet`：一次生成四格设定图（正面 / 3-4 侧 / 正侧面 / 背面）再等分切图，1 张图的成本，内部一致性最好。
- `separate`：逐视图生成，4 张成本，可逐张指定视角并叠加参考图。
- `demo`：无 key 或 Dry run 时只产出布局占位，明确标注，**不写资产库、不调用 API**。

### 精灵动画切图包

`studio/sprites.py` 从同一张基准 PNG 做确定性的位移、挤压拉伸、摆动、镜像变换，帧天然对齐（让模型「画 8 帧」做不到这一点）。四角自动抠背景，可选不抠或指定色值。

输出：

```
sheet.png                 网格精灵图（RGBA）
sheet.json                Aseprite JSON Hash（Phaser 3 / Pixi / Aseprite）
sheet.array.json          TexturePacker JSON Array
frames/*.png              逐帧独立 PNG，任何引擎都能用
godot/SpriteFrames.tres   Godot 4 AnimatedSprite2D 资源
unity/UnitySpriteSlicer.cs + unity/slices.json   Unity Editor 自动切片
rpgmaker/!$name.png       3×4 行走图布局
preview.html              可播放动画预览，零依赖
README.txt                各引擎导入说明
```

支持动作：`idle` 待机、`walk` 行走、`run` 奔跑、`jump` 跳跃、`attack` 攻击、`hurt` 受击。每段帧数、格子尺寸、每行帧数、fps、抠色容差都可调。RPG Maker 的四个方向目前由镜像与位移近似，正式素材请用多视角功能生成后再替换。

### 画风配方与可复现批量

角色提示词由「风格配方 + 角色锚点」拼成，配方可在工作台的「画风配方」下拉框切换。`styles/character-turnaround.json` 是扁平矢量设定图，`styles/sanguo-hero.json` 是高细节三国工笔立绘（默认 `gpt-image-2 / high / 1024x1536`）。新增自己的画风只需复制一份 JSON 并把 `status` 设为 `tool`。

`batches/sanguo-heroes.json` + `tools/generate_sanguo.py` 是一份完整的可复现批量示例：小说摘录 → 文本模型提取 + 人工校订 → 6 张角色卡（带锚点）→ 6 张高细节立绘 + 四视图设定图 → 导出 ZIP。

```bash
python tools/generate_sanguo.py --list            # 看计划与预估成本
python tools/generate_sanguo.py                   # 全量生成
python tools/generate_sanguo.py --only 关羽        # 只跑一个角色
python tools/generate_sanguo.py --skip-extract    # 原样复跑，已生成的图自动跳过、不重复计费
```

脚本是幂等的：按 `character_id` + 资产类型查重，中断后直接重跑即可续上。导出的 ZIP 内含 `images/`、`manifest.json`（每张图的 prompt / model / quality / size / `character_id` / `anchor_version` / cost / 来源元数据）、`character-cards.json`（逐字复用的锚点与版本）、`style-bible.json`（画风）、`recipe.json`（本次全部参数与复现命令）、`prompts.txt`、`README.txt`。API 原图的 C2PA 来源元数据不剥离，切分出来的派生图标为 `derived`。

## 主要 API

```
GET    /api/config  /api/stats  /api/health  /api/pricing  /api/styles
GET    /api/assets?limit=&offset=&character_id=      分页资产
PATCH  /api/assets/<id>                             改评分 / 标签 / 状态
POST   /api/generate  /api/export  /api/sync
POST   /api/key  /api/key/check  /api/key/persist  /api/settings
POST/DELETE /api/key/file                         明文写入 / 删除 config.json 密钥
GET    /api/game/meta                                角色表单元数据
POST   /api/characters/extract                      小说特征提取
GET/POST/DELETE /api/characters[/<id>]              角色卡
GET    /api/characters/<id>/prompt?view=front        完整提示词预览
POST   /api/characters/<id>/reproduce                锚点复现单张
POST   /api/characters/<id>/turnaround               四视图包
POST   /api/sprites                                  精灵动画包
GET    /api/packs  /api/packs/<id>/zip  DELETE /api/packs/<id>
GET    /api/scenes  /api/comic                        场景卡与连环画批次
GET    /api/stories?q=&status=                         故事候选库（只读）
GET    /pack/story-bank/index.html                     可分享的离线故事库
GET    /pack/<id>/<file>                             包内预览与图片
```

## 连环画批量（GPT-6.1 Sol 写剧本 + gpt-image-2.6-sunburst 出图）

- `tools/write_comic.py` 用文本模型 `gpt-6.1-sol` 选一个《三国演义》未收录、见于《三国志》等史料的三国故事，产出 6 格分镜脚本 `batches/comic-*.json`：旁白、对白、画面描述、新人物设定、场景定义，并记录模型 usage 与 prompt 摘要便于追溯。
- `tools/generate_comic.py` 幂等生成：新人物立绘 → 无人物场景概念图 → 6 格分镜。**画面一律不含文字**，旁白与对白由 `comic.html` 排版叠加，避免模型写错字。
- 人物：已有角色（如曹操）复用角色卡 anchor 与已有立绘，作为 `/images/edits` 参考图延续长相与画风；资产库里没有的角色自动建 17 字段角色卡、生成立绘并设为该角色的参考图。多人物分镜会一次传入多张参考图，`studio/imaging.py` 自动使用 `image[]` 数组语法（`gpt-image-2.6-sunburst` 不接受重复的 `image` 字段），并按需丢弃模型不支持的可选参数。
- 场景：`studio/scenes.py` + SQLite `scenes` 表。场景卡保存完整 prompt、环境描述、模型参数与资产编号，独立入库、可复用于其它故事；`GET /api/scenes`、`GET /api/comic` 可查询。
- 画风 `styles/sanguo-comic.json` 沿用 `sanguo-hero` 的高细节历史颜料风格，模型 `gpt-image-2.6-sunburst / high`，分镜与场景 `1536x1024`、立绘 `1024x1536`。
- 导出 ZIP 含 `images/`、`comic.html`、`script.json`、`manifest.json`（含 scene_id / panel_no / 对白）、`character-cards.json`、`scene-cards.json`、`style-bible.json`、`recipe.json`、`README.txt`；同步到资产盘，并在工作台 `http://127.0.0.1:8777/pack/<批次id>/comic.html` 可直接阅读。

```bash
python tools/write_comic.py                 # 用文本模型创作剧本
python tools/generate_comic.py --list      # 打印计划与预估成本
python tools/generate_comic.py             # 全量生成（幂等，中断可重跑）
python tools/generate_comic.py --export-only
python tools/generate_comic.py --redo-degraded-panels  # 只补生成降级为纯文本的分镜
python tools/generate_comic.py --redo-panels           # 重做全部 6 格（会重复计费）
```

注意：`gpt-image-2.6-sunburst` 的官方单价未由 `/v1/models` 公开，`studio/pricing.py` 的 `ESTIMATED_MODELS` 按上一代同档估算记账；收到正式账单后应更新该表。

## 十集系列批量（共享人物资产库）

`tools/write_comic_series.py` 用 `gpt-6.1-sol` 一次规划 10 个《三国演义》未收录的史料小故事（选题缓存在 `batches/series-*-plan.json`，可用 `--replan` 重选），再逐集写出完整剧本 `batches/comic-sNN-*.json`；每集 2 个场景、6 格分镜、每格最多 2 个人物。

`tools/generate_comic_series.py` 是总控：

1. **先共享、后并发**：所有角色卡只建一次，缺立绘的人物统一生成一张全系列共用立绘（线程池），之后才启动各集进程。这样跨集复用同一人物（如曹丕同时出现在第 4、5 集）不会有依赖或重复计费。
2. 各集用独立进程跑 `generate_comic.py --plan <剧本> --cards-ready`（角色卡只读，SQLite WAL + busy_timeout 保证并发写安全），每集自己导出 ZIP、`comic.html`、场景卡并同步到资产盘。全程幂等，中断重跑自动跳过已入库的图。
3. 最后生成系列 `index.html`（10 集卡片、缩略图、史料依据、成本、阅读/下载入口）与 `series-manifest.json`，连同 10 个素材包 ZIP 一起写入 `D:\gdrive.photo\ai-assets\all\sanguo-tales-10\` 和工作台 `data/packs/sanguo-tales-10/`。每集 ZIP 的 `shared-portraits/` 会附本集用到、但归在全系列共享资产下的角色立绘副本，因此单集包也是自足的。

```bash
python tools/write_comic_series.py --show          # 只看 10 集选题
python tools/generate_comic_series.py --list       # 打印计划与预估成本
python tools/generate_comic_series.py --prepare-only --skip-portraits  # 只建角色卡（零花费）
python tools/generate_comic_series.py --workers 4 --fix-degraded
python tools/generate_comic_series.py --index-only # 只重建索引并同步
python tools/generate_comic_series.py --episodes 1,2,3
```

`--fix-degraded` 会在全部生成完后自动补跑「降级为纯文本」或缺失的分镜；单集也可 `python tools/generate_comic.py --plan batches/comic-sNN-*.json --redo-degraded-panels`。阅读页在工作台 `http://127.0.0.1:8777/pack/sanguo-tales-10/index.html`。

## 故事候选库（只收集，不出图）

`tools/collect_stories.py` 用文本模型按 8 个来源分批收集候选小故事（魏书 / 蜀书 / 吴书及裴注、《世说新语》、《后汉书》《晋书》、器物制度、演义亦载、跨来源补漏）。每批单独缓存到 `data/story-collect/`，重跑零花费，失败一批不影响其它批。

`tools/build_story_bank.py` 纯离线：合并 `write_comic_series.py` 中 24 条人工核验候选与模型批次，去重、排除已拍故事、套用人工考据结论，产出 `batches/story-bank.json`（唯一数据源）+ 自包含的 `data/packs/story-bank/index.html`，并同步到 `D:\gdrive.photo\ai-assets\all\story-bank\`。

- **口径已放宽**：《三国演义》写过的故事同样收录，用 `romance_group` 标为「演义亦载 / 仅被提及 / 有相似回目 / 演义未载 / 归属待考」。`batches/romance-toc.json` 存了 120 回回目，`toc_hits` 只是参考提示，不代表该故事被正面叙写。
- **等级**：A 人工核验基线；B 模型采集、高置信且有原文摘录；C 中置信建议抽查；R 待复核（出处/原文存疑或归属不确定）；U 已拍成连环画。
- 每条保留出处、原文摘录、梗概、人物、地域、情绪、演义归属依据、可视化钩子与回目提示，供后续选题直接挑。

```bash
python tools/collect_stories.py --show                # 查看已缓存批次
python tools/collect_stories.py --only romance,mixed   # 只补采某几批
python tools/collect_stories.py --workers 3            # 全部批次，3 并发
python tools/build_story_bank.py                      # 合并、渲染 HTML、同步云盘
python tools/build_story_bank.py --show --no-sync     # 只看统计
```

工作台「故事库」页也能查看与筛选：`http://127.0.0.1:8777/` → 故事库。

### 资料复核（零图像花费）

`tools/review_stories.py` 用文本模型分批复核有缺口的条目：把人工基线的占位原文换成可核查原文摘录，逐条确认出处与演义归属，结果落到 `batches/story-reviews.json`。`build_story_bank.py` 合并复核结果，`reviewed` / `data_complete` / `quote_status` 会写进每条候选；人工考据结论（`ROMANCE_VERDICTS`）优先级最高，永远覆盖模型判定。当前 87 条全部资料完整、0 条待复核。

```bash
python tools/review_stories.py --show         # 查看复核缓存
python tools/review_stories.py                # 只复核有缺口的条目
python tools/build_story_bank.py              # 合并复核结果并同步
```

### 全量分批队列（80 集）

`tools/run_story_queue.py` 把 `status=candidate` 的候选切成 8 批 × 10 集，逐批复用已验证的系列生产线；`batches/story-queue.json` 记录每批状态、成本、资产数与新角色名，中断后重跑会跳过已写剧本和已入库图片，绝不重复计费。每批独立生成自己的 `series-*.json`、阅读目录与 ZIP，并同步到 `D:\gdrive.photo\ai-assets\all\sanguo-tales-qNN\`。

```bash
python tools/run_story_queue.py --prepare          # 只生成队列，不调用 API
python tools/run_story_queue.py --status           # 进度、花费、下一批
python tools/run_story_queue.py --run-next         # 跑下一个未完成批次
python tools/run_story_queue.py --batch 3          # 单独重试第 3 批
python tools/run_story_queue.py --all --workers 3  # 依次跑完全部批次

# 中断后重启（自动跳过已写剧本与已入库图片，不会重复计费）
python tools/run_story_queue.py --all --workers 3 --attempts 3 --child-watchdog 1200
```

注意：`gpt-image-2.6-sunburst` 单价为估算值，队列里 `budget_spent_usd` 是估算累计，不是账单；`--status` 可随时查看并叫停。

### 长跑稳定性（三层防护）

1. **单集看门狗**：`generate_comic_series.py --watchdog 600`（队列自动传入）会在某一集日志静默 600 秒时判定本地代理长连接半开，终止该 worker，把这一集留给 `--fix-degraded` 补跑；`--watchdog 0` 可关闭。
2. **子进程看门狗**：`run_story_queue.py --child-watchdog 1200` 同时监控系列总控的 stdout 与本批各集日志的最新写入时间，静默 20 分钟即 `taskkill /T /F` 整棵进程树并重试本批。笔记本休眠、整机挂起导致所有 worker 同时僵死时，这一层能兜住（单集看门狗对「父进程一起冻结」无能为力）。**必须看日志**：总控只在一集结束时才打印，正常也要 13–30 分钟，只看 stdout 会把健康批次误杀。
3. **完整性校验 + 重试**：每批生成后逐集核对分镜数量与 `reference_used` / `degraded` 标记，只有 10 集全部 6 格合格且子进程 rc=0 才标记 `done`；否则最多重试 `--attempts` 次，每次只补缺口，绝不重复计费。失败会写 `error` 字段并停下队列，不会假装完成。

剧本阶段也是断点安全的：只要 `series-*.json` 指向的 10 个剧本文件都在，就自动跳过文本模型阶段，无需手动加 `--skip-scripts`。

**人工恢复流程**：若队列长时间无输出，`python tools/run_story_queue.py --status` 看状态；确认僵死后终止 `run_story_queue` / `generate_comic_series` / `generate_comic` 进程树，再用上面「中断后重启」命令重跑即可。已入库图片全部跳过。

### 重做某个角色的形象

改 `batches/sanguo-heroes.json` 里该角色的 `pose` 与 `profile`（视觉设定的唯一来源），再执行：

```bash
python tools/redo_character.py --character 诸葛亮 --dry-run   # 先看会归档哪些资产
python tools/redo_character.py --character 诸葛亮
```

脚本会：① 把旧立绘 / 四视图 / 相关分镜**移动**到 `data/raw/_superseded/<slug>-<时间戳>/`（不删除，可回滚）并从索引移除；② 清空 `ref_asset_id`、按新 profile 重建角色锚点并升版；③ `--force` 重出立绘与四视图；④ 重做受影响的整部连环画（已有场景概念图仍复用）；⑤ 重建系列索引、ZIP 并同步云盘；⑥ 校验 12 格分镜全部带参考图、0 降级。

要调整受影响剧集清单，改 `tools/redo_character.py` 顶部的 `PLANS` / `SERIES`。**注意**：`sanguo-heroes.json` 的 `novel_excerpt` 是原文摘录，不做改动；只改视觉设定。重做后旧图在云盘解压目录里可能仍有残留（覆盖式同步不删旧文件），但 `comic.html` / manifest 只引用新图。

### 修订单集的画面规则

`tools/revise_wangmei.py` 演示了「望梅止渴」这类的单集返工：原剧本的 `visual_rules` 写了「每格不超过两人、不得有背景人群」并强制沿用曹操既有朝服，导致行军故事里没有队伍、曹操穿朝服。修订脚本会：① 新建一个**本集专用**角色卡「曹操·行军」（戎装），不动全局曹操卡，避免影响其他集；② 改写 `visual_rules` 与 6 格 `visual`，明确要求 5–8 人的同装束行军队列；③ 软化场景 `prompt_extra` 里的 `no people`（否则 `panel_prompt` 的 CONTINUITY 段会压制队伍）；④ 归档旧分镜后不带 `--cards-ready` 重跑 `generate_comic.py`（它会自动建卡 + 出行军立绘，已有士卒立绘与场景图仍跳过）；⑤ 重建系列索引。

`python tools/revise_wangmei.py --dry-run` 可只预览新规则。教训：**人数上限写在 `panel["characters"]`（参考图数量，≤2）没问题，但写进 `visual_rules` 变成「画面里不许有其他人」，就会把该有的群众场面一起禁掉**。

### 只重做某几格

局部瑕疵（手部畸形、左右手画反、道具错误）不必重拍整集。先在剧本里改对应格的 `visual`（必要时在 `visual_rules` 补通用约束），再执行：

```bash
python tools/redo_panels.py --plan batches/comic-xxx.json --panels 4,5 \
    --series batches/series-xxx.json --dry-run   # 预览
python tools/redo_panels.py --plan batches/comic-xxx.json --panels 4,5 \
    --series batches/series-xxx.json
```

脚本把指定格的旧图**移动**到 `data/raw/_superseded/` 并从索引移除，然后跑 `generate_comic.py --plan ... --cards-ready`：其余格、场景概念图、人物立绘都会被幂等查找跳过，**两格的修复只花两张图的钱**。最后重建系列索引并校验 6 格齐全、全部带参考图、0 降级。

已用此法修过「孙权庱亭射虎」第 4/5 格的手：在两格 `visual` 里写明「双手各 5 指、拇指压在戟杆前、手腕与前臂顺直、禁止镜像/换手/反掌」，并把最容易画崩的**张开手掌改成半握**（另一手改为握缰的握拳），同时在 `visual_rules` 加一条全集通用的手部解剖约束。

## 目录

```
styles/          风格配方（含 character-turnaround / sanguo-hero / sanguo-comic）
studio/          config / store(SQLite) / imaging / text / characters / scenes / turnaround / sprites / png / thumbs / credentials / exporter / pricing
web/             单页管理界面
tools/           命令行生成、剧本创作、连环画批量、批量导入、缩略图预热
data/            本地数据（被 .gitignore 排除）
  assets.db      SQLite 索引（含 characters / packs / scenes 表）
  raw/           生成原图
  thumbs/        缩略图缓存
  packs/         多视角包、精灵包与连环画阅读页（目录 + ZIP）
  export/        导出的素材包
server.py        本地 HTTP 服务
```

## 说明

- 默认 Dry run，不调用 API、不花钱；真实生成必须显式取消 Dry run 且配置有效 key。
- 生成图自带的 C2PA / AIGC 来源元数据与显式标识不要剥离，「元数据完整性」页会检查。发布到平台时按平台要求勾选「含 AI 生成内容」。
- 不要生成在世艺术家风格、公众人物或真实人脸的写实形象；涉及人脸素材需单独取得同意。
- 本项目只是本地内容生产工具，不对外提供生成能力，也不含任何后端服务或用户系统。

MIT License，见 [LICENSE](LICENSE)。
