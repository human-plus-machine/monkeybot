#!/usr/bin/env python3
"""Web search skill using Perplexity AI (sonar model)."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
MAX_RETRIES = 3


def search(query: str, num: int = 5, recency: str | None = None) -> str:
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        print("Error: PERPLEXITY_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": query}],
        "max_tokens": 1024,
    }
    if recency:
        payload["search_recency_filter"] = recency

    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(PERPLEXITY_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            citations = data.get("citations", [])

            lines = [f"Search results for: {query}\n", content]

            if citations:
                lines.append(f"\nSources ({min(len(citations), num)}):")
                for i, url in enumerate(citations[:num], 1):
                    lines.append(f"  {i}. {url}")

            return "\n".join(lines)

        except urllib.error.HTTPError as e:
            status = e.code
            body_err = e.read().decode()
            last_error = f"HTTP {status}: {body_err}"
            # Don't retry 4xx client errors
            if status < 500:
                break
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)

        except urllib.error.URLError as e:
            last_error = f"Network error: {e.reason}"
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)

    print(f"Error: Perplexity API failed — {last_error}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Search the web using Perplexity AI")
    parser.add_argument("--query", required=True, help="Search query")
    parser.add_argument("--num", type=int, default=5, help="Max source citations to show (1-20)")
    parser.add_argument(
        "--recency",
        choices=["day", "week", "month", "year"],
        help="Time filter for results",
    )
    args = parser.parse_args()

    try:
        print(search(args.query, args.num, args.recency))
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
