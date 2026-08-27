"""Compatibility helpers for OpenAI-compatible API providers."""

import os


def get_openai_api_mode() -> str:
    """Return the Agents SDK transport selected for the configured provider."""
    mode = os.environ.get("SCENESMITH_OPENAI_API_MODE", "responses").strip().lower()
    if mode not in {"responses", "chat_completions"}:
        raise ValueError(
            "SCENESMITH_OPENAI_API_MODE must be 'responses' or 'chat_completions'"
        )
    return mode


def uses_json_object_structured_outputs() -> bool:
    """Return whether the provider supports JSON mode but not JSON Schema mode."""
    return (
        os.environ.get("SCENESMITH_OPENAI_STRUCTURED_OUTPUT_MODE", "json_schema")
        .strip()
        .lower()
        == "json_object"
    )


def configure_openai_agents_compatibility() -> None:
    """Configure the Agents SDK for the selected provider capabilities.

    Some OpenAI-compatible Responses APIs accept ``text.format=json_object`` but
    reject ``json_schema``. The Agents SDK still validates the returned JSON with
    its Pydantic output schema; only the wire-level response format is relaxed.
    """
    from agents import set_default_openai_api

    set_default_openai_api(get_openai_api_mode())

    if not uses_json_object_structured_outputs():
        return

    from agents.models.chatcmpl_converter import Converter as ChatConverter
    from agents.models.openai_responses import Converter as ResponsesConverter
    from openai import omit

    def get_response_format(cls, output_schema):
        if output_schema is None or output_schema.is_plain_text():
            return omit
        return {"format": {"type": "json_object"}}

    if not getattr(ResponsesConverter, "_scenesmith_json_object_compat", False):
        ResponsesConverter.get_response_format = classmethod(get_response_format)
        ResponsesConverter._scenesmith_json_object_compat = True

    def convert_chat_response_format(cls, output_schema):
        if output_schema is None or output_schema.is_plain_text():
            return omit
        return {"type": "json_object"}

    if not getattr(ChatConverter, "_scenesmith_json_object_compat", False):
        ChatConverter.convert_response_format = classmethod(
            convert_chat_response_format
        )
        ChatConverter._scenesmith_json_object_compat = True
