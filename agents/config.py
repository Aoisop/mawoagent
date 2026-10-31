"""Shared startup configuration, independent of CLI and web presentation."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def load_env_file() -> None:
    """Load `.env` from the working directory and nowhere else.

    ``find_dotenv`` walks upwards to the filesystem root even with ``usecwd``,
    which meant a repository could ship a `.env` one level above the directory
    the user ran from and redirect the API base URL while the real key was still
    sent to it. Configuration must come from the directory you chose to run in.
    """

    env_path = Path.cwd() / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def _is_anthropic_compatible_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    path = (parsed.path or "").lower().rstrip("/")
    return (
        parsed.hostname == "api.anthropic.com"
        or path.endswith("/anthropic")
        or "/anthropic/" in path
    )


def resolve_api_config(cli_api_base: str | None = None) -> tuple[str | None, str | None, bool]:
    """Resolve the base URL, key, and whether to use the OpenAI protocol.

    CLI and generic URL overrides take priority over provider-specific URLs.
    Provider-specific URL variables select their protocol even for proxy URLs.
    """
    generic_api_key = _clean_env(os.environ.get("APIKEY"))
    openai_api_key = _clean_env(os.environ.get("OPENAI_API_KEY"))
    anthropic_api_key = _clean_env(os.environ.get("ANTHROPIC_API_KEY"))

    generic_api_base = _clean_env(os.environ.get("API"))
    openai_api_base = _clean_env(os.environ.get("OPENAI_BASE_URL"))
    anthropic_api_base = _clean_env(os.environ.get("ANTHROPIC_BASE_URL"))
    resolved_api_base = _clean_env(cli_api_base) or generic_api_base

    if resolved_api_base:
        if _is_anthropic_compatible_base_url(resolved_api_base):
            return resolved_api_base, generic_api_key or anthropic_api_key or openai_api_key, False
        return resolved_api_base, generic_api_key or openai_api_key or anthropic_api_key, True

    if openai_api_base:
        return openai_api_base, generic_api_key or openai_api_key or anthropic_api_key, True
    if anthropic_api_base or anthropic_api_key:
        return anthropic_api_base, generic_api_key or anthropic_api_key or openai_api_key, False
    if openai_api_key:
        return None, generic_api_key or openai_api_key, True
    return None, generic_api_key, False


def _env_number(name: str, default: float, cast=float):
    """Read a numeric setting, naming the variable when it cannot be parsed."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def resolve_agent_options(
    *,
    api_config: tuple[str | None, str | None, bool] | None = None,
    model: str | None = None,
    input_price: float | None = None,
    output_price: float | None = None,
    context_window: int | None = None,
) -> dict:
    """Build common Agent options with explicit overrides before env defaults.

    Prices use ``None`` to distinguish an omitted override from a zero price.
    A zero context-window override retains the CLI's existing env fallback.
    """
    base, key, use_openai = resolve_api_config() if api_config is None else api_config
    return {
        "model": model or os.environ.get("MODEL") or "deepseek-chat",
        "api_key": key,
        "api_base": base if use_openai else None,
        "anthropic_base_url": None if use_openai else base,
        "api_protocol": "openai" if use_openai else "anthropic",
        "input_price_per_million": (
            input_price if input_price is not None
            else _env_number("MAWOAGENT_INPUT_PRICE_PER_MILLION", 3.0)
        ),
        "output_price_per_million": (
            output_price if output_price is not None
            else _env_number("MAWOAGENT_OUTPUT_PRICE_PER_MILLION", 15.0)
        ),
        "shutdown_timeout": _env_number("MAWOAGENT_SHUTDOWN_TIMEOUT", 5.0),
        "context_window": context_window or (
            _env_number("MAWOAGENT_CONTEXT_WINDOW", 0, int) or None
        ),
        "background_skill_concurrency": _env_number("MAWOAGENT_SKILL_CONCURRENCY", 2, int),
        "background_skill_queue_limit": _env_number("MAWOAGENT_SKILL_QUEUE_LIMIT", 16, int),
    }
