"""Context window management: token estimation and message compaction."""

import json


def estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~3 chars per token."""
    return len(json.dumps(messages, ensure_ascii=False)) // 3


def compact_messages(messages: list, client, model: str) -> list:
    """Compact conversation history by summarizing old messages.

    Keeps the system prompt and the last 4 messages intact.
    Summarizes everything in between using the LLM itself.
    """
    if len(messages) <= 6:
        return messages

    system_msg = messages[0] if messages[0]["role"] == "system" else None
    tail = messages[-4:]
    middle = messages[1:-4] if system_msg else messages[:-4]

    if not middle:
        return messages

    # Build a summary of the middle messages
    summary_parts = []
    for msg in middle:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "tool":
            # Truncate long tool outputs
            if content and len(content) > 500:
                content = content[:500] + "..."
            summary_parts.append(f"[tool result]: {content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                summary_parts.append(f"[assistant called tools: {', '.join(names)}]")
            if content:
                summary_parts.append(f"[assistant]: {content[:500]}")
        elif role == "user":
            summary_parts.append(f"[user]: {content}")

    conversation_text = "\n".join(summary_parts)

    # Ask the LLM to summarize
    try:
        summary_response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Summarize this conversation concisely. Focus on: what the user asked for, what files were created/modified, what commands were run, and the current state. Be brief.",
                },
                {"role": "user", "content": conversation_text},
            ],
            max_tokens=1024,
        )
        summary = summary_response.choices[0].message.content
    except Exception:
        # Fallback: simple truncation summary
        summary = f"[Previous conversation with {len(middle)} messages was compacted]"

    # Rebuild messages
    result = []
    if system_msg:
        result.append(system_msg)
    result.append({
        "role": "user",
        "content": f"[Conversation summary]: {summary}",
    })
    result.append({
        "role": "assistant",
        "content": "Understood. I have the context from our previous conversation. How can I continue helping you?",
    })
    result.extend(tail)
    return result
