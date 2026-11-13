"""Utilitarios compartilhados do projeto PUC."""

from .openai_compat import (
    OpenAICompatibleError,
    OpenAIConfig,
    OpenAIConfigError,
    create_chat_completion,
    parse_json_object,
)

__all__ = [
    "OpenAICompatibleError",
    "OpenAIConfig",
    "OpenAIConfigError",
    "create_chat_completion",
    "parse_json_object",
]
