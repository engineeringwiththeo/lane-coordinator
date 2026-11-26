"""Shared helpers: locate the per-project state dir and load its files."""
import json, os, re, subprocess

TIERS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]  # cheapest first


def repo_root(path=None):
    path = path or os.environ.get("LANE_REPO") or os.getcwd()
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, check=True).stdout.strip()
        # --git-common-dir resolves worktrees back to the main checkout's .git
        if out.endswith("/.git"):
            return out[: -len("/.git")]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return os.path.abspath(path)


def project_dir(path=None):
    slug = re.sub(r"[^A-Za-z0-9]", "-", repo_root(path))
    return os.path.expanduser(f"~/.claude/projects/{slug}")


def state_dir(path=None):
    return os.path.join(project_dir(path), "metrics")


def load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def load_ledger(sd):
    rows = []
    try:
        with open(os.path.join(sd, "ledger.jsonl")) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def tier(model):
    return TIERS.index(model) if model in TIERS else None


def shift(model, step):
    i = tier(model)
    if i is None:
        return model
    return TIERS[max(0, min(len(TIERS) - 1, i + step))]


def resolve(routing, ticket_type):
    """Routed model for a type, before exploration."""
    if ticket_type in routing.get("overrides", {}):
        return routing["overrides"][ticket_type]
    size, _kind, stack = (ticket_type.split(":") + ["", "", ""])[:3]
    model = routing["size_default"].get(size, TIERS[1])
    floor = routing.get("stack_floor", {}).get(stack)
    if floor and tier(floor) is not None and tier(model) is not None and tier(floor) > tier(model):
        model = floor
    return model
