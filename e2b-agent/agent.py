#!/usr/bin/env python3
"""e2b-agent: Local LLM coding agent powered by vLLM."""

import sys
import json
import time
import os

from openai import OpenAI
from rich.live import Live
from rich.spinner import Spinner

from config import VLLM_URL, VLLM_API_KEY, MODEL, MAX_TOKENS, MAX_CONTEXT, MAX_ITERATIONS
from tools import TOOL_DEFINITIONS, execute_tool
from system_prompt import build_system_prompt
from compact import estimate_tokens, compact_messages
from tui import (
    get_console,
    print_welcome,
    print_tool_call,
    print_tool_result,
    print_assistant,
    print_error,
    print_status,
    stream_print,
    stream_end,
)


def main():
    client = OpenAI(base_url=VLLM_URL, api_key=VLLM_API_KEY)
    messages = [{"role": "system", "content": build_system_prompt()}]
    console = get_console()

    print_welcome(MODEL, os.getcwd())

    while True:
        try:
            user_input = console.input("[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            handle_slash_command(user_input, messages, client)
            continue

        messages.append({"role": "user", "content": user_input})

        # Agent loop
        for iteration in range(MAX_ITERATIONS):
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    stream=True,
                    max_tokens=MAX_TOKENS,
                )
            except Exception as e:
                print_error(f"API error: {e}")
                break

            # Stream response and collect tool calls
            assistant_content = ""
            current_tool_calls = {}
            first_text = True

            # Show spinner until first content arrives
            spinner_live = Live(
                Spinner("dots", text="Thinking..."),
                console=console,
                transient=True,
            )
            spinner_live.start()
            spinner_active = True

            try:
                for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if not delta:
                        continue

                    # Text content
                    if delta.content:
                        if spinner_active:
                            spinner_live.stop()
                            spinner_active = False
                        assistant_content += delta.content
                        stream_print(delta.content)

                    # Tool calls
                    if delta.tool_calls:
                        if spinner_active:
                            spinner_live.stop()
                            spinner_active = False
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {
                                    "id": tc.id or f"call_{idx}_{iteration}",
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc.function:
                                if tc.function.name:
                                    current_tool_calls[idx]["name"] = tc.function.name
                                if tc.function.arguments:
                                    current_tool_calls[idx]["arguments"] += tc.function.arguments
            finally:
                if spinner_active:
                    spinner_live.stop()

            # Finish streaming text
            if assistant_content:
                stream_end()
                print_assistant(assistant_content)

            # Build tool_calls list
            tool_calls = []
            for idx in sorted(current_tool_calls.keys()):
                tool_calls.append(current_tool_calls[idx])

            # Build assistant message
            assistant_msg = {"role": "assistant", "content": assistant_content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            # No tool calls = done
            if not tool_calls:
                break

            # Execute tools
            for tc in tool_calls:
                name = tc["name"]
                args = tc["arguments"]
                print_tool_call(name, args)

                start = time.time()
                result = execute_tool(name, args)
                elapsed = time.time() - start

                success = not result.startswith("Error:")
                print_tool_result(name, result, success, elapsed)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # Auto-compact
        tokens = estimate_tokens(messages)
        if tokens > MAX_CONTEXT * 0.8:
            console.print("[dim]Auto-compacting context...[/]")
            messages = compact_messages(messages, client, MODEL)


def handle_slash_command(cmd, messages, client):
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()

    if command == "/help":
        print_help()
    elif command == "/status":
        print_status(MODEL, estimate_tokens(messages), len(messages), os.getcwd())
    elif command == "/compact":
        messages[:] = compact_messages(messages, client, MODEL)
        get_console().print("[green]Context compacted.[/]")
    elif command == "/clear":
        messages.clear()
        messages.append({"role": "system", "content": build_system_prompt()})
        get_console().print("[green]Conversation cleared.[/]")
    else:
        get_console().print(f"[red]Unknown command: {command}[/]")


def print_help():
    get_console().print("""[bold]Commands:[/]
  /help     Show this help
  /status   Show model, token count, CWD
  /compact  Compress conversation history
  /clear    Start fresh conversation
  Ctrl+D    Exit""")


if __name__ == "__main__":
    main()
