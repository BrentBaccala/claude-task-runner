"""
Experimental MCP server to test whether server-initiated notifications
surface to a running Claude Code conversation.

Exposes tools that cause the server to send `notifications/message`
(MCP LoggingMessageNotification) back to the client — immediately,
after a delay, or via an external trigger file.
"""
import asyncio
import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("NotifyTest")

TRIGGER_DIR = Path("/tmp/mcp-notify-test")
TRIGGER_DIR.mkdir(exist_ok=True)

LogLevel = Literal["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]


@mcp.tool()
async def notify_now(ctx: Context, message: str, level: LogLevel = "info") -> str:
    """
    Send an MCP log-message notification to the client immediately, then return.

    Args:
        message: The text to send as the notification payload.
        level: MCP log level (debug/info/notice/warning/error/critical/alert/emergency).

    Returns:
        Confirmation that the notification was sent.
    """
    await ctx.session.send_log_message(level=level, data=message, logger="notify-test")
    return f"sent notification (level={level}): {message!r}"


@mcp.tool()
async def notify_after(ctx: Context, message: str, delay_seconds: float, level: LogLevel = "info") -> str:
    """
    Schedule an MCP log-message notification to fire after delay_seconds.
    The tool returns immediately; the notification is sent later from a
    background task, so the client receives it outside any tool-call response.

    Args:
        message: The text to send as the notification payload.
        delay_seconds: How long to wait before sending.
        level: MCP log level.

    Returns:
        Confirmation that the notification is scheduled.
    """
    session = ctx.session

    async def _fire():
        try:
            await asyncio.sleep(delay_seconds)
            await session.send_log_message(level=level, data=message, logger="notify-test")
        except Exception as e:
            # Best-effort; log to stderr so we can see failures in the MCP log.
            import sys
            print(f"notify_after failed: {e!r}", file=sys.stderr)

    asyncio.create_task(_fire())
    return f"scheduled notification in {delay_seconds}s (level={level}): {message!r}"


@mcp.tool()
async def watch_trigger_file(ctx: Context, filename: str = "inbox", level: LogLevel = "info") -> str:
    """
    Start watching /tmp/mcp-notify-test/<filename>. Whenever the file is
    written to (or appended to), its new contents are sent as a notification.

    This lets an external process inject messages into the running conversation
    by simply writing to the file:
        echo 'hello from outside' >> /tmp/mcp-notify-test/inbox

    The tool returns immediately; watching continues in the background.

    Args:
        filename: File name inside /tmp/mcp-notify-test/ to watch.
        level: MCP log level for the notifications.

    Returns:
        Confirmation that the watcher started.
    """
    path = TRIGGER_DIR / filename
    path.touch(exist_ok=True)
    session = ctx.session

    async def _watch():
        last_size = path.stat().st_size
        while True:
            try:
                await asyncio.sleep(0.5)
                size = path.stat().st_size
                if size > last_size:
                    with path.open("rb") as f:
                        f.seek(last_size)
                        chunk = f.read(size - last_size).decode("utf-8", errors="replace")
                    last_size = size
                    for line in chunk.splitlines():
                        line = line.strip()
                        if line:
                            await session.send_log_message(level=level, data=line, logger="notify-test")
                elif size < last_size:
                    last_size = size  # file was truncated; resync
            except Exception as e:
                import sys
                print(f"watch_trigger_file failed: {e!r}", file=sys.stderr)
                return

    asyncio.create_task(_watch())
    return f"watching {path} (append lines to send notifications)"


@mcp.tool()
async def ask_user(ctx: Context, message: str) -> str:
    """
    Use MCP elicitation/create to ask the user for input during a tool call.
    Returns the user's answer (or a marker if declined/cancelled).
    """
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string", "description": "Your reply"}},
        "required": ["answer"],
    }
    result = await ctx.session.elicit_form(message=message, requestedSchema=schema)
    action = getattr(result, "action", None)
    content = getattr(result, "content", None)
    return f"action={action!r} content={content!r}"


@mcp.tool()
async def ask_model(ctx: Context, prompt: str, max_tokens: int = 256) -> str:
    """
    Use MCP sampling/createMessage to have the CLIENT run an LLM completion
    on behalf of the server. This forces a model turn inside the tool call.
    """
    from mcp import types as mcp_types
    messages = [
        mcp_types.SamplingMessage(
            role="user",
            content=mcp_types.TextContent(type="text", text=prompt),
        )
    ]
    result = await ctx.session.create_message(messages=messages, max_tokens=max_tokens)
    content = getattr(result, "content", None)
    text = getattr(content, "text", None) if content is not None else None
    model = getattr(result, "model", None)
    stop_reason = getattr(result, "stopReason", None)
    return f"model={model!r} stop_reason={stop_reason!r} text={text!r}"


if __name__ == "__main__":
    mcp.run()
