"""Per-image prices from the OpenAI image generation pricing page (checked 2026-11-09).

These are list prices for 1024x1024 output. Batch API halves text token cost and,
where supported for images, halves the effective price. Kept in one place so the
cost dashboard and the pre-flight estimate never disagree.
"""
from __future__ import annotations

IMAGE_PRICES = {
    "gpt-image-2": {"low": 0.006, "medium": 0.053, "high": 0.211},
    "gpt-image-1.5": {"low": 0.009, "medium": 0.034, "high": 0.133},
    "gpt-image-1": {"low": 0.011, "medium": 0.042, "high": 0.167},
    "gpt-image-1-mini": {"low": 0.005, "medium": 0.011, "high": 0.036},
}

TEXT_PRICES = {
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.4": {"input": 2.50, "output": 15.00},
}


def models() -> list:
    return list(IMAGE_PRICES)


def qualities(model: str) -> list:
    return list(IMAGE_PRICES.get(model, IMAGE_PRICES["gpt-image-1.5"]))


def image_price(model: str, quality: str) -> float:
    table = IMAGE_PRICES.get(model, IMAGE_PRICES["gpt-image-1.5"])
    return float(table.get(quality, table["low"]))


def estimate(model: str, quality: str, count: int, batch: bool = False) -> float:
    unit = image_price(model, quality)
    if batch:
        unit *= 0.5
    return round(unit * max(0, int(count)), 4)


def price_table() -> dict:
    return {"image": IMAGE_PRICES, "text": TEXT_PRICES, "batch_discount": 0.5}
