"""
Anthropic-to-OpenAI API Proxy Server.

Translates Anthropic Messages API requests/responses to OpenAI Chat Completions
format, enabling Claw Code to work with vLLM or any OpenAI-compatible backend.

Usage:
    python server.py
    # or
    uvicorn server:app --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import PROXY_HOST, PROXY_PORT, VLLM_API_KEY, VLLM_BASE_URL, resolve_model
from transform import (
    anthropic_to_openai_request,
    make_content_block_start,
    make_content_block_stop,
    make_input_json_delta,
    make_message_delta,
    make_message_start,
    make_message_stop,
    make_text_delta,
    openai_to_anthropic_response,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("proxy")

app = FastAPI(title="Anthropic-to-OpenAI Proxy")


@app.post("/v1/messages", response_model=None)
async def messages(request: Request):
    """Handle Anthropic Messages API calls, proxy to vLLM OpenAI endpoint."""
    body: dict[str, Any] = await request.json()
    original_model = body.get("model", "")
    stream = body.get("stream", False)

    # Convert request
    openai_req = anthropic_to_openai_request(body)
    # Dynamically clamp max_tokens to fit within model context window
    # Use conservative estimate: 3 chars ≈ 1 token (safer than 4)
    MODEL_CONTEXT_LIMIT = 32768
    msg_text = json.dumps(openai_req.get("messages", []))
    tools_text = json.dumps(openai_req.get("tools", []))
    estimated_input = (len(msg_text) + len(tools_text)) // 3
    available = MODEL_CONTEXT_LIMIT - estimated_input - 1024  # 1024 token safety margin
    openai_req["max_tokens"] = max(min(openai_req.get("max_tokens", 4096), available, 8192), 256)
    logger.info("Estimated input: %d tokens, max_output clamped to: %d", estimated_input, openai_req["max_tokens"])
    logger.info(
        "Proxying %s -> %s (stream=%s, tools=%d)",
        original_model,
        openai_req["model"],
        stream,
        len(openai_req.get("tools", [])),
    )

    vllm_url = f"{VLLM_BASE_URL}/v1/chat/completions"

    if stream:
        return StreamingResponse(
            stream_proxy(vllm_url, openai_req, original_model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming (with retry on context overflow)
    headers = {"Authorization": f"Bearer {VLLM_API_KEY}"}
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(vllm_url, json=openai_req, headers=headers)
        if resp.status_code != 200 and "maximum context length" in resp.text:
            # Retry: halve max_tokens and trim older messages
            logger.warning("Context overflow, retrying with reduced tokens")
            openai_req["max_tokens"] = max(openai_req["max_tokens"] // 2, 256)
            msgs = openai_req.get("messages", [])
            # Keep system + last 4 messages
            if len(msgs) > 5:
                system_msgs = [m for m in msgs if m.get("role") == "system"]
                openai_req["messages"] = system_msgs + msgs[-4:]
            resp = await client.post(vllm_url, json=openai_req, headers=headers)
        if resp.status_code != 200:
            logger.error("vLLM error %d: %s", resp.status_code, resp.text[:500])
            return JSONResponse(
                status_code=resp.status_code,
                content={"type": "error", "error": {"type": "api_error", "message": resp.text}},
            )
        openai_resp = resp.json()

    anthropic_resp = openai_to_anthropic_response(openai_resp, original_model)
    return JSONResponse(content=anthropic_resp)


async def stream_proxy(
    vllm_url: str, openai_req: dict[str, Any], original_model: str
) -> Any:
    """Stream OpenAI SSE events, converting each to Anthropic SSE format."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    text_block_started = False
    text_index = 0
    tool_blocks: dict[int, dict[str, Any]] = {}
    next_block_index = 0
    output_tokens = 0
    input_tokens = 0
    finish_reason = "end_turn"
    headers = {"Authorization": f"Bearer {VLLM_API_KEY}"}

    # Retry logic: if context overflow, trim messages and retry
    current_req = openai_req
    max_attempts = 2

    async with httpx.AsyncClient(timeout=300.0) as client:
        for attempt in range(max_attempts):
            async with client.stream("POST", vllm_url, json=current_req, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = ""
                    async for chunk in resp.aiter_text():
                        error_body += chunk

                    if attempt < max_attempts - 1 and "maximum context length" in error_body:
                        logger.warning("Context overflow (attempt %d), trimming messages", attempt)
                        current_req = dict(current_req)
                        current_req["max_tokens"] = max(current_req["max_tokens"] // 2, 256)
                        msgs = current_req.get("messages", [])
                        if len(msgs) > 5:
                            system_msgs = [m for m in msgs if m.get("role") == "system"]
                            current_req["messages"] = system_msgs + msgs[-4:]
                        continue

                    logger.error("vLLM stream error %d: %s", resp.status_code, error_body[:500])
                    yield make_message_start(msg_id, original_model, 0)
                    yield make_content_block_start(0, "text")
                    yield make_text_delta(0, f"[Backend error: context too long. Try /compact or start a new session.]")
                    yield make_content_block_stop(0)
                    yield make_message_delta("end_turn", 0, 0)
                    yield make_message_stop()
                    return

                # Success - stream the response
                yield make_message_start(msg_id, original_model, 0)

                buffer = ""
                in_think_block = False  # Track <think> blocks
                think_buffer = ""

                async for raw_chunk in resp.aiter_text():
                    buffer += raw_chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()

                        if not line or line == "data: [DONE]" or not line.startswith("data: "):
                            continue

                        try:
                            chunk_data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        choice = (chunk_data.get("choices") or [{}])[0]
                        delta = choice.get("delta", {})

                        chunk_usage = chunk_data.get("usage")
                        if chunk_usage:
                            input_tokens = chunk_usage.get("prompt_tokens", input_tokens)
                            output_tokens = chunk_usage.get("completion_tokens", output_tokens)

                        fr = choice.get("finish_reason")
                        if fr:
                            reason_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
                            finish_reason = reason_map.get(fr, "end_turn")

                        text_content = delta.get("content")
                        if text_content:
                            # Filter out <think>...</think> blocks from Qwen3
                            think_buffer += text_content
                            # Process accumulated text to strip think blocks
                            while think_buffer:
                                if in_think_block:
                                    end_idx = think_buffer.find("</think>")
                                    if end_idx >= 0:
                                        think_buffer = think_buffer[end_idx + 8:]
                                        in_think_block = False
                                    else:
                                        think_buffer = ""  # Still inside think, discard
                                        break
                                else:
                                    start_idx = think_buffer.find("<think>")
                                    if start_idx >= 0:
                                        # Emit text before <think>
                                        clean = think_buffer[:start_idx]
                                        if clean:
                                            if not text_block_started:
                                                yield make_content_block_start(next_block_index, "text")
                                                text_index = next_block_index
                                                next_block_index += 1
                                                text_block_started = True
                                            yield make_text_delta(text_index, clean)
                                            output_tokens += 1
                                        think_buffer = think_buffer[start_idx + 7:]
                                        in_think_block = True
                                    else:
                                        # No think tag, emit all
                                        if not text_block_started:
                                            yield make_content_block_start(next_block_index, "text")
                                            text_index = next_block_index
                                            next_block_index += 1
                                            text_block_started = True
                                        yield make_text_delta(text_index, think_buffer)
                                        output_tokens += 1
                                        think_buffer = ""
                                        break

                        for tc in delta.get("tool_calls") or []:
                            tc_index = tc.get("index", 0)
                            func = tc.get("function", {})

                            if tc_index not in tool_blocks:
                                if text_block_started:
                                    yield make_content_block_stop(text_index)
                                    text_block_started = False

                                tool_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                                tool_name = func.get("name", "")
                                tool_blocks[tc_index] = {
                                    "id": tool_id, "name": tool_name,
                                    "args_buffer": "", "block_index": next_block_index,
                                }
                                yield make_content_block_start(
                                    next_block_index, "tool_use", id=tool_id, name=tool_name,
                                )
                                next_block_index += 1

                            args_chunk = func.get("arguments", "")
                            if args_chunk:
                                tool_blocks[tc_index]["args_buffer"] += args_chunk
                                yield make_input_json_delta(tool_blocks[tc_index]["block_index"], args_chunk)

                # Close open blocks
                if text_block_started:
                    yield make_content_block_stop(text_index)
                for tb in tool_blocks.values():
                    yield make_content_block_stop(tb["block_index"])

                yield make_message_delta(finish_reason, output_tokens, input_tokens)
                yield make_message_stop()
                return  # Success, don't retry


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Proxy model listing from vLLM."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{VLLM_BASE_URL}/v1/models", headers={"Authorization": f"Bearer {VLLM_API_KEY}"})
            return JSONResponse(content=resp.json())
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": f"Cannot reach vLLM: {e}"},
            )


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Anthropic-to-OpenAI proxy on %s:%d", PROXY_HOST, PROXY_PORT)
    logger.info("vLLM backend: %s", VLLM_BASE_URL)
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="info")
