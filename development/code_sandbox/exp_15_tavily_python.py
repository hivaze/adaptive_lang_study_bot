"""Experiment 15a: Test tavily-python library directly.

Tests AsyncTavilyClient.search() with various queries to understand
output format, response structure, and error handling.
"""

import asyncio
import json
import os
import sys

from shared import Log, load_env

load_env()

log = Log("exp_15_tavily_python")


async def main():
    from tavily import AsyncTavilyClient

    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        log("ERROR: TAVILY_API_KEY not set in .env")
        return

    client = AsyncTavilyClient(api_key=api_key)

    # --- Test 1: Basic general search ---
    log.sep("Test 1: Basic general search")
    try:
        result = await client.search(
            query="Spanish culture traditions",
            max_results=3,
            include_answer=True,
        )
        log(f"Type: {type(result)}")
        log(f"Keys: {list(result.keys())}")
        log(f"Answer: {result.get('answer', 'N/A')[:200]}")
        log(f"Results count: {len(result.get('results', []))}")
        for i, r in enumerate(result.get("results", [])):
            log(f"\n  Result {i}:")
            log(f"    Keys: {list(r.keys())}")
            log(f"    Title: {r.get('title', 'N/A')}")
            log(f"    URL: {r.get('url', 'N/A')}")
            log(f"    Content (first 200): {r.get('content', 'N/A')[:200]}")
            log(f"    Score: {r.get('score', 'N/A')}")
        log(f"\nFull response JSON:\n{json.dumps(result, indent=2, default=str)[:3000]}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 2: News topic ---
    log.sep("Test 2: News search (topic='news')")
    try:
        result = await client.search(
            query="technology news in German",
            topic="news",
            max_results=3,
            include_answer=True,
        )
        log(f"Answer: {result.get('answer', 'N/A')[:200]}")
        log(f"Results count: {len(result.get('results', []))}")
        for i, r in enumerate(result.get("results", [])):
            log(f"  Result {i}: {r.get('title', 'N/A')[:80]}")
            log(f"    Content: {r.get('content', 'N/A')[:200]}")
            log(f"    Published date: {r.get('published_date', 'N/A')}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 3: Language-specific search ---
    log.sep("Test 3: Search for content IN target language")
    try:
        result = await client.search(
            query="noticias sobre tecnología en español",
            topic="news",
            max_results=3,
            include_answer=True,
        )
        log(f"Answer: {result.get('answer', 'N/A')[:300]}")
        for i, r in enumerate(result.get("results", [])):
            log(f"  Result {i}: {r.get('title', 'N/A')[:100]}")
            log(f"    URL: {r.get('url', 'N/A')}")
            log(f"    Content: {r.get('content', 'N/A')[:300]}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 4: include_answer=False ---
    log.sep("Test 4: Without answer (include_answer=False)")
    try:
        result = await client.search(
            query="French cooking vocabulary",
            max_results=2,
            include_answer=False,
        )
        log(f"Keys: {list(result.keys())}")
        log(f"Answer field present: {'answer' in result}")
        log(f"Answer value: {repr(result.get('answer'))}")
        log(f"Results count: {len(result.get('results', []))}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 5: search_depth advanced ---
    log.sep("Test 5: Advanced search depth")
    try:
        result = await client.search(
            query="learn Italian through news articles",
            search_depth="advanced",
            max_results=2,
            include_answer=True,
        )
        log(f"Answer: {result.get('answer', 'N/A')[:300]}")
        for i, r in enumerate(result.get("results", [])):
            log(f"  Result {i}: {r.get('title', 'N/A')[:80]}")
            content = r.get("content", "")
            log(f"    Content length: {len(content)} chars")
            log(f"    Content preview: {content[:300]}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 6: Error handling - timeout ---
    log.sep("Test 6: Timeout handling")
    try:
        result = await asyncio.wait_for(
            client.search(query="test query", max_results=1),
            timeout=0.001,  # Extremely short timeout to trigger
        )
        log("Unexpectedly succeeded")
    except asyncio.TimeoutError:
        log("TimeoutError caught correctly")
    except Exception as e:
        log(f"Other error: {type(e).__name__}: {e}")

    log.sep("Done")
    log.close()


if __name__ == "__main__":
    asyncio.run(main())
