"""Sprite-sheet production for real game engines.

A single character reference image is normalised into a transparent, square
sprite, then animated with deterministic per-frame transforms (offset, squash
& stretch, sway, mirror). Frames are always pixel-aligned because they all come
from the same source - that is something an AI "draw me 8 frames" prompt cannot
guarantee.

Outputs a self-contained pack:
  sheet.png                 Aseprite / Phaser / generic grid
  sheet.json                Aseprite JSON Hash (Phaser 3 atlas)
  sheet.array.json          TexturePacker JSON Array (Phaser / Pixi / Unity tools)
  frames/*.png              one PNG per frame, works in literally any engine
  godot/SpriteFrames.tres   Godot 4 AnimatedSprite2D resource
  unity/UnitySpriteSlicer.cs + slices.json   Unity Editor auto-slicer
  rpgmaker/!$name.png       3x4 walk layout (approximated directions)
  preview.html              playable animation, no dependencies
  README.txt                import instructions per engine
"""
from __future__ import annotations

import html
import json
import math
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import characters, png, store

ACTIONS = {
    "idle": "待机 / 呼吸",
    "walk": "行走",
    "run": "奔跑",
    "jump": "跳跃",
    "attack": "攻击",
    "hurt": "受击",
}
DEFAULT_ACTIONS = ["idle", "walk"]
ENGINES = ["generic", "godot", "unity", "phaser", "rpgmaker"]


def _frame(action: str, i: int, n: int) -> dict:
    """Deterministic transform parameters for one frame."""
    t = (i / max(1, n)) * 2 * math.pi
    p = i / max(1, n - 1)
    if action == "idle":
        return dict(dy=round(2 * math.sin(t)), sy=1.0 + 0.018 * math.sin(t))
    if action == "walk":
        return dict(dy=-abs(round(2 * math.sin(t))), dx=round(2 * math.sin(t)),
                    wave=2.0 * math.sin(t))
    if action == "run":
        return dict(dy=-abs(round(4 * math.sin(t))), dx=round(4 * math.sin(t)),
                    wave=4.0 * math.sin(t), sx=1.0 + 0.04 * math.sin(t))
    if action == "jump":
        return dict(dy=-round(20 * math.sin(math.pi * p)),
                    sy=1.0 + 0.10 * math.sin(math.pi * p))
    if action == "attack":
        return dict(dx=round(12 * math.sin(math.pi * p)),
                    wave=7.0 * math.sin(math.pi * p),
                    sx=1.0 + 0.07 * math.sin(math.pi * p))
    if action == "hurt":
        return dict(dx=round(5 * math.sin(2 * t)), wave=-3.0 * math.sin(t))
    return dict()


def _square(img: png.Image) -> png.Image:
    side = min(img.width, img.height)
    return png.crop(img, (img.width - side) // 2, (img.height - side) // 2,
                    side, side)


def _parse_color(value: str):
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _aseprite(frames: list, cell: int, cols: int, fps: int, tags: list) -> dict:
    out = {}
    for i, name in enumerate(frames):
        c, r = i % cols, i // cols
        out[name + ".png"] = {
            "frame": {"x": c * cell, "y": r * cell, "w": cell, "h": cell},
            "rotated": False, "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0, "w": cell, "h": cell},
            "sourceSize": {"w": cell, "h": cell},
            "duration": round(1000 / max(1, fps)),
        }
    rows = math.ceil(len(frames) / cols)
    return {
        "frames": out,
        "meta": {
            "app": "ai-asset-studio", "version": "1.0",
            "image": "sheet.png", "format": "RGBA8888",
            "size": {"w": cols * cell, "h": rows * cell}, "scale": "1",
            "frameTags": tags,
        },
    }


def _godot(frames: list, cell: int, cols: int, fps: int, tags: list) -> str:
    lines = ["[gd_resource type=\"SpriteFrames\" load_steps=%d format=3]" % (2 + len(frames)),
             "", '[ext_resource type="Texture2D" path="res://sheet.png" id="1_sheet"]', ""]
    for i in range(len(frames)):
        c, r = i % cols, i // cols
        lines += ['[sub_resource type="AtlasTexture" id="AtlasTexture_%d"]' % i,
                  'atlas = ExtResource("1_sheet")',
                  'region = Rect2(%d, %d, %d, %d)' % (c * cell, r * cell, cell, cell), ""]
    blocks = []
    for tag in tags:
        fr = []
        for i in range(tag["from"], tag["to"] + 1):
            fr.append('{"duration": 1.0, "texture": SubResource("AtlasTexture_%d")}' % i)
        blocks.append('{\n"frames": [%s],\n"loop": true,\n"name": &"%s",\n"speed": %d.0\n}'
                      % (", ".join(fr), tag["name"], fps))
    lines += ["[resource]", "animations = [%s]" % ",\n".join(blocks), ""]
    return "\n".join(lines)


def _unity_script() -> str:
    return '''// UnitySpriteSlicer.cs -- put this file in Assets/Editor/
// Menu: Tools > AI Asset Studio > Slice Sprite Sheet
// Adjust SHEET_PATH to where you imported sheet.png.
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;

public class UnitySpriteSlicer : EditorWindow
{
    const string SHEET_PATH = "Assets/sheet.png";
    const string SLICES = "slices.json";

    [System.Serializable] class Slice { public string name; public int x, y, w, h; }
    [System.Serializable] class SliceList { public List<Slice> slices; }

    [MenuItem("Tools/AI Asset Studio/Slice Sprite Sheet")]
    static void Slice()
    {
        var importer = AssetImporter.GetAtPath(SHEET_PATH) as TextureImporter;
        if (importer == null) { Debug.LogError("Import sheet.png first: " + SHEET_PATH); return; }
        string jsonPath = Path.Combine(Application.dataPath, "..",
            Path.GetDirectoryName(SHEET_PATH.Replace("Assets/", "")), SLICES);
        if (!File.Exists(jsonPath))
            jsonPath = Path.Combine(Directory.GetParent(Application.dataPath).FullName, SLICES);
        if (!File.Exists(jsonPath)) { Debug.LogError("slices.json not found next to the sheet"); return; }

        var data = JsonUtility.FromJson<SliceList>(File.ReadAllText(jsonPath));
        var meta = new List<SpriteMetaData>();
        foreach (var s in data.slices)
            meta.Add(new SpriteMetaData {
                name = s.name,
                rect = new Rect(s.x, s.y, s.w, s.h),
                alignment = (int)SpriteAlignment.Custom,
                pivot = new Vector2(0.5f, 0f)
            });
        importer.textureType = TextureImporterType.Sprite;
        importer.spriteImportMode = SpriteImportMode.Multiple;
        importer.spritePixelsPerUnit = 100;
        importer.filterMode = FilterMode.Point;
        importer.textureCompression = TextureImporterCompression.Uncompressed;
        importer.mipmapEnabled = false;
        importer.spritesheet = meta.ToArray();
        importer.SaveAndReimport();
        Debug.Log("Sliced " + meta.Count + " sprites.");
    }
}
'''


def _preview(sheet_w: int, sheet_h: int, cell: int, cols: int,
             tags: list, fps: int, name: str) -> str:
    anims = ", ".join('"%s"' % t["name"] for t in tags)
    tags_json = json.dumps(tags, ensure_ascii=False)
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>%(title)s - 精灵动画预览</title>
<style>
body{font-family:system-ui,sans-serif;background:#15171c;color:#e8e8e8;margin:0;padding:24px}
h1{font-size:16px;font-weight:500;margin:0 0 4px}.sub{color:#8b8f99;font-size:12px;margin-bottom:20px}
.panel{background:#20242c;border:1px solid #333944;border-radius:10px;padding:16px;display:inline-block}
.stage{width:%(cw)dpx;height:%(ch)dpx;background-color:#2b303a;
 background-image:linear-gradient(45deg,#242830 25%%,transparent 25%%),linear-gradient(-45deg,#242830 25%%,transparent 25%%),linear-gradient(45deg,transparent 75%%,#242830 75%%),linear-gradient(-45deg,transparent 75%%,#242830 75%%);
 background-size:20px 20px;background-position:0 0,0 10px,10px -10px,-10px 0;
 image-rendering:pixelated;overflow:hidden;border-radius:6px}
.sprite{width:%(cell)dpx;height:%(cell)dpx;background-image:url('sheet.png');background-repeat:no-repeat;image-rendering:pixelated}
.controls{margin-top:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:12px;color:#b9bdc7}
button,select,input{background:#2b303a;color:#e8e8e8;border:1px solid #3b424e;border-radius:6px;padding:5px 10px;font-size:12px}
button{cursor:pointer}label{display:flex;gap:6px;align-items:center}
</style></head><body>
<h1>%(title)s</h1><div class="sub">sheet.png · %(cell)dx%(cell)d / 帧 · %(count)d 帧 · 点击播放查看循环效果</div>
<div class="panel">
 <div class="stage" id="stage"><div class="sprite" id="sprite"></div></div>
 <div class="controls">
  <select id="anim"></select>
  <button id="play">暂停</button>
  <label>速度 <input type="range" id="fps" min="1" max="24" value="%(fps)d"><span id="fpsv">%(fps)d</span> fps</label>
  <label>缩放 <input type="range" id="zoom" min="1" max="6" value="3"><span id="zoomv">3x</span></label>
 </div>
</div>
<script>
const TAGS=%(tags)s, CELL=%(cell)d, COLS=%(cols)d, NAMES=[%(anims)s];
const sprite=document.getElementById('sprite'),stage=document.getElementById('stage');
const anim=document.getElementById('anim'),fpsEl=document.getElementById('fps');
const zoomEl=document.getElementById('zoom'),playBtn=document.getElementById('play');
NAMES.forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;anim.appendChild(o)});
let idx=0,cur=0,playing=true,timer=null;
function tag(){return TAGS[anim.selectedIndex]||TAGS[0]}
function draw(){
 const t=tag(); if(!t)return;
 const i=t.from+idx, c=i%%COLS, r=Math.floor(i/COLS);
 sprite.style.backgroundPosition=(-c*CELL)+'px '+(-r*CELL)+'px';
}
function loop(){if(!playing)return;const t=tag();idx=(idx+1)%%(t.to-t.from+1);draw();
 clearTimeout(timer);timer=setTimeout(loop,1000/Number(fpsEl.value));}
function restart(){idx=0;draw();clearTimeout(timer);if(playing)timer=setTimeout(loop,1000/Number(fpsEl.value));}
function zoom(){const z=Number(zoomEl.value);stage.style.width=(CELL*z)+'px';stage.style.height=(CELL*z)+'px';
 sprite.style.transform='scale('+z+')';sprite.style.transformOrigin='0 0';document.getElementById('zoomv').textContent=z+'x';}
anim.onchange=restart;fpsEl.oninput=()=>{document.getElementById('fpsv').textContent=fpsEl.value;restart()};
zoomEl.oninput=zoom;playBtn.onclick=()=>{playing=!playing;playBtn.textContent=playing?'暂停':'播放';restart()};
zoom();restart();
</script></body></html>
""" % dict(title=html.escape(name), cw=cell * 3, ch=cell * 3,
           sw=sheet_w, sh=sheet_h, cell=cell, count=sum(
               t["to"] - t["from"] + 1 for t in tags),
           tags=tags_json, anims=anims, fps=fps, cols=cols, name=html.escape(name))


def _readme(name: str, cell: int, fps: int, actions: list, engines: list) -> str:
    return f"""AI 资产工作台 · 精灵动画包
================================
角色/资产: {name}
每帧尺寸: {cell} x {cell}   默认帧率: {fps} fps
动作: {', '.join(actions)}
生成时间: {datetime.now().astimezone().isoformat(timespec='seconds')}

文件说明
--------
sheet.png            所有帧按从左到右、从上到下排列的网格图（RGBA 透明背景）
sheet.json           Aseprite JSON Hash，Phaser 3 / PixiJS / Aseprite 可直接用
sheet.array.json     TexturePacker JSON Array，另一种常见 atlas 格式
frames/              逐帧独立 PNG，任何引擎都能用，最保险
preview.html         双击即可播放动画（无需服务器、无需依赖）

按引擎使用
----------
【Godot 4】
  把 sheet.png 和 godot/SpriteFrames.tres 一起放进项目（同一目录）。
  给 AnimatedSprite2D 节点把 Sprite Frames 指向这个 .tres，
  代码里 $AnimatedSprite2D.play("idle") 即可。
  若 .tres 里的 res://sheet.png 路径不对，用文本编辑器改 ext_resource 那一行。

【Phaser 3】
  把 sheet.png 和 sheet.json 放到 public/assets/：
  this.load.atlas('hero', 'assets/sheet.png', 'assets/sheet.json');
  this.anims.create({{ key:'idle', frames:this.anims.generateFrameNames(
      'hero', {{ start:0, end:7, prefix:'idle_', zeroPad:2, suffix:'.png' }}),
      frameRate:{fps}, repeat:-1 }});
  帧名见 sheet.json 的 frames 键（如 idle_00.png）。

【Aseprite】
  File > Import Sprite Sheet 选 sheet.png，Grid 设为 {cell}x{cell}；
  或直接用 sheet.json 的 frameTags 恢复动画标签。

【Unity】
  把 sheet.png、slices.json 一起导入 Assets/，
  把 unity/UnitySpriteSlicer.cs 放进 Assets/Editor/，
  菜单 Tools > AI Asset Studio > Slice Sprite Sheet 自动切出全部 Sprite。
  脚本里 SHEET_PATH / SLICES 路径可按你的目录调整。
  FilterMode 已设为 Point、关闭 Mipmap，适合像素/扁平素材。

【RPG Maker MZ / MV】
  rpgmaker/!$<name>.png 是 3 列 x 4 行的行走图布局（$ 表示单角色）。
  注意：四个方向目前由镜像与位移近似，不是真正绘制的背面/侧面；
  正式素材请用「人物多视角」功能生成四视图后再替换。

【其他引擎 / 自研】
  直接用 frames/ 下的逐帧 PNG，按文件名顺序播放即可。

透明背景
--------
默认按图片四角颜色做抠色。若边缘残留背景或角色被误抠，
重新生成时调整 tolerance（默认 40）或改用 bg=none 保留原图。
"""


def build(cfg: dict, *, asset: dict, actions: list | None = None,
          frames_per_action: int = 8, cell: int = 128, cols: int = 8,
          fps: int = 8, bg: str = "auto", tolerance: int = 40,
          engines: list | None = None, name: str = "") -> dict:
    rel = str(asset.get("file") or "")
    src = cfg["local"] / rel
    if not src.is_file() or src.suffix.lower() != ".png":
        raise ValueError("基准图必须是 PNG（当前资产不是 PNG 或文件缺失）。")
    raw = png.decode(src.read_bytes())
    if raw is None:
        raise ValueError("无法解析这张 PNG（仅支持 8-bit RGB/RGBA 非隔行）。")

    actions = [a for a in (actions or DEFAULT_ACTIONS) if a in ACTIONS] or list(DEFAULT_ACTIONS)
    frames_per_action = max(2, min(int(frames_per_action or 8), 24))
    cell = max(32, min(int(cell or 128), 512))
    cols = max(1, min(int(cols or 8), 16))
    fps = max(1, min(int(fps or 8), 30))
    engines = [e for e in (engines or ENGINES) if e in ENGINES] or list(ENGINES)

    base = _square(raw)
    base = png.resize(base, cell, cell)
    if bg == "none":
        base = png.to_rgba(base)
    elif bg.startswith("#"):
        color = _parse_color(bg) or png.corner_color(base)
        base = png.chroma_key(base, color=color, tolerance=tolerance)
    else:
        base = png.chroma_key(base, tolerance=tolerance)

    frame_names, frames, tags = [], [], []
    for action in actions:
        start = len(frames)
        for i in range(frames_per_action):
            params = _frame(action, i, frames_per_action)
            frames.append(png.transform(base, cell, cell, **params))
            frame_names.append(f"{action}_{i:02d}")
        tags.append({"name": action, "from": start, "to": len(frames) - 1,
                     "direction": "forward"})

    sheet_img = png.sheet(frames, cols, cell, cell)
    sheet_w, sheet_h = sheet_img.width, sheet_img.height
    pack_id = characters.new_id("sprites")
    safe = "".join(c for c in (name or asset.get("label") or asset.get("id") or "sprite")
                   if c.isalnum() or c in "-_")[:40] or "sprite"
    root = cfg["local"] / "packs" / pack_id
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    (root / "sheet.png").write_bytes(png.encode(sheet_img))
    (root / "frames").mkdir()
    for nm, frame in zip(frame_names, frames):
        (root / "frames" / (nm + ".png")).write_bytes(png.encode(frame))

    aseprite = _aseprite(frame_names, cell, cols, fps, tags)
    (root / "sheet.json").write_text(
        json.dumps(aseprite, ensure_ascii=False, indent=2), encoding="utf-8")
    array = {"frames": [dict(filename=k + ".png", **v) for k, v in
                        aseprite["frames"].items()], "meta": aseprite["meta"]}
    (root / "sheet.array.json").write_text(
        json.dumps(array, ensure_ascii=False, indent=2), encoding="utf-8")

    if "godot" in engines:
        (root / "godot").mkdir()
        (root / "godot" / "SpriteFrames.tres").write_text(
            _godot(frame_names, cell, cols, fps, tags), encoding="utf-8")
    if "unity" in engines:
        (root / "unity").mkdir()
        (root / "unity" / "UnitySpriteSlicer.cs").write_text(
            _unity_script(), encoding="utf-8")
        slices = {"slices": [
            {"name": nm + ".png", "x": (i % cols) * cell,
             "y": sheet_h - (i // cols + 1) * cell, "w": cell, "h": cell}
            for i, nm in enumerate(frame_names)]}
        (root / "unity" / "slices.json").write_text(
            json.dumps(slices, ensure_ascii=False, indent=2), encoding="utf-8")
    if "rpgmaker" in engines:
        (root / "rpgmaker").mkdir()
        walk = [f for f, nm in zip(frames, frame_names) if nm.startswith("walk_")][:3]
        while len(walk) < 3:
            walk.append(frames[0])
        rows = []
        for direction in range(4):
            rows.append([png.transform(f, cell, cell, flip_h=(direction == 1))
                         for f in walk])
        flat = [f for row in rows for f in row]
        rm = png.sheet(flat, 3, cell, cell)
        (root / "rpgmaker" / ("!$" + safe + ".png")).write_bytes(png.encode(rm))

    (root / "preview.html").write_text(
        _preview(sheet_w, sheet_h, cell, cols, tags, fps, safe), encoding="utf-8")
    (root / "README.txt").write_text(
        _readme(safe, cell, fps, actions, engines), encoding="utf-8")

    zip_path = cfg["local"] / "packs" / (pack_id + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=str(f.relative_to(root)))

    data = {
        "id": pack_id,
        "asset_id": asset.get("id"), "asset_file": rel,
        "actions": actions, "frames": len(frames),
        "frames_per_action": frames_per_action, "cell": cell, "cols": cols,
        "fps": fps, "bg": bg, "tolerance": tolerance, "engines": engines,
        "sheet_size": [sheet_w, sheet_h],
        "preview": f"/pack/{pack_id}/preview.html",
        "sheet": f"/pack/{pack_id}/sheet.png",
        "download": f"/api/packs/{pack_id}/zip",
        "zip": str(zip_path), "dir": str(root),
    }
    characters.add_pack(cfg, "spritesheet", safe, str(zip_path), data, pid=pack_id)
    return data
