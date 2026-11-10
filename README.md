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
GET    /pack/<id>/<file>                             包内预览与图片
```

## 目录

```
styles/          风格配方（含 character-turnaround.json）
studio/          config / store(SQLite) / imaging / characters / turnaround / sprites / png / thumbs / credentials / exporter / pricing
web/             单页管理界面
tools/           命令行生成、批量导入、缩略图预热
data/            本地数据（被 .gitignore 排除）
  assets.db      SQLite 索引
  raw/           生成原图
  thumbs/        缩略图缓存
  packs/         多视角包与精灵包（目录 + ZIP）
  export/        导出的素材包
server.py        本地 HTTP 服务
```

## 说明

- 默认 Dry run，不调用 API、不花钱；真实生成必须显式取消 Dry run 且配置有效 key。
- 生成图自带的 C2PA / AIGC 来源元数据与显式标识不要剥离，「元数据完整性」页会检查。发布到平台时按平台要求勾选「含 AI 生成内容」。
- 不要生成在世艺术家风格、公众人物或真实人脸的写实形象；涉及人脸素材需单独取得同意。
- 本项目只是本地内容生产工具，不对外提供生成能力，也不含任何后端服务或用户系统。

MIT License，见 [LICENSE](LICENSE)。
