"""Prompt assembly, image generation, and asset persistence.

The API key is read from the environment on every call. It is never written to
a file, never logged, and never sent back to the browser.
"""
from __future__ import annotations

import base64
import html
import http.client
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import config, pricing, store, thumbs

API_URL = "https://api.openai.com/v1/images/generations"

# A credit error must stop the WHOLE queue, not just this image. SystemExit is
# a BaseException, so neither the panel generator's
# `except (RuntimeError, OSError, ValueError)` (which degrades to text-only)
# nor the series controller's `except Exception` (which continues to the next
# panel) can swallow it. A shared sentinel also stops sibling workers.
HALT_FLAG = Path(__file__).resolve().parent.parent / "data" / "queue-halt.flag"


class InsufficientBalanceError(SystemExit):
    """Account credit exhausted - abort generation instead of retrying."""


BALANCE_MARKERS = (
    "insufficient_quota", "insufficient balance", "insufficient credit",
    "insufficient funds", "out of credit", "out of balance", "no credit",
    "credit balance", "billing hard limit", "payment required",
    "exceeded your current quota", "quota exceeded",
    "余额不足", "余额不够", "欠费", "额度已用尽", "余额为零",
)


def is_balance_error(code: int, detail: str) -> bool:
    text = (detail or "").lower()
    return code == 402 or any(m in text for m in BALANCE_MARKERS)


def halt_on_no_balance(detail: str) -> None:
    """Write the shared stop sentinel, then abort this process immediately."""
    try:
        HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        HALT_FLAG.write_text(json.dumps({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": detail[:400]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        pass
    raise InsufficientBalanceError("INSUFFICIENT_BALANCE: " + detail[:300])


def check_halt_flag() -> None:
    """Every paid call re-checks the sentinel so sibling workers stop too.

    Offline self-tests mock the network layer; they opt out with
    AI_STUDIO_IGNORE_HALT=1 so a real credit stop cannot break `selftest.py`.
    """
    if os.environ.get("AI_STUDIO_IGNORE_HALT") == "1":
        return
    if HALT_FLAG.is_file():
        raise InsufficientBalanceError(
            "INSUFFICIENT_BALANCE: 已有余额熔断标记 " + str(HALT_FLAG))


def endpoint(cfg: dict) -> str:
    """Configurable so a compatible gateway can be used later."""
    base = str(cfg.get("api_base") or "https://api.openai.com/v1").rstrip("/")
    return base + "/images/generations"


def edit_endpoint(cfg: dict) -> str:
    base = str(cfg.get("api_base") or "https://api.openai.com/v1").rstrip("/")
    return base + "/images/edits"


def _multipart(fields: dict, files: list) -> tuple[bytes, str]:
    """Build multipart/form-data without third-party packages."""
    boundary = "----aiAssetStudio" + uuid.uuid4().hex
    out = []
    for key, value in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, key, value)).encode("utf-8"))
    for name, filename, data in files:
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                    "filename=\"%s\"\r\nContent-Type: image/png\r\n\r\n"
                    % (boundary, name, filename)).encode("utf-8"))
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out), "multipart/form-data; boundary=" + boundary


def _opener(cfg: dict):
    """Route API calls through an explicitly configured proxy when provided."""
    proxy = str(cfg.get("api_proxy") or "").strip()
    if not proxy:
        proxy = os.environ.get("AI_STUDIO_API_PROXY", "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


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
    check_halt_flag()
    key = config.api_key(cfg)
    if not key:
        raise RuntimeError(
            "尚未设置 API key。到「连接设置」粘贴会话密钥，或设置环境变量 OPENAI_API_KEY。")
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
    }
    req = urllib.request.Request(
        endpoint(cfg),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = _opener(cfg)
    body = None
    for attempt in range(3):
        try:
            with opener.open(req, timeout=600) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            if is_balance_error(exc.code, detail):
                halt_on_no_balance(f"HTTP {exc.code}: {detail}")
            if exc.code == 401:
                raise RuntimeError(
                    "HTTP 401: API key 无效或已撤销，请到「连接设置」更新会话密钥。") from None
            raise RuntimeError(f"OpenAI API returned HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # local proxies drop long TLS tunnels
                continue
            raise RuntimeError(
                f"无法连接 OpenAI API：{exc.reason}。请检查 config.json 里的 api_proxy。") from None
        except (OSError, http.client.HTTPException) as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"图像生成请求超时或连接中断：{exc}。可重跑批量脚本，已完成的图会自动跳过。") from None
    if body is None:
        raise RuntimeError("图像生成请求未返回响应；可重跑批量脚本，已完成的图会自动跳过。")

    try:
        item = body["data"][0]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Unexpected API response: no image data") from None

    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), "png"
    if item.get("url"):
        try:
            with opener.open(item["url"], timeout=120) as resp:
                return resp.read(), "png"
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not download generated image: {exc.reason}") from None
    raise RuntimeError("API response had neither b64_json nor url")


def check_connection(cfg: dict) -> dict:
    """Read-only probe: GET /models through the configured proxy.

    Never spends money and never echoes the key back in any field.
    """
    key = config.api_key(cfg)
    base = str(cfg.get("api_base") or "https://api.openai.com/v1").rstrip("/")
    proxy = str(cfg.get("api_proxy") or os.environ.get("AI_STUDIO_API_PROXY", "") or "").strip()
    out = {
        "ok": False,
        "http_status": 0,
        "endpoint": base + "/models",
        "proxy": proxy or "(system default)",
        "key_source": config.key_source(cfg),
        "model_count": 0,
        "message": "",
    }
    if not key:
        out["message"] = "尚未设置 API key。在「连接设置」里粘贴会话密钥，或设置环境变量 OPENAI_API_KEY。"
        return out

    req = urllib.request.Request(
        out["endpoint"],
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with _opener(cfg).open(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        out["http_status"] = 200
        out["ok"] = True
        out["model_count"] = len(payload.get("data") or [])
        out["message"] = "连接正常，API key 有效，可以取消 Dry run 生成真实图片。"
    except urllib.error.HTTPError as exc:
        out["http_status"] = exc.code
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 401:
            out["message"] = "HTTP 401：API key 无效或已撤销，请更新后重试。"
        elif exc.code == 403:
            out["message"] = f"HTTP 403：该 key 无权限或地区受限。{detail}"
        elif exc.code == 429:
            out["message"] = "HTTP 429：额度用尽或触发限流，请稍后再试。"
        else:
            out["message"] = f"HTTP {exc.code}：{detail}"
    except urllib.error.URLError as exc:
        out["message"] = f"网络不通：{exc.reason}。检查代理软件是否运行、代理地址是否正确。"
    except (ValueError, OSError) as exc:
        out["message"] = f"连接异常：{exc}"
    return out


def call_openai_edit(cfg: dict, prompt: str, images: list[bytes], *,
                     model: str | None = None, quality: str | None = None,
                     size: str | None = None, background: str = "transparent",
                     input_fidelity: str = "high", timeout: int = 600) -> bytes:
    """Generate an image guided by reference images of an existing character.

    ``input_fidelity`` is the API's lever for matching facial features and art
    style on models that support it. Several optional knobs (input_fidelity,
    background, output_format) are rejected with HTTP 400 by newer models such
    as gpt-image-2; on such an error we drop exactly the parameter the API
    names and retry. More than one reference image is sent with the ``image[]``
    array syntax (repeated plain ``image`` fields raise duplicate_parameter on
    gpt-image-2.6-sunburst); if a model rejects that syntax we fall back to
    repeated ``image`` fields once. GPT Image models always return b64_json.
    """
    check_halt_flag()
    key = config.api_key(cfg)
    if not key:
        raise RuntimeError("尚未设置 API key，无法使用参考图生成。")
    if not images:
        raise ValueError("参考图生成至少需要一张图片。")
    model = model or str(cfg.get("edit_model") or cfg["default_model"])
    quality = quality or str(cfg.get("default_quality") or "low")
    size = size or str(cfg.get("default_size") or "1024x1024")
    fields = {
        "model": model, "prompt": prompt, "n": "1", "size": size,
        "quality": quality, "background": background,
        "output_format": "png", "input_fidelity": input_fidelity,
    }
    raw_files = [(f"reference_{i}.png", data)
                 for i, data in enumerate(images[:16])]
    opener = _opener(cfg)
    payload = None
    current = dict(fields)
    optional = ("input_fidelity", "background", "output_format")
    network_retries = 0
    # Newer models require image[] for multiple references; older ones accept
    # repeated `image`. Start with the array syntax and negotiate on HTTP 400.
    array_mode = len(raw_files) > 1
    array_fallback_done = False
    for _attempt in range(len(optional) + 8):
        image_field = "image[]" if array_mode else "image"
        files = [(image_field, name, data) for name, data in raw_files]
        body, ctype = _multipart(current, files)
        req = urllib.request.Request(
            edit_endpoint(cfg), data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": ctype},
            method="POST")
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if is_balance_error(exc.code, detail):
                halt_on_no_balance(f"HTTP {exc.code}: {detail}")
            # duplicate_parameter means repeated `image` fields were sent;
            # the API itself asks for the image[] array syntax.
            if exc.code == 400 and "duplicate_parameter" in detail:
                if not array_mode:
                    array_mode = True
                    continue
            # A model that does not understand image[] gets one plain retry.
            if (exc.code == 400 and array_mode and not array_fallback_done
                    and "image[]" in detail
                    and "duplicate_parameter" not in detail
                    and "array syntax" not in detail):
                array_mode = False
                array_fallback_done = True
                continue
            drop = next((k for k in optional
                         if k in current and k in detail), "")
            if exc.code == 400 and drop:
                current.pop(drop)  # model rejects this knob; retry without it
                continue
            if exc.code == 401:
                raise RuntimeError("HTTP 401: API key 无效或已撤销。") from None
            raise RuntimeError(f"参考图生成失败 HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if network_retries < 2:
                network_retries += 1
                time.sleep(2 * network_retries)
                continue  # transient proxy reset before the API answered
            raise RuntimeError(f"无法连接参考图接口：{exc.reason}。检查 api_proxy。") from None
        except (OSError, http.client.HTTPException) as exc:
            if network_retries < 2:
                network_retries += 1
                time.sleep(2 * network_retries)
                continue
            raise RuntimeError(f"参考图生成超时或连接中断：{exc}") from None
    if payload is None:
        raise RuntimeError("参考图接口未返回响应")
    try:
        item = payload["data"][0]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("参考图接口返回中没有图片数据") from None
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with opener.open(item["url"], timeout=120) as resp:
            return resp.read()
    raise RuntimeError("参考图接口既没有 b64_json 也没有 url")


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
             asset_kind: str = "", note: str = "", character_id: str = "",
             anchor_version: int = 0) -> dict:
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
    if ext == "png":
        try:
            thumbs.ensure(cfg, f"raw/{day}/{filename}")
        except Exception:
            pass  # a thumbnail is only an optimisation; never fail the asset

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
        "character_id": character_id,
        "anchor_version": anchor_version,
        "metadata": check_metadata(folder / filename),
        "created_at": store.now(),
    }
    store.add(cfg, record)
    return {"asset": record, "estimate_usd": cost, "demo": demo, "prompt": final_prompt}
