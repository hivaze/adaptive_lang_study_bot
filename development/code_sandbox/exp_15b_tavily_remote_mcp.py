"""Experiment 15b: Test Tavily remote MCP endpoint via Claude Agent SDK.

Tests the remote MCP approach: SDK connects to Tavily's hosted MCP server.
Discovers available tools, their names, and output format.
"""

import asyncio
import os

from shared import Log, extract_text, extract_result, load_env

load_env()

log = Log("exp_15b_tavily_remote_mcp")


async def main():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock, ToolUseBlock, ResultMessage

    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        log("ERROR: TAVILY_API_KEY not set in .env")
        return

    tavily_url = f"https://mcp.tavily.com/mcp/?tavilyApiKey={api_key}"

    log.sep("Test 1: Remote MCP — discover Tavily tools")
    log(f"URL: {tavily_url[:50]}...")

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5",
        max_turns=5,
        thinking={"type": "disabled"},
        effort="low",
        mcp_servers={
            "tavily": {
                "type": "http",
                "url": tavily_url,
            },
        },
        allowed_tools=["mcp__tavily__*"],
        permission_mode="bypassPermissions",
        system_prompt=(
            "You have access to Tavily web search tools. "
            "Search for recent news about learning Spanish. "
            "Report back what you found."
        ),
    )

    try:
        async with ClaudeSDKClient(options) as client:
            await client.query("Search for recent news articles about learning Spanish language")

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            log(f"TEXT: {block.text[:500]}")
                        elif isinstance(block, ToolUseBlock):
                            log(f"TOOL USE: name={block.name}")
                            log(f"  Input: {str(block.input)[:500]}")
                elif isinstance(msg, ResultMessage):
                    log(f"\nRESULT:")
                    log(f"  Cost: ${msg.total_cost_usd}")
                    log(f"  Turns: {msg.num_turns}")
                    log(f"  Error: {msg.is_error}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())

    log.sep("Done")
    log.close()


if __name__ == "__main__":
    asyncio.run(main())
