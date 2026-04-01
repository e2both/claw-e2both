"""Unit tests for the Anthropic <-> OpenAI transform layer."""

import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from transform import (
    anthropic_to_openai_request,
    convert_messages,
    convert_tools,
    convert_tool_choice,
    openai_to_anthropic_response,
    make_message_start,
    make_text_delta,
    make_content_block_start,
    make_content_block_stop,
    make_message_delta,
    make_message_stop,
    make_input_json_delta,
)


def test_simple_text_request():
    """Simple text message converts correctly."""
    req = {
        "model": "claude-opus-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    result = anthropic_to_openai_request(req)
    assert result["max_tokens"] == 1024
    assert result["messages"] == [{"role": "user", "content": "Hello"}]
    assert result["stream"] is False


def test_system_prompt_becomes_first_message():
    """System prompt is inserted as the first system message."""
    req = {
        "model": "claude-opus-4-6",
        "max_tokens": 100,
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = anthropic_to_openai_request(req)
    assert result["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert result["messages"][1] == {"role": "user", "content": "Hi"}


def test_content_blocks_to_string():
    """Array of text content blocks becomes a single string."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": "World"},
            ],
        }
    ]
    result = convert_messages(messages, None)
    assert result[0]["content"] == "Hello\nWorld"


def test_tool_use_in_assistant_message():
    """Assistant tool_use blocks convert to tool_calls."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "bash",
                    "input": {"command": "ls"},
                },
            ],
        }
    ]
    result = convert_messages(messages, None)
    msg = result[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Let me check."
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "toolu_123"
    assert tc["function"]["name"] == "bash"
    assert json.loads(tc["function"]["arguments"]) == {"command": "ls"}


def test_tool_result_becomes_tool_message():
    """Tool results become role=tool messages."""
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": "file1.txt\nfile2.txt",
                }
            ],
        }
    ]
    result = convert_messages(messages, None)
    assert result[0]["role"] == "tool"
    assert result[0]["tool_call_id"] == "toolu_123"
    assert result[0]["content"] == "file1.txt\nfile2.txt"


def test_convert_tools():
    """Anthropic tool definitions convert to OpenAI function format."""
    tools = [
        {
            "name": "bash",
            "description": "Run a shell command",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }
    ]
    result = convert_tools(tools)
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "bash"
    assert result[0]["function"]["parameters"]["type"] == "object"


def test_convert_tool_choice():
    """Tool choice variants convert correctly."""
    assert convert_tool_choice({"type": "auto"}) == "auto"
    assert convert_tool_choice({"type": "any"}) == "required"
    assert convert_tool_choice({"type": "tool", "name": "bash"}) == {
        "type": "function",
        "function": {"name": "bash"},
    }


def test_openai_text_response():
    """Simple text response converts to Anthropic format."""
    openai_resp = {
        "id": "chatcmpl-123",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    result = openai_to_anthropic_response(openai_resp, "claude-opus-4-6")
    assert result["type"] == "message"
    assert result["role"] == "assistant"
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "Hello!"
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5


def test_openai_tool_call_response():
    """Tool call response converts to tool_use content blocks."""
    openai_resp = {
        "id": "chatcmpl-456",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command": "ls"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    result = openai_to_anthropic_response(openai_resp, "claude-opus-4-6")
    assert result["stop_reason"] == "tool_use"
    assert len(result["content"]) == 1
    block = result["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "bash"
    assert block["input"] == {"command": "ls"}


def test_streaming_events_format():
    """SSE event formatting produces valid Anthropic SSE."""
    start = make_message_start("msg_123", "claude-opus-4-6", 10)
    assert "event: message_start\n" in start
    parsed = json.loads(start.split("data: ", 1)[1].split("\n\n")[0])
    assert parsed["type"] == "message_start"

    block_start = make_content_block_start(0, "text")
    assert "event: content_block_start\n" in block_start

    delta = make_text_delta(0, "Hello")
    assert "event: content_block_delta\n" in delta
    parsed = json.loads(delta.split("data: ", 1)[1].split("\n\n")[0])
    assert parsed["delta"]["text"] == "Hello"

    block_stop = make_content_block_stop(0)
    assert "event: content_block_stop\n" in block_stop

    msg_delta = make_message_delta("end_turn", 42)
    parsed = json.loads(msg_delta.split("data: ", 1)[1].split("\n\n")[0])
    assert parsed["delta"]["stop_reason"] == "end_turn"
    assert parsed["usage"]["output_tokens"] == 42

    msg_stop = make_message_stop()
    assert "event: message_stop\n" in msg_stop


def test_tool_use_streaming_events():
    """Tool use SSE events are correctly formatted."""
    block_start = make_content_block_start(
        1, "tool_use", id="toolu_abc", name="bash"
    )
    parsed = json.loads(block_start.split("data: ", 1)[1].split("\n\n")[0])
    assert parsed["content_block"]["type"] == "tool_use"
    assert parsed["content_block"]["id"] == "toolu_abc"
    assert parsed["content_block"]["name"] == "bash"

    json_delta = make_input_json_delta(1, '{"command":')
    parsed = json.loads(json_delta.split("data: ", 1)[1].split("\n\n")[0])
    assert parsed["delta"]["type"] == "input_json_delta"
    assert parsed["delta"]["partial_json"] == '{"command":'


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
