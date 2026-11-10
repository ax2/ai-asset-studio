"""Configuration. Secrets live in memory, in an encrypted user-scoped file, or
in the environment - never in the repository."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import credentials

ROOT = Path(__file__).resolve().parent.parent

_SESSION_KEY: str = ""

DEFAULTS = {
    "asset_root": "D:/gdrive.photo/ai-assets",
    "local_root": "data",
    "host": "127.0.0.1",
    "port": 8777,
    "monthly_budget_usd": 100,
    "image_budget_usd": 60,
    "default_model": "gpt-image-1.5",
    "default_quality": "low",
    "default_size": "1024x1024",
    "batch_discount": 0.5,
    "api_key_env": "OPENAI_API_KEY",
    "api_base": "https://api.openai.com/v1",
    "api_proxy": "",
    "sync_include_demo": False,
}

ENV_OVERRIDES = {
    "AI_STUDIO_ASSET_ROOT": ("asset_root", str),
    "AI_STUDIO_HOST": ("host", str),
    "AI_STUDIO_PORT": ("port", int),
    "AI_STUDIO_MONTHLY_BUDGET": ("monthly_budget_usd", float),
    "AI_STUDIO_API_PROXY": ("api_proxy", str),
    "AI_STUDIO_API_BASE": ("api_base", str),
}

_PUBLIC_KEYS = (
    "asset_root", "local_root", "host", "port", "monthly_budget_usd",
    "image_budget_usd", "default_model", "default_quality", "default_size",
    "batch_discount", "api_key_env", "api_base", "api_proxy", "sync_include_demo",
)


def load() -> dict:
    cfg = dict(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"config.json is not valid JSON: {exc}") from exc

    for env_key, (cfg_key, caster) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw:
            try:
                cfg[cfg_key] = caster(raw)
            except ValueError:
                pass

    cfg["root"] = ROOT
    cfg["local"] = (ROOT / str(cfg["local_root"])).resolve()
    cfg["styles_dir"] = ROOT / "styles"
    cfg["web_dir"] = ROOT / "web"
    cfg["asset_root_path"] = Path(str(cfg["asset_root"]))

    for sub in ("raw", "keepers", "export", "thumbs"):
        (cfg["local"] / sub).mkdir(parents=True, exist_ok=True)

    # Local import avoids a cycle: store receives cfg but never imports config.
    from . import store
    store.ensure(cfg)
    return cfg


def public(cfg: dict) -> dict:
    """Safe subset for the browser - never contains credentials or Path objects."""
    out = {k: cfg.get(k) for k in _PUBLIC_KEYS}
    out["api_key_set"] = has_key(cfg)
    out["api_key_source"] = key_source(cfg)
    out["asset_root_writable"] = asset_root_writable(cfg)
    cred = credentials.info()
    out["api_key_persisted"] = cred["persisted"]
    out["api_key_hint"] = cred["hint"]
    out["credential_path"] = cred["path"]
    out["credential_cipher"] = cred["cipher"]
    out["credential_saved_at"] = cred["saved_at"]
    return out


def set_session_key(value: str, persist: bool = False) -> dict:
    """Keep the key in memory; optionally seal it to the user's credential file."""
    global _SESSION_KEY
    _SESSION_KEY = str(value or "").strip()
    if persist and _SESSION_KEY:
        credentials.save(_SESSION_KEY)
    return credentials.info()


def clear_session_key() -> None:
    global _SESSION_KEY
    _SESSION_KEY = ""


def persist_current_key(cfg: dict) -> dict:
    """Persist whichever key is currently active without exposing it again."""
    key = api_key(cfg)
    if not key:
        raise ValueError("当前没有可用的 API key，请先粘贴会话密钥或设置环境变量。")
    return credentials.save(key)


def clear_persisted_key() -> dict:
    return credentials.clear()


def key_source(cfg: dict) -> str:
    if _SESSION_KEY:
        return "session"
    if credentials.load():
        return "disk"
    name = str(cfg.get("api_key_env") or "OPENAI_API_KEY")
    return "env" if os.environ.get(name, "").strip() else "none"


def api_key(cfg: dict) -> str:
    if _SESSION_KEY:
        return _SESSION_KEY
    persisted = credentials.load()
    if persisted:
        return persisted
    name = str(cfg.get("api_key_env") or "OPENAI_API_KEY")
    return os.environ.get(name, "").strip()


def has_key(cfg: dict) -> bool:
    return bool(api_key(cfg))


_RUNTIME_KEYS = ("api_proxy", "api_base")


def apply_runtime(cfg: dict, patch: dict) -> list:
    """Change non-secret network settings for the current process only.

    Nothing is written to config.json, so a restart restores the file's values.
    The API key deliberately cannot be set this way.
    """
    changed = []
    for key in _RUNTIME_KEYS:
        if key not in patch:
            continue
        value = str(patch[key] or "").strip()
        if key == "api_proxy" and value and not value.startswith(
                ("http://", "https://", "socks5://", "socks5h://")):
            raise ValueError("api_proxy 必须以 http:// / https:// / socks5:// 开头")
        if key == "api_base" and value and not value.startswith(("http://", "https://")):
            raise ValueError("api_base 必须是 http(s):// 开头的完整地址")
        cfg[key] = value.rstrip("/") if key == "api_base" else value
        changed.append(key)
    return changed


_PROBE_CACHE: dict[str, bool] = {}


def asset_root_writable(cfg: dict) -> bool:
    """Probe by actually creating a folder - virtual drives may report writable
    at the OS level while rejecting every create call."""
    key = str(cfg["asset_root_path"])
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    ok = False
    try:
        root = Path(key)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / (".probe-" + str(os.getpid()))
        probe.mkdir()
        probe.rmdir()
        ok = True
    except OSError:
        ok = False
    _PROBE_CACHE[key] = ok
    return ok


def refresh_asset_root(cfg: dict, ok: bool = True) -> None:
    _PROBE_CACHE[str(cfg["asset_root_path"])] = bool(ok)
