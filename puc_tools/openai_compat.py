"""Cliente HTTP minimo para endpoints OpenAI-compatible de Chat Completions."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


class OpenAIConfigError(ValueError):
    """Indica configuracao ausente ou invalida do endpoint de LLM."""


class OpenAICompatibleError(RuntimeError):
    """Indica falha de transporte ou resposta invalida da LLM."""


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _discover_model(
    base_url: str,
    chat_completions_url: Optional[str],
    api_key: Optional[str],
    timeout_seconds: float,
) -> str:
    """Seleciona o primeiro modelo anunciado pelo mesmo servidor da API de chat."""

    models_base = base_url.strip().rstrip("/")
    if models_base.lower().endswith("/chat/completions"):
        models_base = models_base[: -len("/chat/completions")]
    if not models_base:
        endpoint = (chat_completions_url or "").strip()
        if endpoint.lower().endswith("/chat/completions"):
            models_base = endpoint[: -len("/chat/completions")]
    if not models_base:
        raise OpenAIConfigError(
            "Defina OPENAI_MODEL quando a URL de chat nao permite localizar /models."
        )

    models_url = f"{models_base}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with request.urlopen(request.Request(models_url, headers=headers), timeout=timeout_seconds) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise OpenAIConfigError(
            f"Nao foi possivel consultar modelos em {_safe_url_for_error(models_url)}. "
            "Verifique a API local ou defina OPENAI_MODEL explicitamente."
        ) from exc

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        for item in payload["data"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                return item["id"].strip()
    raise OpenAIConfigError(
        f"Nenhum modelo disponivel em {_safe_url_for_error(models_url)}."
    )


@dataclass(frozen=True)
class OpenAIConfig:
    """Configuracao de um endpoint OpenAI-compatible.

    ``base_url`` segue a convencao dos clientes OpenAI: normalmente termina em
    ``/v1``. Tambem pode ser a URL completa de ``/chat/completions``. Quando
    ``chat_completions_url`` e informado, ele tem precedencia sobre ``base_url``.
    """

    base_url: str
    model: str
    api_key: Optional[str] = None
    timeout_seconds: float = 120.0
    chat_completions_url: Optional[str] = None
    provider: str = "openai"

    def __post_init__(self) -> None:
        base_url = (self.base_url or "").strip().rstrip("/")
        model = (self.model or "").strip()
        api_key = (self.api_key or "").strip() or None
        endpoint = (self.chat_completions_url or "").strip() or None
        provider = (self.provider or "openai").strip().lower()

        try:
            timeout_seconds = float(self.timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise OpenAIConfigError("OPENAI_TIMEOUT_SECONDS deve ser numerico.") from exc

        if not base_url and not endpoint:
            raise OpenAIConfigError(
                "Defina OPENAI_BASE_URL (ou LLM_BASE_URL/MARITACA_BASE_URL) "
                "ou OPENAI_CHAT_COMPLETIONS_URL."
            )
        if not model:
            raise OpenAIConfigError(
                "Defina OPENAI_MODEL (ou LLM_MODEL/MARITACA_MODEL)."
            )
        if timeout_seconds <= 0:
            raise OpenAIConfigError("OPENAI_TIMEOUT_SECONDS deve ser maior que zero.")

        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "chat_completions_url", endpoint)
        if provider not in {"openai", "anthropic", "google", "local", "compatible"}:
            raise OpenAIConfigError("OPENAI_PROVIDER deve ser openai, anthropic, google, local ou compatible.")
        object.__setattr__(self, "provider", provider)

    @classmethod
    def from_env(cls, default_timeout: float = 120.0) -> "OpenAIConfig":
        """Carrega configuracao generica, aliases LLM e aliases legados.

        Seleciona uma unica familia pela primeira que tiver base ou endpoint:
        OPENAI_* > LLM_* > MARITACA_*. Os demais campos sao lidos somente da
        familia selecionada, evitando enviar uma chave legada a outro endpoint.
        A chave e opcional para permitir servidores locais sem autenticacao.
        Sem MODEL, consulta /models e usa o primeiro ID anunciado.
        """

        families = (
            (
                "OPENAI",
                ("OPENAI_API_KEY",),
            ),
            (
                "LLM",
                ("LLM_API_KEY",),
            ),
            (
                "MARITACA",
                ("MARITACA_API_KEY", "TOKEN_MARITACA", "MARITACA_TOKEN"),
            ),
        )

        selected_prefix = "OPENAI"
        selected_key_names = ("OPENAI_API_KEY",)
        for prefix, key_names in families:
            if _first_env(
                f"{prefix}_BASE_URL",
                f"{prefix}_CHAT_COMPLETIONS_URL",
            ):
                selected_prefix = prefix
                selected_key_names = key_names
                break

        timeout_raw = _first_env(f"{selected_prefix}_TIMEOUT_SECONDS")
        timeout: Any = timeout_raw if timeout_raw is not None else default_timeout

        base_url = _first_env(f"{selected_prefix}_BASE_URL") or ""
        endpoint = _first_env(f"{selected_prefix}_CHAT_COMPLETIONS_URL")
        api_key = _first_env(*selected_key_names)
        model = _first_env(f"{selected_prefix}_MODEL")
        if not model and (base_url or endpoint):
            try:
                timeout_value = float(timeout)
            except (TypeError, ValueError) as exc:
                raise OpenAIConfigError("OPENAI_TIMEOUT_SECONDS deve ser numerico.") from exc
            if timeout_value <= 0:
                raise OpenAIConfigError("OPENAI_TIMEOUT_SECONDS deve ser maior que zero.")
            model = _discover_model(base_url, endpoint, api_key, timeout_value)

        provider = _first_env("OPENAI_PROVIDER")
        if not provider:
            configured_url = base_url or endpoint or ""
            try:
                configured_host = urlsplit(configured_url).hostname
            except ValueError:
                configured_host = None
            provider = "openai" if configured_host == "api.openai.com" else "compatible"

        return cls(
            base_url=base_url,
            model=model or "",
            api_key=api_key,
            timeout_seconds=timeout,
            chat_completions_url=endpoint,
            provider=provider,
        )

    @property
    def endpoint_url(self) -> str:
        """Retorna uma unica URL deterministica de Chat Completions."""

        if self.chat_completions_url:
            return self.chat_completions_url
        if self.base_url.lower().endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"


def _safe_url_for_error(url: str) -> str:
    """Remove query/fragment para nao vazar credenciais presentes na URL."""

    try:
        parts = urlsplit(url)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "endpoint configurado"


def _redact_secret(text: str, secret: Optional[str]) -> str:
    if secret:
        return text.replace(secret, "[REDACTED]")
    return text


def _extract_message_content(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        raise OpenAICompatibleError("Resposta da LLM nao e um objeto JSON.")

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise OpenAICompatibleError("Resposta da LLM sem choices.")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise OpenAICompatibleError("Primeiro choice da LLM e invalido.")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise OpenAICompatibleError("Resposta da LLM sem message no primeiro choice.")

    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            raise OpenAICompatibleError("Resposta da LLM com content vazio.")
        return content

    if isinstance(content, list):
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {None, "text", "output_text"}
            and isinstance(part.get("text"), str)
        ]
        joined = "\n".join(part for part in text_parts if part)
        if joined.strip():
            return joined

    raise OpenAICompatibleError("Resposta da LLM sem content textual.")


def create_chat_completion(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    temperature: Optional[float] = 0.2,
) -> str:
    """Executa uma requisicao nao-streaming de Chat Completions e retorna texto."""

    if not messages:
        raise ValueError("messages nao pode ser vazio.")

    if config.provider == "anthropic":
        return _create_anthropic_completion(config, messages, temperature)
    if config.provider == "google":
        return _create_gemini_completion(config, messages, temperature)
    if config.provider == "openai":
        return _create_openai_responses_completion(config, messages)

    payload: Dict[str, Any] = {
        "model": config.model,
        "messages": [dict(message) for message in messages],
    }
    if temperature is not None:
        payload["temperature"] = temperature

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    endpoint = config.endpoint_url
    req = request.Request(
        endpoint,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )

    try:
        with request.urlopen(req, timeout=config.timeout_seconds) as response:
            body = response.read().decode("utf-8-sig")
    except error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        finally:
            exc.close()
        detail = _redact_secret(detail, config.api_key)
        suffix = f" Detalhe: {detail[:500]}" if detail else ""
        raise OpenAICompatibleError(
            f"Falha em {_safe_url_for_error(endpoint)}: HTTP {exc.code}.{suffix}"
        ) from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise OpenAICompatibleError(
            f"Falha ao conectar em {_safe_url_for_error(endpoint)}: {reason}"
        ) from exc
    except TimeoutError as exc:
        raise OpenAICompatibleError(
            f"Timeout ao conectar em {_safe_url_for_error(endpoint)}."
        ) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenAICompatibleError("Resposta da LLM nao contem JSON valido.") from exc

    return _extract_message_content(parsed)


def _responses_text_and_calls(parsed: Dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    output = parsed.get("output")
    if not isinstance(output, list):
        raise OpenAICompatibleError("A resposta da API Responses não contém itens de saída.")
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls.append(item)
        elif item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, list):
                text_parts.extend(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
                )
    return "\n".join(part for part in text_parts if part).strip(), calls


def _openai_responses_payload(messages: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions = "\n\n".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") in {"system", "developer"}
    )
    transcript = [
        {
            "role": str(message.get("role")),
            "content": str(message.get("content") or ""),
        }
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    return instructions, transcript


def _create_openai_responses_completion(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
) -> str:
    instructions, transcript = _openai_responses_payload(messages)
    payload: Dict[str, Any] = {"model": config.model, "input": transcript, "max_output_tokens": 4096}
    if instructions:
        payload["instructions"] = instructions
    parsed = _post_json(
        _base_endpoint(config, "responses"),
        payload,
        {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
        config.timeout_seconds,
        config.api_key,
    )
    text, calls = _responses_text_and_calls(parsed)
    if calls or not text:
        raise OpenAICompatibleError("A API Responses não retornou texto para esta solicitação.")
    return text


def _base_endpoint(config: OpenAIConfig, suffix: str) -> str:
    base = config.base_url.strip().rstrip("/")
    if not base:
        endpoint = (config.chat_completions_url or "").strip().rstrip("/")
        base = endpoint.rsplit("/", 1)[0] if "/" in endpoint else endpoint
    if base.lower().endswith(suffix.lower()):
        return base
    return f"{base}/{suffix.lstrip('/')}"


def _post_json(
    endpoint: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
    api_key: Optional[str],
) -> Dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **headers,
    }
    req = request.Request(
        endpoint,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8-sig"))
    except error.HTTPError as exc:
        try:
            detail = _redact_secret(exc.read().decode("utf-8", errors="replace").strip(), api_key)
        finally:
            exc.close()
        suffix = f" Detalhe: {detail[:500]}" if detail else ""
        raise OpenAICompatibleError(
            f"Falha em {_safe_url_for_error(endpoint)}: HTTP {exc.code}.{suffix}"
        ) from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise OpenAICompatibleError(
            f"Falha ao conectar em {_safe_url_for_error(endpoint)}: {reason}"
        ) from exc
    except TimeoutError as exc:
        raise OpenAICompatibleError(
            f"Timeout ao conectar em {_safe_url_for_error(endpoint)}."
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenAICompatibleError("A resposta do provedor não contém JSON válido.") from exc
    if not isinstance(parsed, dict):
        raise OpenAICompatibleError("O provedor retornou uma resposta JSON inválida.")
    return parsed


def _create_anthropic_completion(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    temperature: Optional[float],
) -> str:
    system_parts = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "system"
    ]
    transcript = [
        {"role": str(message.get("role")), "content": str(message.get("content") or "")}
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    payload: Dict[str, Any] = {
        "model": config.model,
        "max_tokens": 4096,
        "messages": transcript,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if temperature is not None:
        payload["temperature"] = temperature
    parsed = _post_json(
        _base_endpoint(config, "messages"),
        payload,
        {
            "x-api-key": config.api_key or "",
            "anthropic-version": "2023-06-01",
        },
        config.timeout_seconds,
        config.api_key,
    )
    blocks = parsed.get("content")
    if not isinstance(blocks, list):
        raise OpenAICompatibleError("A resposta do Claude não contém blocos de conteúdo.")
    text = "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not text:
        raise OpenAICompatibleError("A resposta do Claude não contém texto.")
    return text


def _gemini_endpoint(config: OpenAIConfig) -> str:
    base = config.base_url.strip().rstrip("/")
    model = config.model.strip().strip("/")
    if not base:
        raise OpenAIConfigError("Defina a URL base da API Gemini.")
    if not model or "/" in model:
        raise OpenAIConfigError("Informe um identificador de modelo Gemini válido.")
    if base.endswith(":generateContent"):
        return base
    if "/models/" in base:
        return f"{base}:generateContent"
    return f"{base}/models/{model}:generateContent"


def _gemini_request_payload(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    temperature: Optional[float],
    tools: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    systems = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "system"
    ]
    contents = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant", "model"}:
            continue
        if isinstance(message.get("_gemini_content"), dict):
            parts = message["_gemini_content"].get("parts")
            if isinstance(parts, list):
                contents.append({
                    "role": "model",
                    "parts": parts,
                })
                continue
        if isinstance(message.get("_gemini_parts"), list):
            contents.append({
                "role": "user",
                "parts": message["_gemini_parts"],
            })
            continue
        contents.append({
            "role": "model" if role in {"assistant", "model"} else "user",
            "parts": [{"text": str(message.get("content") or "")}],
        })
    payload: Dict[str, Any] = {"contents": contents}
    if systems:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(systems)}]}
    generation: Dict[str, Any] = {}
    if temperature is not None:
        generation["temperature"] = temperature
    if generation:
        payload["generationConfig"] = generation
    if tools:
        def gemini_schema(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: (item.upper() if key == "type" and isinstance(item, str) else gemini_schema(item))
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [gemini_schema(item) for item in value]
            return value

        declarations = []
        for item in tools:
            function = item.get("function") if isinstance(item, dict) else None
            if not isinstance(function, dict):
                continue
            declarations.append({
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": gemini_schema(function.get("parameters", {"type": "object", "properties": {}})),
            })
        if declarations:
            payload["tools"] = [{"functionDeclarations": declarations}]
    return payload


def _gemini_response(parsed: Dict[str, Any]) -> tuple[str, list[dict[str, Any]], Optional[dict[str, Any]]]:
    candidates = parsed.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise OpenAICompatibleError("A resposta do Gemini não contém partes de conteúdo.")
    text = "\n".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()
    calls = [
        part["functionCall"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
    ]
    return text, calls, content if isinstance(content, dict) else None


def _create_gemini_completion(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    temperature: Optional[float],
) -> str:
    parsed = _post_json(
        _gemini_endpoint(config),
        _gemini_request_payload(config, messages, temperature),
        {"x-goog-api-key": config.api_key or ""},
        config.timeout_seconds,
        config.api_key,
    )
    text, calls, _ = _gemini_response(parsed)
    if calls or not text:
        raise OpenAICompatibleError("O Gemini não retornou texto para esta solicitação.")
    return text


def create_chat_completion_with_tools(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    run_tool: Any,
    *,
    max_tool_rounds: int = 4,
) -> tuple[str, list[dict[str, Any]]]:
    """Executa tools de leitura com modelos OpenAI-compatible ou com Claude."""
    if config.provider == "anthropic":
        return _anthropic_tools(config, messages, tools, run_tool, max_tool_rounds)
    if config.provider == "google":
        return _gemini_tools(config, messages, tools, run_tool, max_tool_rounds)
    if config.provider == "openai":
        return _openai_responses_tools(config, messages, tools, run_tool, max_tool_rounds)
    return _openai_tools(config, messages, tools, run_tool, max_tool_rounds)


def _openai_tools(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    run_tool: Any,
    max_tool_rounds: int,
) -> tuple[str, list[dict[str, Any]]]:
    transcript = [dict(message) for message in messages]
    sources: list[dict[str, Any]] = []
    for _round in range(max_tool_rounds + 1):
        payload: Dict[str, Any] = {
            "model": config.model,
            "messages": transcript,
            "tools": list(tools),
            "tool_choice": "auto",
        }
        parsed = _post_json(
            config.endpoint_url,
            payload,
            {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
            config.timeout_seconds,
            config.api_key,
        )
        choices = parsed.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise OpenAICompatibleError("A resposta do modelo não contém uma mensagem.")
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return _extract_message_content(parsed), sources
        transcript.append(message)
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                arguments = {}
            result = run_tool(name, arguments if isinstance(arguments, dict) else {})
            sources.extend(result.get("sources", []))
            transcript.append({
                "role": "tool",
                "tool_call_id": str(call.get("id") or ""),
                "content": json.dumps(result.get("result"), ensure_ascii=False),
            })
    raise OpenAICompatibleError("O modelo excedeu o limite de pesquisas nesta resposta.")


def _openai_responses_tools(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    run_tool: Any,
    max_tool_rounds: int,
) -> tuple[str, list[dict[str, Any]]]:
    instructions, transcript = _openai_responses_payload(messages)
    response_tools = []
    for item in tools:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict):
            response_tools.append({
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                "strict": False,
            })

    sources: list[dict[str, Any]] = []
    for _round in range(max_tool_rounds + 1):
        payload: Dict[str, Any] = {
            "model": config.model,
            "input": transcript,
            "max_output_tokens": 4096,
            "tools": response_tools,
            "tool_choice": "auto",
        }
        if instructions:
            payload["instructions"] = instructions
        parsed = _post_json(
            _base_endpoint(config, "responses"),
            payload,
            {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
            config.timeout_seconds,
            config.api_key,
        )
        text, calls = _responses_text_and_calls(parsed)
        if not calls:
            if not text:
                raise OpenAICompatibleError("A API Responses não retornou texto para esta solicitação.")
            return text, sources

        output = parsed.get("output")
        if not isinstance(output, list):
            raise OpenAICompatibleError("A API Responses retornou chamadas de ferramenta sem a saída correspondente.")
        transcript.extend(output)
        for call in calls:
            name = str(call.get("name") or "")
            raw_arguments = call.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                arguments = {}
            result = run_tool(name, arguments if isinstance(arguments, dict) else {})
            sources.extend(result.get("sources", []))
            transcript.append({
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or ""),
                "output": json.dumps(result.get("result"), ensure_ascii=False),
            })
    raise OpenAICompatibleError("O modelo excedeu o limite de pesquisas nesta resposta.")


def _anthropic_tools(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    run_tool: Any,
    max_tool_rounds: int,
) -> tuple[str, list[dict[str, Any]]]:
    system_parts = [str(message.get("content") or "") for message in messages if message.get("role") == "system"]
    transcript = [
        {"role": str(message.get("role")), "content": str(message.get("content") or "")}
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    anthropic_tools = []
    for item in tools:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict):
            anthropic_tools.append({
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            })
    sources: list[dict[str, Any]] = []
    for _round in range(max_tool_rounds + 1):
        payload: Dict[str, Any] = {
            "model": config.model,
            "max_tokens": 4096,
            "messages": transcript,
            "tools": anthropic_tools,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        parsed = _post_json(
            _base_endpoint(config, "messages"),
            payload,
            {"x-api-key": config.api_key or "", "anthropic-version": "2023-06-01"},
            config.timeout_seconds,
            config.api_key,
        )
        blocks = parsed.get("content")
        if not isinstance(blocks, list):
            raise OpenAICompatibleError("A resposta do Claude não contém blocos de conteúdo.")
        tool_calls = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
        if not tool_calls:
            answer = "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("type") == "text").strip()
            if not answer:
                raise OpenAICompatibleError("A resposta do Claude não contém texto.")
            return answer, sources
        transcript.append({"role": "assistant", "content": blocks})
        results = []
        for call in tool_calls:
            result = run_tool(str(call.get("name") or ""), call.get("input") if isinstance(call.get("input"), dict) else {})
            sources.extend(result.get("sources", []))
            results.append({
                "type": "tool_result",
                "tool_use_id": str(call.get("id") or ""),
                "content": json.dumps(result.get("result"), ensure_ascii=False),
            })
        transcript.append({"role": "user", "content": results})
    raise OpenAICompatibleError("O Claude excedeu o limite de pesquisas nesta resposta.")


def _gemini_tools(
    config: OpenAIConfig,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    run_tool: Any,
    max_tool_rounds: int,
) -> tuple[str, list[dict[str, Any]]]:
    transcript = [
        {"role": str(message.get("role")), "content": str(message.get("content") or "")}
        for message in messages
        if message.get("role") in {"system", "user", "assistant"}
    ]
    sources: list[dict[str, Any]] = []
    for _round in range(max_tool_rounds + 1):
        payload = _gemini_request_payload(config, transcript, None, tools)
        parsed = _post_json(
            _gemini_endpoint(config),
            payload,
            {"x-goog-api-key": config.api_key or ""},
            config.timeout_seconds,
            config.api_key,
        )
        text, calls, model_content = _gemini_response(parsed)
        if not calls:
            if not text:
                raise OpenAICompatibleError("O Gemini não retornou texto.")
            return text, sources
        transcript.append({"role": "model", "content": "", "_gemini_content": model_content})
        responses = []
        for call in calls:
            name = str(call.get("name") or "")
            arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
            result = run_tool(name, arguments)
            sources.extend(result.get("sources", []))
            responses.append({
                "functionResponse": {
                    "name": name,
                    "response": {"result": result.get("result")},
                }
            })
        transcript.append({"role": "user", "content": "", "_gemini_parts": responses})
    raise OpenAICompatibleError("O Gemini excedeu o limite de pesquisas nesta resposta.")


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extrai um objeto JSON de texto puro, bloco markdown ou texto envoltorio."""

    raw = (text or "").strip()
    if not raw:
        return None

    fenced_candidates = [
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json)?\s*(.*?)```",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]

    decoder = json.JSONDecoder()
    for candidate in [raw, *fenced_candidates]:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    for candidate in [*fenced_candidates, raw]:
        for match in re.finditer(r"\{", candidate):
            try:
                parsed, _ = decoder.raw_decode(candidate[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    return None
