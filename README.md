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
