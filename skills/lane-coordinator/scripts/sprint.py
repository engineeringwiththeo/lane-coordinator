#!/usr/bin/env python3
"""End-of-sprint routing review. Suggests tier moves per the rules in references/metrics.md.

Usage: python3 -I sprint.py [--repo PATH] [--today YYYY-MM-DD] [--apply]
--apply writes accepted moves into routing.json overrides (review the suggestions first).
"""
import datetime as dt, json, os, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import state_dir, load_json, load_ledger, resolve, shift


def arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else None


sd = state_dir(arg("--repo"))
rpath = os.path.join(sd, "routing.json")
routing = load_json(rpath)
if routing is None:
    sys.exit(f"no routing.json in {sd}")
cfg = load_json(os.path.join(sd, "config.json"), {})
today = dt.date.fromisoformat(arg("--today") or dt.date.today().isoformat())
since = (today - dt.timedelta(days=int(cfg.get("window_days", 28)))).isoformat()

tickets = [r for r in load_ledger(sd) if r.get("kind") == "ticket" and r.get("date", "") >= since]


def stats(rows):
    n = len(rows)
    fp = sum(1 for r in rows if r.get("first_pass")) / n if n else 0
    costs = [r["cost_usd"] for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    return {"n": n, "first_pass": round(fp, 2),
            "reverts": sum(1 for r in rows if r.get("reverted_7d") is True),
            "unchecked_reverts": sum(1 for r in rows if r.get("reverted_7d") is None),
            "cost_per": round(sum(costs) / len(costs), 2) if costs else None}


moves = []
for ttype in sorted({r.get("type") for r in tickets if r.get("type")}):
    cur = resolve(routing, ttype)
    cheaper = shift(cur, -1)
    on_cur = [r for r in tickets if r.get("type") == ttype and r.get("model") == cur]
    on_cheap = [r for r in tickets if r.get("type") == ttype and r.get("model") == cheaper] if cheaper != cur else []
    sc, sk = stats(on_cur), stats(on_cheap)
    line = {"type": ttype, "current": cur, "on_current": sc, "on_cheaper": sk if on_cheap else None}
    if sk["n"] >= 5 and sk["first_pass"] >= 0.8 and sk["reverts"] == 0 and \
            (sk["cost_per"] is None or sc["cost_per"] is None or sk["cost_per"] < sc["cost_per"]):
        line["suggest"] = f"DOWN -> {cheaper}"
        if sk["unchecked_reverts"]:
            line["warn"] = f"{sk['unchecked_reverts']} cheaper-tier tickets have reverted_7d unset"
        moves.append((ttype, cheaper))
    elif sc["n"] >= 5:
        last5 = on_cur[-5:]
        esc = sum(1 for r in last5 if (r.get("escalations") or 0) > 0)
        if esc >= 2 or sc["first_pass"] < 0.5:
            up = shift(cur, 1)
            if up != cur:
                line["suggest"] = f"UP -> {up} (escalated {esc}/5, first_pass {sc['first_pass']})"
                moves.append((ttype, up))
    print(json.dumps(line))

# Estimate misses: actual minutes outside 0.5x-2x of the size's median.
by_size = {}
for r in tickets:
    if isinstance(r.get("actual_minutes"), (int, float)) and r.get("est_size"):
        by_size.setdefault(r["est_size"], []).append(r)
for size, rows in sorted(by_size.items()):
    med = statistics.median(r["actual_minutes"] for r in rows)
    for r in rows:
        if med and not (0.5 * med <= r["actual_minutes"] <= 2 * med):
            print(json.dumps({"estimate_miss": r.get("ticket"), "size": size,
                              "minutes": r["actual_minutes"], "median": med}))

unchecked = [r.get("ticket") for r in tickets if r.get("reverted_7d") is None
             and r.get("date", "9999") <= (today - dt.timedelta(days=7)).isoformat()]
if unchecked:
    print(json.dumps({"set_reverted_7d_for": unchecked}))

if "--apply" in sys.argv and moves:
    for ttype, model in moves:
        routing.setdefault("overrides", {})[ttype] = model
    routing["updated"] = today.isoformat()
    with open(rpath, "w") as fh:
        json.dump(routing, fh, indent=2)
        fh.write("\n")
    print(json.dumps({"applied": moves}))
