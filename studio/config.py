"""Configuration. The API key lives only in an environment variable, never on disk."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
    "sync_include_demo": False,
}

ENV_OVERRIDES = {
    "AI_STUDIO_ASSET_ROOT": ("asset_root", str),
    "AI_STUDIO_HOST": ("host", str),
    "AI_STUDIO_PORT": ("port", int),
    "AI_STUDIO_MONTHLY_BUDGET": ("monthly_budget_usd", float),
}

_PUBLIC_KEYS = (
    "asset_root", "local_root", "host", "port", "monthly_budget_usd",
    "image_budget_usd", "default_model", "default_quality", "default_size",
    "batch_discount", "api_key_env", "sync_include_demo",
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

    for sub in ("raw", "keepers", "export"):
        (cfg["local"] / sub).mkdir(parents=True, exist_ok=True)
    return cfg


def public(cfg: dict) -> dict:
    """Safe subset for the browser - never contains credentials or Path objects."""
    out = {k: cfg.get(k) for k in _PUBLIC_KEYS}
    out["api_key_set"] = has_key(cfg)
    out["asset_root_writable"] = asset_root_writable(cfg)
    return out


def api_key(cfg: dict) -> str:
    name = str(cfg.get("api_key_env") or "OPENAI_API_KEY")
    return os.environ.get(name, "").strip()


def has_key(cfg: dict) -> bool:
    return bool(api_key(cfg))


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
