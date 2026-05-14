---
name: search-web
description: "Search the web for information. Use this to research topics, look up websites, find news, or gather information from the internet. Takes a search query and returns an AI-synthesized answer with cited sources."
metadata:
  monkeybot:
    requires:
      bins: ["python3"]
      env:
        - PERPLEXITY_API_KEY
---

# Web Search Skill

Search the internet using Perplexity AI (sonar model). Returns a synthesized answer plus source URLs.

## Usage

```bash
python3 skills/search-web/search_web.py --query "auriga os social media strategy" --num 5
python3 skills/search-web/search_web.py --query "AI news today" --recency day
```

## Arguments

- `--query` (required): The search query string
- `--num` (optional): Max source citations to show (default: 5, max: 20)
- `--recency` (optional): Time filter — `day`, `week`, `month`, or `year`

## Output

Returns a synthesized answer followed by a numbered list of source URLs.

## Setup

Requires one environment variable:
- `PERPLEXITY_API_KEY`: API key from https://www.perplexity.ai/settings/api
