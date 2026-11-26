#!/usr/bin/env python3
"""Sum token usage and API-equivalent cost from Claude Code agent transcripts (JSONL).

Usage: python3 -I usage.py <transcript.jsonl> [--since ISO] [--until ISO]
Prints one JSON object per model, plus a total. Cost is an estimate at Anthropic
first-party API list prices; a subscription plan bills differently.
"""
import json, sys

# $ per million tokens (cached 2026-11-03 from the claude-api skill).
# cache_write = 1.25x input (5-minute TTL); Haiku cache read assumed 0.1x input.
PRICES = {
    "claude-opus-5-5":   {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50,  "cache_read": 0.01, "cache_write": 0.125},
}

def main():
    args = sys.argv[1:]
    path = args[0]
    since = args[args.index("--since") + 1] if "--since" in args else None
    until = args[args.index("--until") + 1] if "--until" in args else None
    per = {}
    seen = set()
    first = last = None
    with open(path) as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            ts = row.get("timestamp")
            if since and ts and ts < since:
                continue
            if until and ts and ts > until:
                continue
            msg = row.get("message") or {}
            mid = msg.get("id") or row.get("requestId")
            if mid in seen:
                continue  # one transcript row per content block repeats the message's usage
            seen.add(mid)
            u = msg.get("usage") or {}
            model = msg.get("model") or row.get("requestedModel") or "unknown"
            p = per.setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "turns": 0})
            p["in"] += u.get("input_tokens", 0) or 0
            p["out"] += u.get("output_tokens", 0) or 0
            p["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            p["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            p["turns"] += 1
            first = first or ts
            last = ts or last
    total = 0.0
    for model, p in per.items():
        price = PRICES.get(model)
        cost = None
        if price:
            cost = sum(p[k] * price[k] for k in ("in", "out", "cache_read", "cache_write")) / 1e6
            total += cost
        print(json.dumps({"model": model, **p, "cost_usd": round(cost, 4) if cost is not None else None}))
    print(json.dumps({"total_cost_usd": round(total, 4), "first": first, "last": last}))

if __name__ == "__main__":
    main()
