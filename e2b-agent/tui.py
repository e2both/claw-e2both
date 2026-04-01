"""Terminal UI helpers using the rich library."""

import sys
import json
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_console = Console()


def get_console() -> Console:
    return _console


def print_welcome(model: str, cwd: str):
    _console.print(Panel(
        f"[bold green]e2b-agent[/] - Local LLM Coding Agent\n"
        f"[dim]Model:[/] {model}\n"
        f"[dim]CWD:[/]   {cwd}\n"
        f"[dim]Type /help for commands, Ctrl+D to exit[/]",
        border_style="cyan",
    ))


def print_tool_call(name: str, args: str):
    try:
        parsed = json.loads(args)
        # Show compact version of args
        if name == "bash":
            display = parsed.get("command", args)
        elif name == "write_file":
            path = parsed.get("path", "?")
            content = parsed.get("content", "")
            lines = content.count("\n") + 1
            display = f"{path} ({lines} lines)"
        elif name == "edit_file":
            path = parsed.get("path", "?")
            display = f"{path}"
        elif name == "read_file":
            display = parsed.get("path", args)
        else:
            display = json.dumps(parsed, ensure_ascii=False)
            if len(display) > 200:
                display = display[:200] + "..."
    except (json.JSONDecodeError, TypeError):
        display = args[:200] if args else ""

    _console.print(Panel(
        f"[yellow]{display}[/]",
        title=f"[bold yellow]Tool: {name}[/]",
        border_style="yellow",
        padding=(0, 1),
    ))


def print_tool_result(name: str, output: str, success: bool, elapsed: float):
    color = "green" if success else "red"
    # Truncate very long output for display
    display = output
    if len(display) > 2000:
        display = display[:2000] + f"\n... ({len(output) - 2000} more chars)"

    _console.print(Panel(
        display,
        title=f"[bold {color}]{name}[/] [dim]({elapsed:.1f}s)[/]",
        border_style=color,
        padding=(0, 1),
    ))


def print_thinking():
    """Not used directly - Live+Spinner is used inline in agent.py."""
    pass


def print_assistant(text: str):
    """Called after streaming is complete. Since we already streamed the raw
    text to stdout, we don't re-print it. This is intentionally a no-op."""
    pass


def print_error(msg: str):
    _console.print(f"[bold red]Error:[/] {msg}")


def print_status(model: str, tokens: int, messages: int, cwd: str):
    _console.print(Panel(
        f"[dim]Model:[/]    {model}\n"
        f"[dim]Tokens:[/]   ~{tokens:,}\n"
        f"[dim]Messages:[/] {messages}\n"
        f"[dim]CWD:[/]      {cwd}",
        title="[bold]Status[/]",
        border_style="blue",
    ))


def stream_print(text_chunk: str):
    """Print a streaming text chunk immediately to stdout."""
    sys.stdout.write(text_chunk)
    sys.stdout.flush()


def stream_end():
    """Finish streaming output with a newline."""
    sys.stdout.write("\n")
    sys.stdout.flush()
