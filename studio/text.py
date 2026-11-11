#!/usr/bin/env python3
"""Text-model helpers for every non-image task.

GPT-6.1 Sol is the default text model. Story writing, character extraction and
prompt expansion all go through here, so endpoint, auth, proxy and JSON parsing
live in one place and each call can be recorded for reproducibility.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from . import config, imaging

DEFAULT_MODEL = "gpt-6.1-sol"


def endpoint(cfg: dict) -> str:
    base = str(cfg.get("api_base") or "https://api.openai.com/v1").rstrip("/")
    return base + "/chat/completions"


def chat(cfg: dict, messages: list[dict], *, model: str | None = None,
         max_tokens: int = 8000, timeout: int = 300) -> dict:
    """Send a chat/completions request and return the raw response dict."""
    key = config.api_key(cfg)
    if not key:
        raise RuntimeError("没有可用的 API key，无法调用文本模型。")
    payload: dict[str, Any] = {
        "model": model or str(cfg.get("text_model") or DEFAULT_MODEL),
        "messages": messages,
    }
    if max_tokens:
        payload["max_completion_tokens"] = max_tokens

    def send(body: dict) -> dict:
        req = urllib.request.Request(
            endpoint(cfg), data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            method="POST")
        with imaging._opener(cfg).open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return send(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code == 400 and "max_completion_tokens" in detail:
            payload.pop("max_completion_tokens", None)
            payload["max_tokens"] = max_tokens
            return send(payload)
        if exc.code == 401:
            raise RuntimeError("HTTP 401: API key 无效或已撤销。") from None
        raise RuntimeError(f"文本模型 HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接文本模型：{exc.reason}。检查 api_proxy。") from None


def chat_json(cfg: dict, prompt: str, *, system: str = "",
              model: str | None = None, max_tokens: int = 8000,
              timeout: int = 300) -> tuple[Any, dict]:
    """Chat, then parse a JSON object/array out of the reply.

    Returns ``(data, meta)`` so the caller can archive model/usage/finish
    reason next to the generated content.
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = chat(cfg, messages, model=model, max_tokens=max_tokens,
                timeout=timeout)
    choice = (body.get("choices") or [{}])[0]
    content = str((choice.get("message") or {}).get("content") or "")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not starts:
        raise ValueError("文本模型没有返回 JSON：" + content[:200])
    start = min(starts)
    closing = "}" if cleaned[start] == "{" else "]"
    end = cleaned.rfind(closing)
    if end <= start:
        raise ValueError("文本模型返回的 JSON 不完整：" + content[:200])
    data = json.loads(cleaned[start:end + 1])
    meta = {
        "model": body.get("model"),
        "usage": body.get("usage") or {},
        "raw_chars": len(content),
        "finish_reason": choice.get("finish_reason"),
    }
    return data, meta
