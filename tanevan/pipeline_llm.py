# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Routes memory-pipeline LLM calls to Anthropic or a local OpenAI-compatible API (LM Studio)
based on ~/tanevan-data/pipeline_config.json — same file the Tanevan UI edits.

Until this module existed, summarizer / extractor / updater always called Anthropic regardless of UI.
"""

import json
import os
import re
import sys

import anthropic
import requests


class PipelineStepError(Exception):
    """A memory-pipeline LLM step could not produce a usable result."""


def _step_failure(message, *, backend=None):
    print(f"✗ {message}")
    meta = {"error": message}
    if backend:
        meta["_backend"] = backend
    return None, meta


def is_nonretryable_step_error(message):
    """True when retrying the same LLM call cannot succeed (auth, missing key/model)."""
    text = (message or "").lower()
    needles = (
        "401",
        "403",
        "authentication_error",
        "api key is invalid",
        "invalid api key",
        "incorrect api key",
        "no anthropic api key",
        "no openrouter api key",
        "no openai api key",
        "no model configured",
    )
    return any(n in text for n in needles)

PIPELINE_CONFIG_FILE = os.path.join(
    os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data")),
    "pipeline_config.json",
)


def _love_refactored_root():
    env_home = (os.environ.get("LR_HOME") or "").strip()
    if env_home:
        return os.path.abspath(os.path.expanduser(env_home))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_lr_root = _love_refactored_root()
if _lr_root not in sys.path:
    sys.path.insert(0, _lr_root)
from lib.lr_settings import load_settings, settings_api_key


def _load_lr_global_provider():
    return (load_settings().get("provider") or "").strip().lower()


def _load_lr_api_key(section):
    """Read apiKey from data/settings.json (fallback when not in pipeline_config)."""
    return settings_api_key(section)


def _default_pipeline_provider():
    env_default = (os.environ.get("TANEVAN_DEFAULT_PIPELINE_PROVIDER", "") or "").strip().lower()
    if env_default in ("local", "anthropic", "openrouter", "openai", "hybrid"):
        return env_default
    lr_provider = _load_lr_global_provider()
    if lr_provider in ("lmstudio", "custom"):
        return "local"
    if lr_provider == "openrouter":
        return "openrouter"
    if lr_provider == "openai":
        return "openai"
    return "anthropic"


def load_pipeline_config():
    """Load pipeline config from disk, merged over empty defaults (UI / env overrides only)."""
    defaults = {
        "provider": _default_pipeline_provider(),
        "local": {"endpoint": "http://localhost:1234/v1", "model": ""},
        "anthropic": {
            "api_key": "",
            "summarizer_model": "",
            "extractor_model": "",
            "updater_model": "",
            "reflection_model": "",
            "brief_model": "",
        },
        "openai": {
            "api_key": "",
            "url": "https://api.openai.com",
            "summarizer_model": "",
            "extractor_model": "",
            "updater_model": "",
            "reflection_model": "",
            "brief_model": "",
        },
        "openrouter": {
            "api_key": "",
            "summarizer_model": "",
            "extractor_model": "",
            "updater_model": "",
            "reflection_model": "",
            "brief_model": "",
        },
        "steps": {
            "summarizer": "anthropic",
            "extractor": "anthropic",
            "updater": "anthropic",
            "reflection": "anthropic",
            "brief": "anthropic",
        },
    }
    try:
        if os.path.exists(PIPELINE_CONFIG_FILE):
            with open(PIPELINE_CONFIG_FILE) as f:
                saved = json.load(f)
            for key in defaults:
                if key in saved:
                    if isinstance(defaults[key], dict) and isinstance(saved[key], dict):
                        defaults[key].update(saved[key])
                    else:
                        defaults[key] = saved[key]
    except Exception as e:
        print(f"⚠️ pipeline_llm: could not load {PIPELINE_CONFIG_FILE}: {e}")
    return defaults


def _env_model_for_step(step_name):
    """Optional env override — only when explicitly set, never a code default."""
    env_map = {
        "summarizer": "TANEVAN_SUMMARIZER_MODEL",
        "extractor": "TANEVAN_EXTRACTOR_MODEL",
        "updater": "TANEVAN_UPDATER_MODEL",
        "reflection": "TANEVAN_REFLECTION_MODEL",
        "brief": "BRIEF_MODEL_NAME",
    }
    return (os.environ.get(env_map.get(step_name, ""), "") or "").strip()


def anthropic_model_for_step(step_name, config):
    ant = config.get("anthropic") or {}
    field_map = {
        "summarizer": "summarizer_model",
        "extractor": "extractor_model",
        "updater": "updater_model",
        "reflection": "reflection_model",
        "brief": "brief_model",
    }
    f = field_map.get(step_name, f"{step_name}_model")
    return (ant.get(f) or _env_model_for_step(step_name) or "").strip()


def effective_anthropic_key(config, api_key_override=None):
    if api_key_override and not str(api_key_override).startswith("sk-ant-..."):
        return api_key_override
    k = (config.get("anthropic") or {}).get("api_key") or ""
    if k and not k.startswith("sk-ant-..."):
        return k
    env = (os.environ.get("ANTHROPIC_API_KEY", "") or "").strip()
    if env:
        return env
    return _load_lr_api_key("anthropic")


def _missing_model_message(step_name, backend):
    return (
        f"No {backend} model configured for pipeline step '{step_name}'. "
        "Set it in Love Refactored → Settings → Memory Pipeline."
    )


def openrouter_model_for_step(step_name, config):
    o = config.get("openrouter") or {}
    field_map = {
        "summarizer": "summarizer_model",
        "extractor": "extractor_model",
        "updater": "updater_model",
        "reflection": "reflection_model",
        "brief": "brief_model",
    }
    return (o.get(field_map.get(step_name, f"{step_name}_model")) or "").strip()


def openai_model_for_step(step_name, config):
    o = config.get("openai") or {}
    field_map = {
        "summarizer": "summarizer_model",
        "extractor": "extractor_model",
        "updater": "updater_model",
        "reflection": "reflection_model",
        "brief": "brief_model",
    }
    return (o.get(field_map.get(step_name, f"{step_name}_model")) or "").strip()


def _model_for_backend_step(step_name, backend, config):
    """Resolve model id for a step on a specific backend; brief falls back to summarizer."""
    backend = (backend or "").lower()
    if backend == "local":
        _, model = local_endpoint_and_model(config)
        return model
    if backend == "openrouter":
        model = openrouter_model_for_step(step_name, config)
    elif backend == "openai":
        model = openai_model_for_step(step_name, config)
    else:
        model = anthropic_model_for_step(step_name, config)
    if not model and step_name == "brief":
        return _model_for_backend_step("summarizer", backend, config)
    return model


def brief_uses_env_override():
    """True when legacy BRIEF_MODEL_KEY env forces OpenAI-compatible direct routing."""
    return bool((os.environ.get("BRIEF_MODEL_KEY") or "").strip())


def brief_is_configured(config=None):
    """Whether the life-brief sidecar has enough LLM config to run."""
    if brief_uses_env_override():
        return True
    if config is None:
        config = load_pipeline_config()
    backend = resolve_backend("brief", config)
    if backend == "local":
        return True
    model = _model_for_backend_step("brief", backend, config)
    if backend == "openrouter":
        return bool(effective_openrouter_key(config)) and bool(model)
    if backend == "openai":
        return bool(effective_openai_key(config)) and bool(model)
    return bool(effective_anthropic_key(config)) and bool(model)


def effective_openai_key(config):
    k = (config.get("openai") or {}).get("api_key") or ""
    if k and not str(k).startswith("sk-..."):
        return k
    env = (os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if env:
        return env
    return _load_lr_api_key("openai")


def openai_base_url(config):
    o = config.get("openai") or {}
    url = (o.get("url") or "https://api.openai.com").strip().rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


def effective_openrouter_key(config):
    k = (config.get("openrouter") or {}).get("api_key") or ""
    if k and not k.startswith("sk-or-..."):
        return k
    env = (os.environ.get("OPENROUTER_API_KEY", "") or "").strip()
    if env:
        return env
    return _load_lr_api_key("openrouter")


def resolve_backend(step_name, config=None):
    """Return 'anthropic', 'openai', 'openrouter', or 'local' for this pipeline step."""
    if config is None:
        config = load_pipeline_config()
    top = (config.get("provider") or _default_pipeline_provider()).lower()
    steps = config.get("steps") or {}
    if top == "hybrid":
        return (steps.get(step_name) or "anthropic").lower()
    if top == "local":
        return "local"
    if top == "openrouter":
        return "openrouter"
    if top == "openai":
        return "openai"
    return "anthropic"


def local_endpoint_and_model(config):
    loc = config.get("local") or {}
    ep = (loc.get("endpoint") or "http://localhost:1234/v1").strip().rstrip("/")
    model = (loc.get("model") or "").strip()
    return ep, model


def openai_compatible_chat(endpoint_v1, model, system_prompt, user_content, max_tokens, temperature=0.3):
    """POST /v1/chat/completions (LM Studio, Ollama OpenAI shim, etc.)."""
    url = endpoint_v1.rstrip("/") + "/chat/completions"
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model:
        body["model"] = model
    r = requests.post(url, json=body, timeout=600)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    inp = int(usage.get("prompt_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    return text.strip(), inp, out


def anthropic_response_text(response):
    """Join visible text blocks; skip thinking / redacted_thinking."""
    parts = []
    for block in (getattr(response, "content", None) or []):
        btype = getattr(block, "type", None)
        if btype in ("thinking", "redacted_thinking"):
            continue
        if btype == "text" or getattr(block, "text", None) is not None:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
    return "".join(parts).strip()


def anthropic_thinking_pin(model):
    """thinking_pin_v1: mirrors anthropicThinkingPin in server.js. 5-family models default
    thinking ON. Older 5s accept disabled; opus-5-5/fable/mythos are always-on, so send
    adaptive + effort low. Returns a dict to pass as extra_body."""
    import re as _re
    m = str(model or "")
    if not _re.search(r"(sonnet|opus|haiku|fable|mythos)-5\b", m):
        return {}
    if _re.search(r"opus-5-5|fable|mythos", m):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
    return {"thinking": {"type": "disabled"}}
def anthropic_chat(api_key, model, system_prompt, user_content, max_tokens, cache_system=False):
    client = anthropic.Anthropic(api_key=api_key)
    system_blocks = (
        [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        if cache_system
        else [{"type": "text", "text": system_prompt}]
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
        extra_body=anthropic_thinking_pin(model),  # thinking_pin_v1
    )
    raw_text = anthropic_response_text(response)
    inp = response.usage.input_tokens
    out = response.usage.output_tokens
    MODEL_PRICING = {
        'claude-opus-4-6': (5.0, 25.0),
        'claude-opus-4-5-20251101': (5.0, 25.0),
        'claude-sonnet-4-6': (3.0, 15.0),
        'claude-sonnet-4-20250514': (3.0, 15.0),
        'claude-haiku-4-5-20251001': (0.80, 4.0),
    }
    inp_rate, out_rate = MODEL_PRICING.get(model, (5.0, 25.0))
    cost = (inp * inp_rate / 1_000_000) + (out * out_rate / 1_000_000)
    return raw_text, inp, out, round(cost, 4)


def openai_api_chat(api_key, endpoint_v1, model, system_prompt, user_content, max_tokens, temperature=0.3):
    """POST /v1/chat/completions on OpenAI (or compatible base URL)."""
    url = endpoint_v1.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    r = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json=body,
        timeout=600,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    inp = int(usage.get("prompt_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    return text.strip(), inp, out


def openrouter_chat(api_key, model, system_prompt, user_content, max_tokens, temperature=0.3, reasoning=None):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning is not None:
        body["reasoning"] = reasoning
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "Love Refactored",
        },
        json=body,
        timeout=600,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return text.strip(), inp, out


def run_pipeline_step(
    step_name,
    api_key_override,
    system_prompt,
    user_content,
    *,
    max_tokens=4096,
    temperature=0.3,
    cache_system=False,
    reasoning=None,
):
    """
    Run one pipeline step on Anthropic, OpenAI, OpenRouter, or local OpenAI-compatible API.

    Returns:
        (raw_text: str | None, meta: dict | None)
        meta includes _input_tokens, _output_tokens, _cost, _backend
    """
    config = load_pipeline_config()
    backend = resolve_backend(step_name, config)

    if backend == "local":
        ep, model = local_endpoint_and_model(config)
        model_label = model or "(server default)"
        try:
            print(f"🖥️  {step_name}: local OpenAI-compatible API → {ep} (model={model_label})")
            raw, inp, out = openai_compatible_chat(
                ep, model, system_prompt, user_content, max_tokens, temperature=temperature
            )
            return raw, {
                "_input_tokens": inp,
                "_output_tokens": out,
                "_cost": 0.0,
                "_backend": "local",
            }
        except Exception as e:
            return _step_failure(f"Local LLM error ({step_name}): {e}", backend="local")

    if backend == "openrouter":
        key = effective_openrouter_key(config)
        if not key:
            return _step_failure(
                "No OpenRouter API key for pipeline (settings / OPENROUTER_API_KEY / pipeline_config).",
                backend="openrouter",
            )
        model = _model_for_backend_step(step_name, backend, config)
        if not model:
            return _step_failure(_missing_model_message(step_name, "OpenRouter"), backend="openrouter")
        try:
            print(f"☁️  {step_name}: OpenRouter → {model}")
            raw_text, inp, out = openrouter_chat(
                key, model, system_prompt, user_content, max_tokens,
                temperature=temperature, reasoning=reasoning,
            )
            return raw_text, {
                "_input_tokens": inp,
                "_output_tokens": out,
                "_cost": 0.0,
                "_backend": "openrouter",
            }
        except Exception as e:
            return _step_failure(f"OpenRouter error ({step_name}): {e}", backend="openrouter")

    if backend == "openai":
        key = effective_openai_key(config)
        if not key:
            return _step_failure(
                "No OpenAI API key for pipeline (settings / OPENAI_API_KEY / pipeline_config).",
                backend="openai",
            )
        model = _model_for_backend_step(step_name, backend, config)
        if not model:
            return _step_failure(_missing_model_message(step_name, "OpenAI"), backend="openai")
        base = openai_base_url(config)
        try:
            print(f"☁️  {step_name}: OpenAI → {model}")
            raw_text, inp, out = openai_api_chat(
                key, base, model, system_prompt, user_content, max_tokens, temperature=temperature
            )
            return raw_text, {
                "_input_tokens": inp,
                "_output_tokens": out,
                "_cost": 0.0,
                "_backend": "openai",
            }
        except Exception as e:
            return _step_failure(f"OpenAI error ({step_name}): {e}", backend="openai")

    key = effective_anthropic_key(config, api_key_override)
    if not key:
        return _step_failure(
            "No Anthropic API key for pipeline (settings / env / pipeline_config).",
            backend="anthropic",
        )

    model = _model_for_backend_step(step_name, backend, config)
    if not model:
        return _step_failure(_missing_model_message(step_name, "Anthropic"), backend="anthropic")

    try:
        print(f"☁️  {step_name}: Anthropic → {model}")
        raw_text, inp, out, cost = anthropic_chat(
            key, model, system_prompt, user_content, max_tokens, cache_system=cache_system
        )
        return raw_text, {
            "_input_tokens": inp,
            "_output_tokens": out,
            "_cost": cost,
            "_backend": "anthropic",
        }
    except anthropic.APIError as e:
        return _step_failure(f"Anthropic API error ({step_name}): {e}", backend="anthropic")
    except Exception as e:
        return _step_failure(f"Anthropic error ({step_name}): {e}", backend="anthropic")


def strip_json_fences(raw_text):
    t = raw_text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, count=1)
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    return t.strip()


# =============================================================================
# Life-brief step (used by tanevan/brief_sidecar.py)
# =============================================================================
def _brief_env_reasoning():
    _rz = (os.environ.get("BRIEF_REASONING", "off") or "").strip().lower()
    if _rz in ("off", "none", "false", "0", "no", "disabled"):
        return {"enabled": False}
    if _rz in ("low", "medium", "high", "xhigh"):
        return {"effort": _rz}
    return None


def _openai_compat_env_brief_chat(base, key, model, system_prompt, user_content, max_tokens, temperature, reasoning):
    """Legacy env-only brief path (BRIEF_MODEL_*)."""
    import json as _json
    import urllib.request

    b = base.rstrip("/")
    if b.endswith("/chat/completions"):
        url = b
    elif b.endswith("/v1"):
        url = b + "/chat/completions"
    else:
        url = b + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning
    body = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    with urllib.request.urlopen(req, timeout=600) as r:
        data = _json.loads(r.read().decode("utf-8"))
    choice = (data.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    finish = choice.get("finish_reason") or "stop"
    usage = data.get("usage") or {}
    return content, finish, usage


def run_brief_llm(system_prompt, user_content, *, max_tokens=6000, temperature=0.6, reasoning=None):
    """
    Run the life-brief sidecar LLM call.

    Uses BRIEF_MODEL_* env when set (legacy override), otherwise Settings -> Memory Pipeline
    brief step (same provider routing as summarizer/extractor/updater).

    Returns (content, finish_reason, usage_dict) or raises on failure.
    """
    if reasoning is None:
        reasoning = _brief_env_reasoning()

    env_key = (os.environ.get("BRIEF_MODEL_KEY") or "").strip()
    if env_key:
        base = os.environ.get("BRIEF_MODEL_BASE", "https://openrouter.ai/api/v1")
        model = os.environ.get("BRIEF_MODEL_NAME", "deepseek/deepseek-chat")
        return _openai_compat_env_brief_chat(
            base, env_key, model, system_prompt, user_content, max_tokens, temperature, reasoning
        )

    if not brief_is_configured():
        raise RuntimeError(
            "Life brief LLM is not configured. Set Brief model in Settings -> Memory Pipeline "
            "or provide BRIEF_MODEL_KEY."
        )

    raw_text, meta = run_pipeline_step(
        "brief",
        None,
        system_prompt,
        user_content,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning=reasoning,
    )
    if raw_text is None:
        raise RuntimeError("Life brief model call failed (see Tanevan logs).")
    usage = {
        "prompt_tokens": (meta or {}).get("_input_tokens"),
        "completion_tokens": (meta or {}).get("_output_tokens"),
    }
    return raw_text, "stop", usage
