"""Experiment 15c: Test tavily-python extract feature.

Tests AsyncTavilyClient.extract() to understand output format for
fetching full page content from URLs.
"""

import asyncio
import json
import os

from shared import Log, load_env

load_env()

log = Log("exp_15c_tavily_extract")


async def main():
    from tavily import AsyncTavilyClient

    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        log("ERROR: TAVILY_API_KEY not set in .env")
        return

    client = AsyncTavilyClient(api_key=api_key)

    # --- Test 1: Extract from a known article URL ---
    log.sep("Test 1: Extract from a real article")
    try:
        result = await client.extract(
            urls=["https://www.spain.info/en/top/ten-traditions-spain/"],
        )
        log(f"Type: {type(result)}")
        log(f"Keys: {list(result.keys())}")
        results = result.get("results", [])
        log(f"Results count: {len(results)}")
        for i, r in enumerate(results):
            log(f"\n  Result {i}:")
            log(f"    Keys: {list(r.keys())}")
            log(f"    URL: {r.get('url', 'N/A')}")
            raw = r.get("raw_content", "")
            log(f"    Raw content length: {len(raw)} chars")
            log(f"    Raw content preview: {raw[:500]}")
        log(f"\nFull response (truncated):\n{json.dumps(result, indent=2, default=str)[:3000]}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 2: Extract from multiple URLs ---
    log.sep("Test 2: Extract from multiple URLs")
    try:
        result = await client.extract(
            urls=[
                "https://www.bbc.com/news",
                "https://edition.cnn.com",
            ],
        )
        results = result.get("results", [])
        log(f"Results count: {len(results)}")
        failed = result.get("failed_results", [])
        log(f"Failed count: {len(failed)}")
        for i, r in enumerate(results):
            log(f"  Result {i}: {r.get('url', 'N/A')}")
            raw = r.get("raw_content", "")
            log(f"    Content length: {len(raw)} chars")
            log(f"    Content preview: {raw[:300]}")
        for i, f in enumerate(failed):
            log(f"  Failed {i}: {f}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    # --- Test 3: Extract with timeout ---
    log.sep("Test 3: Timeout handling")
    try:
        result = await asyncio.wait_for(
            client.extract(urls=["https://httpbin.org/delay/30"]),
            timeout=0.001,
        )
        log("Unexpectedly succeeded")
    except asyncio.TimeoutError:
        log("TimeoutError caught correctly")
    except Exception as e:
        log(f"Other error: {type(e).__name__}: {e}")

    # --- Test 4: Extract from a non-existent URL ---
    log.sep("Test 4: Non-existent URL")
    try:
        result = await client.extract(
            urls=["https://thisdomaindoesnotexist12345.com/page"],
        )
        log(f"Keys: {list(result.keys())}")
        log(f"Results: {result.get('results', [])}")
        log(f"Failed: {result.get('failed_results', [])}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    log.sep("Done")
    log.close()


if __name__ == "__main__":
    asyncio.run(main())
