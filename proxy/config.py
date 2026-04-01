"""Configuration for the Anthropic-to-OpenAI proxy."""

import os

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "local-key")
PROXY_PORT = int(os.getenv("PROXY_PORT", "9000"))
PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")

# Map Anthropic model names to vLLM model names
# The vLLM model name must match what was passed to `vllm serve`
MODEL_MAPPING: dict[str, str] = {
    "claude-opus-4-6": os.getenv("VLLM_MODEL", "Qwen/Qwen3-235B-A22B"),
    "claude-sonnet-4-6": os.getenv("VLLM_MODEL", "Qwen/Qwen3-235B-A22B"),
    "claude-haiku-4-5": os.getenv("VLLM_MODEL", "Qwen/Qwen3-235B-A22B"),
}

# Fallback: if model not in mapping, pass through as-is
def resolve_model(anthropic_model: str) -> str:
    return MODEL_MAPPING.get(anthropic_model, anthropic_model)
