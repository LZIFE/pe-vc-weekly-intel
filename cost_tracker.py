"""
Track DeepSeek API usage (token counts, cache hits) and estimate costs.
Logs every API call to api_usage.jsonl for the dashboard to read.
"""

import json
import os
import time
import typing
from collections.abc import Callable
from datetime import datetime, timezone

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_usage.jsonl")

# DeepSeek 官方定价（¥/1M tokens）
# https://api-docs.deepseek.com/quick_start/pricing
PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {
        "input": 0.5,       # ¥0.5 / 1M input tokens
        "output": 2.0,      # ¥2 / 1M output tokens
        "cache_hit": 0.1,   # ¥0.1 / 1M cached input tokens (折扣)
    },
    "deepseek-reasoner": {
        "input": 4.0,       # ¥4 / 1M
        "output": 16.0,     # ¥16 / 1M
        "cache_hit": 1.0,   # ¥1 / 1M
    },
}

DEFAULT_PRICING = {"input": 1.0, "output": 4.0, "cache_hit": 0.25}
PRICING_CACHE: dict[str, dict[str, float]] = {}


def _pricing(model: str) -> dict[str, float]:
    """Return pricing dict for the given model, with caching."""
    if model not in PRICING_CACHE:
        # Try exact match, fallback to prefix match, then default
        base = model.split("/")[-1]  # handle "provider/model-name"
        PRICING_CACHE[model] = (
            PRICING.get(model)
            or next((v for k, v in PRICING.items() if k in base), None)
            or DEFAULT_PRICING
        )
    return PRICING_CACHE[model]


def log_api_call(
    model: str,
    usage: typing.Optional[dict],
    endpoint: str = "chat/completions",
    status: str = "success",
    error_msg: str = "",
) -> None:
    """Append one API call record to the JSONL log file."""
    usage = usage or {}
    prompt_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
    completion_tokens = (
        usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    )
    total_tokens = usage.get("total_tokens", 0) or (prompt_tokens + completion_tokens) or 0

    # Cache hit tokens (DeepSeek returns prompt_cache_hit_tokens in usage)
    cache_hit_tokens = usage.get("prompt_cache_hit_tokens", 0) or 0
    # Effective input tokens (non-cached portion)
    effective_input = max(0, prompt_tokens - cache_hit_tokens)

    pricing = _pricing(model)
    cost_input = effective_input * pricing["input"] / 1_000_000
    cost_cache = cache_hit_tokens * pricing["cache_hit"] / 1_000_000
    cost_output = completion_tokens * pricing["output"] / 1_000_000
    cost_total = cost_input + cost_cache + cost_output

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "endpoint": endpoint,
        "status": status,
        "error": error_msg,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "effective_input_tokens": effective_input,
        "cost_input": round(cost_input, 6),
        "cost_cache": round(cost_cache, 6),
        "cost_output": round(cost_output, 6),
        "cost_total": round(cost_total, 6),
    }

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # silently ignore write errors


# ── Aggregation helpers used by the dashboard ────────────────────────


def load_records() -> list[dict]:
    """Load all API call records from the JSONL log."""
    records: list[dict] = []
    if not os.path.isfile(LOG_FILE):
        return records
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return records
    return records


def compute_summary(records: list[dict]) -> dict:
    """Aggregate records into a summary dict for the dashboard."""
    total_calls = len(records)
    total_prompt = sum(r.get("prompt_tokens", 0) for r in records)
    total_completion = sum(r.get("completion_tokens", 0) for r in records)
    total_tokens = total_prompt + total_completion
    total_cache_hit = sum(r.get("cache_hit_tokens", 0) for r in records)
    total_cost = sum(r.get("cost_total", 0) for r in records)

    success_calls = sum(1 for r in records if r.get("status") == "success")
    error_calls = total_calls - success_calls

    cache_rate = (total_cache_hit / total_prompt * 100) if total_prompt > 0 else 0.0

    # Per-model breakdown
    models: dict[str, dict] = {}
    for r in records:
        m = r.get("model", "unknown")
        if m not in models:
            models[m] = {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit_tokens": 0,
                "cost": 0.0,
            }
        models[m]["calls"] += 1
        models[m]["prompt_tokens"] += r.get("prompt_tokens", 0)
        models[m]["completion_tokens"] += r.get("completion_tokens", 0)
        models[m]["cache_hit_tokens"] += r.get("cache_hit_tokens", 0)
        models[m]["cost"] += r.get("cost_total", 0)

    # Last 24h vs all-time
    now = datetime.now(timezone.utc)
    recent = [r for r in records if _parse_ts(r.get("timestamp", "")) and (now - _parse_ts(r.get("timestamp", ""))).total_seconds() < 86400]

    return {
        "total_calls": total_calls,
        "success_calls": success_calls,
        "error_calls": error_calls,
        "total_prompt": total_prompt,
        "total_completion": total_completion,
        "total_tokens": total_tokens,
        "total_cache_hit": total_cache_hit,
        "cache_rate": round(cache_rate, 1),
        "total_cost": round(total_cost, 4),
        "models": models,
        "recent_24h": len(recent),
        "recent_24h_cost": round(sum(r.get("cost_total", 0) for r in recent), 4),
    }


def _parse_ts(ts: str) -> typing.Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None