"""Tool definitions and execution for the e2b coding agent."""

import json
import glob
import os
import subprocess

# ---------------------------------------------------------------------------
# OpenAI function-calling tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return stdout, stderr, and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific text snippet in a file with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find and replace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files matching a glob pattern (recursive).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: current directory).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search file contents with a regex pattern using grep -rn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in (default: current directory).",
                    },
                    "include": {
                        "type": "string",
                        "description": "File glob filter, e.g. '*.py'.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution functions
# ---------------------------------------------------------------------------


def _run_bash(command: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(result.stderr)
        parts.append(f"[exit code: {result.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def _read_file(path: str, offset: int = None, limit: int = None) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = (offset - 1) if offset and offset >= 1 else 0
        end = (start + limit) if limit else len(lines)
        selected = lines[start:end]
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:6d}\t{line.rstrip()}")
        if not numbered:
            return "(empty file)"
        return "\n".join(numbered)
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except Exception as e:
        return f"Error: {e}"


def _write_file(path: str, content: str) -> str:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {line_count} lines to {path}"
    except Exception as e:
        return f"Error: {e}"


def _edit_file(path: str, old_text: str, new_text: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if old_text not in content:
            return f"Error: old_text not found in {path}"
        count = content.count(old_text)
        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        diff_parts = []
        for line in old_lines:
            diff_parts.append(f"- {line}")
        for line in new_lines:
            diff_parts.append(f"+ {line}")
        note = f" ({count} occurrences, replaced first)" if count > 1 else ""
        return f"Edited {path}{note}\n" + "\n".join(diff_parts)
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except Exception as e:
        return f"Error: {e}"


def _glob_search(pattern: str, path: str = ".") -> str:
    try:
        full_pattern = os.path.join(path, pattern)
        matches = sorted(glob.glob(full_pattern, recursive=True))
        if not matches:
            return "No files found."
        return "\n".join(matches)
    except Exception as e:
        return f"Error: {e}"


def _grep_search(pattern: str, path: str = ".", include: str = None) -> str:
    try:
        cmd = ["grep", "-rn", "--color=never"]
        if include:
            cmd.extend(["--include", include])
        cmd.extend([pattern, path])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        output = result.stdout.strip()
        if not output:
            return "No matches found."
        # Limit output to 200 lines
        lines = output.splitlines()
        if len(lines) > 200:
            return "\n".join(lines[:200]) + f"\n... ({len(lines) - 200} more lines)"
        return output
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 30s"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "bash": lambda args: _run_bash(args.get("command", ""), args.get("timeout", 120)),
    "read_file": lambda args: _read_file(args.get("path", ""), args.get("offset"), args.get("limit")),
    "write_file": lambda args: _write_file(args.get("path", ""), args.get("content", "")),
    "edit_file": lambda args: _edit_file(args.get("path", ""), args.get("old_text", ""), args.get("new_text", "")),
    "glob_search": lambda args: _glob_search(args.get("pattern", ""), args.get("path", ".")),
    "grep_search": lambda args: _grep_search(args.get("pattern", ""), args.get("path", "."), args.get("include")),
}


def execute_tool(name: str, args_json: str) -> str:
    """Execute a tool by name with JSON-encoded arguments. Returns result string."""
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON arguments: {e}"

    executor = _EXECUTORS.get(name)
    if not executor:
        return f"Error: Unknown tool: {name}"

    try:
        return executor(args)
    except Exception as e:
        return f"Error executing {name}: {e}"
