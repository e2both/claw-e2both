"""Transform between Anthropic Messages API and OpenAI Chat Completions API formats."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from config import resolve_model


def strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Request: Anthropic -> OpenAI
# ---------------------------------------------------------------------------

def anthropic_to_openai_request(req: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic /v1/messages request to OpenAI /v1/chat/completions."""
    stream = req.get("stream", False)
    openai: dict[str, Any] = {
        "model": resolve_model(req.get("model", "")),
        "max_tokens": req.get("max_tokens", 4096),
        "stream": stream,
    }
    # Request usage in streaming responses
    if stream:
        openai["stream_options"] = {"include_usage": True}

    # Disable Qwen3 thinking mode via chat_template_kwargs
    openai["chat_template_kwargs"] = {"enable_thinking": False}

    # Temperature / top_p passthrough
    if "temperature" in req:
        openai["temperature"] = req["temperature"]
    if "top_p" in req:
        openai["top_p"] = req["top_p"]

    # Messages
    openai["messages"] = convert_messages(req.get("messages", []), req.get("system"))

    # Tools
    if req.get("tools"):
        openai["tools"] = convert_tools(req["tools"])

    # Tool choice
    tc = req.get("tool_choice")
    if tc is not None:
        openai["tool_choice"] = convert_tool_choice(tc)

    return openai


def convert_messages(
    messages: list[dict[str, Any]], system: str | None
) -> list[dict[str, Any]]:
    """Convert Anthropic message list to OpenAI format."""
    out: list[dict[str, Any]] = []

    # System prompt becomes the first message
    if system:
        # Append agent behavior instructions for local models
        agent_suffix = (
            "\n\n<CRITICAL INSTRUCTIONS>"
            "\nYou are an autonomous coding agent. You MUST use the provided tools to complete tasks."
            "\nDo NOT just describe what you would do - actually DO it by calling the appropriate tool."
            "\nWhen asked to check something, use bash to run the command."
            "\nWhen asked to create a file, use write_file."
            "\nWhen asked to modify code, use edit_file."
            "\nNEVER suggest code without executing it. ALWAYS call tools directly."
            "\nDo NOT use <think> tags. Respond directly and concisely."
            "\n"
            "\nFORMATTING RULES:"
            "\n- Keep responses SHORT and CONCISE. Maximum 5-10 lines."
            "\n- Do NOT repeat tool output back to the user. They can already see it."
            "\n- After a tool call, give a 1-2 sentence summary only."
            "\n- Use plain text. Do NOT use excessive markdown, numbered lists, or code blocks for simple answers."
            "\n- When showing search results, list only the top 3 most relevant items, one line each."
            "\n- Respond in the same language the user used."
            "\n</CRITICAL INSTRUCTIONS>"
            "\n/no_think"
        )
        out.append({"role": "system", "content": system + agent_suffix})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # String content — pass through directly
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        # Array of content blocks
        if isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for block in content:
                btype = block.get("type", "")

                if btype == "text":
                    text_parts.append(block.get("text", ""))

                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })

                elif btype == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = "\n".join(
                            b.get("text", "") for b in result_content if b.get("type") == "text"
                        )
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": str(result_content),
                    })

            # Emit assistant message with tool_calls
            if role == "assistant":
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    assistant_msg["content"] = "\n".join(text_parts)
                else:
                    assistant_msg["content"] = None
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                out.append(assistant_msg)
            elif tool_results:
                # Tool results become separate tool messages
                out.extend(tool_results)
                if text_parts:
                    out.append({"role": role, "content": "\n".join(text_parts)})
            else:
                out.append({"role": role, "content": "\n".join(text_parts) if text_parts else ""})

    return out


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function definitions."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def convert_tool_choice(tc: Any) -> Any:
    """Convert Anthropic tool_choice to OpenAI format."""
    if isinstance(tc, str):
        return tc  # "auto", "none", etc.
    if isinstance(tc, dict):
        tc_type = tc.get("type", "auto")
        if tc_type == "auto":
            return "auto"
        if tc_type == "any":
            return "required"
        if tc_type == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


# ---------------------------------------------------------------------------
# Response: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

def openai_to_anthropic_response(
    openai_resp: dict[str, Any], original_model: str
) -> dict[str, Any]:
    """Convert an OpenAI chat completion response to Anthropic message format."""
    choice = (openai_resp.get("choices") or [{}])[0]
    message = choice.get("message", {})
    usage = openai_resp.get("usage", {})

    content: list[dict[str, Any]] = []

    # Text content (strip <think>...</think> blocks from reasoning models)
    text = message.get("content")
    if text:
        text = strip_think_blocks(text)
        if text.strip():
            content.append({"type": "text", "text": text})

    # Tool calls -> tool_use blocks
    for tc in message.get("tool_calls") or []:
        func = tc.get("function", {})
        try:
            input_data = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            input_data = {"raw": func.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": func.get("name", ""),
            "input": input_data,
        })

    # Map finish reason
    finish = choice.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }

    return {
        "id": openai_resp.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": original_model,
        "stop_reason": stop_reason_map.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming: OpenAI SSE -> Anthropic SSE
# ---------------------------------------------------------------------------

def make_message_start(msg_id: str, model: str, input_tokens: int = 0) -> str:
    """Generate the Anthropic message_start SSE event."""
    data = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            },
        },
    }
    return f"event: message_start\ndata: {json.dumps(data)}\n\n"


def make_content_block_start(index: int, block_type: str = "text", **kwargs: Any) -> str:
    if block_type == "text":
        block = {"type": "text", "text": ""}
    elif block_type == "tool_use":
        block = {"type": "tool_use", "id": kwargs.get("id", ""), "name": kwargs.get("name", ""), "input": {}}
    else:
        block = {"type": block_type}
    data = {"type": "content_block_start", "index": index, "content_block": block}
    return f"event: content_block_start\ndata: {json.dumps(data)}\n\n"


def make_text_delta(index: int, text: str) -> str:
    data = {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}
    return f"event: content_block_delta\ndata: {json.dumps(data)}\n\n"


def make_input_json_delta(index: int, partial_json: str) -> str:
    data = {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": partial_json}}
    return f"event: content_block_delta\ndata: {json.dumps(data)}\n\n"


def make_content_block_stop(index: int) -> str:
    data = {"type": "content_block_stop", "index": index}
    return f"event: content_block_stop\ndata: {json.dumps(data)}\n\n"


def make_message_delta(stop_reason: str, output_tokens: int, input_tokens: int = 0) -> str:
    data = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    return f"event: message_delta\ndata: {json.dumps(data)}\n\n"


def make_message_stop() -> str:
    return "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
