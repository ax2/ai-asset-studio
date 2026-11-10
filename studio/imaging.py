"""Prompt assembly, image generation, and asset persistence.

The API key is read from the environment on every call. It is never written to
a file, never logged, and never sent back to the browser.
"""
from __future__ import annotations

import base64
import html
import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config, pricing, store

API_URL = "https://api.openai.com/v1/images/generations"


def load_styles(cfg: dict) -> list:
    out = []
    for path in sorted(cfg["styles_dir"].glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda s: s.get("order", 99))
    return out


def load_style(cfg: dict, style_id: str):
    for style in load_styles(cfg):
        if style.get("id") == style_id:
            return style
    return None


def save_style(cfg: dict, style_id: str, data: dict) -> dict:
    path = cfg["styles_dir"] / f"{style_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"unknown style: {style_id}")
    current = json.loads(path.read_text(encoding="utf-8"))
    for key in ("blocks", "slots", "negative", "notes", "status", "description",
                "model", "budget", "palette", "asset_kinds", "eval_rules"):
        if key in data:
            current[key] = data[key]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return current


def build_prompt(style: dict, *, block_overrides: dict | None = None,
                 slots: dict | None = None, extra: str = "") -> str:
    overrides = block_overrides or {}
    picked = slots or {}
    parts = []
    for block in style.get("blocks", []):
        key = block.get("key", "")
        text = (overrides.get(key) if key in overrides else block.get("en", "")) or ""
        text = text.strip().rstrip(".")
        if text:
            parts.append(text)

    slot_lines = []
    for key in ("subject", "asset_kind", "mood", "concept"):
        value = str(picked.get(key) or "").strip().rstrip(".")
        if value:
            slot_lines.append(f"{key}: {value}")
    if slot_lines:
        parts.append("; ".join(slot_lines))
    if (extra or "").strip():
        parts.append(extra.strip().rstrip("."))

    prompt = ", ".join(parts)
    negative = (style.get("negative") or "").strip()
    if negative:
        prompt += ". Avoid: " + negative
    return prompt


def eval_score(style: dict, prompt: str) -> dict:
    low = (prompt or "").lower()
    rules = style.get("eval_rules", [])
    hits, missing = [], []
    for rule in rules:
        needle = str(rule.get("needle", "")).lower()
        if needle and needle in low:
            hits.append(rule.get("key", ""))
        else:
            missing.append(rule.get("zh") or rule.get("key", ""))
    total = len(rules)
    return {
        "score": len(hits),
        "total": total,
        "percent": round(100 * len(hits) / total) if total else 100,
        "hits": hits,
        "missing": missing,
    }


def make_demo_svg(style: dict, prompt: str) -> bytes:
    """Local placeholder so the whole studio works with zero API spend."""
    palette = style.get("palette") or ["#888780", "#D3D1C7", "#444441"]
    swatches = ""
    for i, color in enumerate(palette[:5]):
        swatches += (f'<rect x="{72 + i * 180}" y="790" width="150" height="150" '
                     f'rx="12" fill="{html.escape(color)}"/>')
    summary = html.escape((prompt or "")[:150])
    name = html.escape(str(style.get("name", "style")))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">'
        '<rect width="1024" height="1024" fill="#20232A"/>'
        f'<text x="72" y="120" fill="#F1EFE8" font-family="sans-serif" font-size="54">{name}</text>'
        '<text x="72" y="180" fill="#9FE1CB" font-family="sans-serif" font-size="30">DRY RUN / DEMO ASSET</text>'
        '<rect x="72" y="240" width="880" height="480" rx="20" fill="#2C313A"/>'
        f'<text x="104" y="310" fill="#D3D1C7" font-family="monospace" font-size="24">{summary}</text>'
        f'{swatches}'
        '<text x="72" y="990" fill="#888780" font-family="sans-serif" font-size="22">'
        'No API call was made. Switch to a real run to generate an image.</text>'
        '</svg>'
    ).encode("utf-8")


def call_openai(cfg: dict, prompt: str, model: str, quality: str,
                size: str) -> tuple[bytes, str]:
    key = config.api_key(cfg)
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Set it in the environment before a real run.")
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"OpenAI API returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the OpenAI API: {exc.reason}") from None

    try:
        item = body["data"][0]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Unexpected API response: no image data") from None

    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), "png"
    if item.get("url"):
        try:
            with urllib.request.urlopen(item["url"], timeout=120) as resp:
                return resp.read(), "png"
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not download generated image: {exc.reason}") from None
    raise RuntimeError("API response had neither b64_json nor url")


def check_metadata(path: Path) -> str:
    if path.suffix.lower() == ".svg":
        return "demo"
    try:
        head = path.read_bytes()[:262144]
    except OSError:
        return "unreadable"
    low = head.lower()
    if b"c2pa" in low or b"content credentials" in low:
        return "c2pa"
    if b"text" in low or b"itxt" in low or b"exif" in low:
        return "metadata"
    return "none"


def generate(cfg: dict, *, style_id: str, prompt: str | None = None,
             block_overrides: dict | None = None, slots: dict | None = None,
             extra: str = "", model: str | None = None,
             quality: str | None = None, size: str | None = None,
             dry_run: bool = True, batch_estimate: bool = False,
             source: str = "generate", parent_id: str | None = None,
             asset_kind: str = "", note: str = "") -> dict:
    style = load_style(cfg, style_id)
    if not style:
        raise RuntimeError(f"unknown style: {style_id}")

    model = model or style.get("model", {}).get("default") or cfg["default_model"]
    quality = quality or style.get("model", {}).get("quality") or cfg["default_quality"]
    size = size or style.get("model", {}).get("size") or cfg["default_size"]
    final_prompt = (prompt or "").strip() or build_prompt(
        style, block_overrides=block_overrides, slots=slots, extra=extra)
    cost = pricing.estimate(model, quality, 1, batch=batch_estimate)

    if dry_run:
        data, ext, demo = make_demo_svg(style, final_prompt), "svg", True
    else:
        data, ext, demo = (*call_openai(cfg, final_prompt, model, quality, size), False)

    asset_id = store.new_id()
    day = store.now()[:10].replace("-", "")
    folder = cfg["local"] / "raw" / day
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{asset_id}.{ext}"
    (folder / filename).write_bytes(data)

    record = {
        "id": asset_id,
        "file": f"raw/{day}/{filename}",
        "filename": filename,
        "style_id": style_id,
        "style_name": style.get("name", style_id),
        "prompt": final_prompt,
        "blocks": block_overrides or {},
        "slots": slots or {},
        "extra": extra,
        "prompt_version": 1,
        "model": model,
        "quality": quality,
        "size": size,
        "cost_usd": 0.0 if demo else cost,
        "batch_estimate": bool(batch_estimate),
        "demo": demo,
        "source": source,
        "parent_id": parent_id,
        "asset_kind": asset_kind,
        "rating": 0,
        "tags": [],
        "favorite": False,
        "status": "raw",
        "note": note,
        "metadata": check_metadata(folder / filename),
        "created_at": store.now(),
    }
    store.add(cfg, record)
    return {"asset": record, "estimate_usd": cost, "demo": demo, "prompt": final_prompt}
