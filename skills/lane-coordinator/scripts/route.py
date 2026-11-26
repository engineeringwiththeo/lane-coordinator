#!/usr/bin/env python3
"""Pick the model for a ticket.

Usage: python3 -I route.py <size:kind:stack> [--issue N] [--repo PATH]
       python3 -I route.py --role ci_watcher|helper|coordinator|planner|designer|reviewer
Prints: model=<id> routed=<id> explore=<true|false>
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import state_dir, load_json, resolve, shift


def arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else None


sd = state_dir(arg("--repo"))
routing = load_json(os.path.join(sd, "routing.json"))
if routing is None:
    sys.exit(f"no routing.json in {sd}; run init.sh first")
cfg = load_json(os.path.join(sd, "config.json"), {})

role = arg("--role")
if role:
    if role not in routing["roles"]:
        sys.exit(f"unknown role {role}; have: {', '.join(routing['roles'])}")
    print(f"model={routing['roles'][role]}")
    sys.exit(0)

ttype = sys.argv[1]
routed = resolve(routing, ttype)
issue = arg("--issue")
explore = (ttype.split(":")[0] in ("S", "M") and issue is not None
           and int(issue) % int(cfg.get("exploration_modulo", 10)) == 0)
model = shift(routed, -1) if explore else routed
if model == routed:
    explore = False  # already at the cheapest tier
print(f"model={model} routed={routed} explore={'true' if explore else 'false'}")
