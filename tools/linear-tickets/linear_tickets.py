#!/usr/bin/env python3
"""Turn a plan's work units into Linear tickets that an Agent Runner can pick up.

The input is a ticket set (JSON). Each ticket carries the same six sections, so
an AR started from the ticket alone has its goal, starting point, changes, tests,
limits and finish line:

  {
    "team": "PAW",
    "project": "Slack Agent App",          # optional
    "labels": ["agent-ready"],             # optional, applied to every ticket
    "tickets": [
      {
        "key": "access-offer",             # local id, used by blocked_by
        "title": "Show Request access only when an Owner is in the workspace",
        "blocked_by": [],
        "goal": "...", "starting_point": "...", "changes": "...",
        "tests": "...", "not_in_scope": "...", "done_when": "..."
      }
    ]
  }

Commands:
  check  FILE            validate the set; exit 2 on any problem
  render FILE            print every ticket as Markdown (what `review` sends)
  review FILE [panel args...]
                         run the cross-model review panel on the rendered set
  create FILE [--dry-run]
                         create the tickets in Linear in dependency order, add
                         "blocks" relations, and write each identifier back into
                         FILE so a second run creates nothing twice

The Linear key comes from LINEAR_API_KEY or ~/.config/linear/api-key. It is
never printed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

LINEAR_URL = "https://api.linear.app/graphql"
KEY_FILE = os.path.expanduser("~/.config/linear/api-key")
PANEL = os.environ.get("REVIEW_PANEL_PY",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "model-review-panel", "panel.py"))

SECTIONS = [
    ("goal", "Goal"),
    ("starting_point", "Starting point"),
    ("changes", "Required changes"),
    ("tests", "Tests"),
    ("not_in_scope", "Not in scope"),
    ("done_when", "Done when"),
]
# An AR reads the whole ticket as its brief; past this a ticket is two tickets.
MAX_BODY_CHARS = 12000
MAX_TITLE_CHARS = 120


# --- validation --------------------------------------------------------------

def problems(ticket_set):
    """Every reason the set cannot be created, as plain sentences. Empty means valid."""
    out = []
    if not isinstance(ticket_set, dict):
        return ["the file must hold one JSON object"]
    if not str(ticket_set.get("team") or "").strip():
        out.append("`team` (the Linear team key, for example PAW) is missing")
    tickets = ticket_set.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        return out + ["`tickets` must be a non-empty list"]
    keys = [t.get("key") for t in tickets if isinstance(t, dict)]
    seen = set()
    for i, t in enumerate(tickets):
        if not isinstance(t, dict):
            out.append(f"ticket {i + 1} is not an object")
            continue
        name = t.get("key") or f"ticket {i + 1}"
        if not str(t.get("key") or "").strip():
            out.append(f"{name}: `key` is missing")
        elif t["key"] in seen:
            out.append(f"{name}: `key` is used twice")
        seen.add(t.get("key"))
        title = str(t.get("title") or "").strip()
        if not title:
            out.append(f"{name}: `title` is missing")
        elif len(title) > MAX_TITLE_CHARS:
            out.append(f"{name}: `title` is over {MAX_TITLE_CHARS} characters")
        for field, label in SECTIONS:
            if not str(t.get(field) or "").strip():
                out.append(f"{name}: section `{field}` ({label}) is empty")
        for dep in t.get("blocked_by") or []:
            if dep not in keys:
                out.append(f"{name}: blocked_by names `{dep}`, which is not a ticket key")
            elif dep == t.get("key"):
                out.append(f"{name}: a ticket cannot block itself")
        if len(body(t)) > MAX_BODY_CHARS:
            out.append(f"{name}: the body is over {MAX_BODY_CHARS} characters; split the ticket")
    if not out and order(tickets) is None:
        out.append("blocked_by has a cycle")
    return out


def order(tickets):
    """Tickets with every blocker before the tickets it blocks, or None on a cycle."""
    by_key = {t["key"]: t for t in tickets}
    done, out, visiting = set(), [], set()

    def visit(key):
        if key in done:
            return True
        if key in visiting:
            return False
        visiting.add(key)
        for dep in by_key[key].get("blocked_by") or []:
            if not visit(dep):
                return False
        visiting.discard(key)
        done.add(key)
        out.append(by_key[key])
        return True

    for t in tickets:
        if not visit(t["key"]):
            return None
    return out


# --- rendering ---------------------------------------------------------------

def body(ticket, identifiers=None):
    parts = []
    deps = ticket.get("blocked_by") or []
    if deps:
        names = [(identifiers or {}).get(d, d) for d in deps]
        parts.append("**Blocked by:** " + ", ".join(names) + ". Start only after these are merged.")
    for field, label in SECTIONS:
        parts.append(f"## {label}\n\n{str(ticket.get(field) or '').strip()}")
    return "\n\n".join(parts) + "\n"


def render(ticket_set):
    out = [f"# Ticket set for team {ticket_set.get('team')}"
           + (f", project {ticket_set['project']}" if ticket_set.get("project") else ""), ""]
    for t in order(ticket_set["tickets"]) or ticket_set["tickets"]:
        out.append(f"# Ticket `{t['key']}`: {t['title']}\n")
        out.append(body(t))
    return "\n".join(out)


# --- Linear ------------------------------------------------------------------

def linear_key():
    key = os.environ.get("LINEAR_API_KEY")
    if key:
        return key.strip()
    with open(KEY_FILE) as fh:
        key = fh.read().strip()
    if not key:
        raise RuntimeError(f"no Linear key: set LINEAR_API_KEY or fill {KEY_FILE}")
    return key


def graphql(query, variables, key):
    req = urllib.request.Request(
        LINEAR_URL, data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.load(resp)
    if out.get("errors"):
        raise RuntimeError("Linear: " + "; ".join(e.get("message", "?") for e in out["errors"]))
    return out["data"]


def resolve(ticket_set, gql):
    team = gql("query($k:String!){ teams(filter:{key:{eq:$k}}){ nodes{ id key } } }",
               {"k": ticket_set["team"]})["teams"]["nodes"]
    if len(team) != 1:
        raise RuntimeError(f"no single Linear team with key {ticket_set['team']}")
    ids = {"teamId": team[0]["id"]}
    if ticket_set.get("project"):
        projects = gql("query($n:String!){ projects(filter:{name:{eq:$n}}){ nodes{ id name } } }",
                       {"n": ticket_set["project"]})["projects"]["nodes"]
        if len(projects) != 1:
            raise RuntimeError(f"no single Linear project named {ticket_set['project']}")
        ids["projectId"] = projects[0]["id"]
    if ticket_set.get("labels"):
        labels = gql("query($t:String!){ team(id:$t){ labels(first:250){ nodes{ id name } } } }",
                     {"t": ids["teamId"]})["team"]["labels"]["nodes"]
        by_name = {label["name"].lower(): label["id"] for label in labels}
        missing = [n for n in ticket_set["labels"] if n.lower() not in by_name]
        if missing:
            raise RuntimeError(f"labels not in team {ticket_set['team']}: {', '.join(missing)}")
        ids["labelIds"] = [by_name[n.lower()] for n in ticket_set["labels"]]
    return ids


def create(ticket_set, gql, save):
    """Create what is not created yet, then every missing "blocks" relation.
    `save` is called after each ticket so a failure part-way loses nothing."""
    ids = resolve(ticket_set, gql)
    made = ticket_set.setdefault("created", {})
    for t in order(ticket_set["tickets"]):
        if t["key"] in made:
            continue
        identifiers = {k: v["identifier"] for k, v in made.items()}
        issue = gql(
            "mutation($i:IssueCreateInput!){ issueCreate(input:$i){ success issue{ id identifier url } } }",
            {"i": {**ids, "title": t["title"], "description": body(t, identifiers)}},
        )["issueCreate"]["issue"]
        made[t["key"]] = {"id": issue["id"], "identifier": issue["identifier"], "url": issue["url"],
                          "relations": []}
        save(ticket_set)
    for t in ticket_set["tickets"]:
        for dep in t.get("blocked_by") or []:
            if dep in made[t["key"]]["relations"]:
                continue
            gql("mutation($i:IssueRelationCreateInput!){ issueRelationCreate(input:$i){ success } }",
                {"i": {"issueId": made[dep]["id"], "relatedIssueId": made[t["key"]]["id"], "type": "blocks"}})
            made[t["key"]]["relations"].append(dep)
            save(ticket_set)
    return made


# --- CLI ---------------------------------------------------------------------

def load(path):
    with open(path) as fh:
        return json.load(fh)


def save_to(path):
    def save(ticket_set):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(ticket_set, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    return save


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan work units to AR-ready Linear tickets")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "render", "review", "create"):
        p = sub.add_parser(name)
        p.add_argument("file")
        if name == "create":
            p.add_argument("--dry-run", action="store_true", help="validate and resolve only")
    args, extra = ap.parse_known_args(argv)
    if extra and args.cmd != "review":
        ap.error(f"unrecognized arguments: {' '.join(extra)}")

    ticket_set = load(args.file)
    bad = problems(ticket_set)
    if bad:
        for line in bad:
            print(f"linear-tickets: {line}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        print(f"ok: {len(ticket_set['tickets'])} tickets for team {ticket_set['team']}")
        return 0
    if args.cmd == "render":
        print(render(ticket_set))
        return 0
    if args.cmd == "review":
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(render(ticket_set))
        try:
            cmd = [sys.executable, PANEL, "review", "--artifact", fh.name, "--kind", "plan",
                   "--author-family", "claude", "--min-cross-model", "2", *extra]
            return subprocess.call(cmd)
        finally:
            os.unlink(fh.name)
    key = linear_key()
    gql = lambda q, v: graphql(q, v, key)  # noqa: E731
    if args.dry_run:
        resolve(ticket_set, gql)
        print(f"dry run ok: {len(ticket_set['tickets'])} tickets resolve against team {ticket_set['team']}")
        return 0
    made = create(ticket_set, gql, save_to(args.file))
    for t in order(ticket_set["tickets"]):
        print(f"{made[t['key']]['identifier']}  {made[t['key']]['url']}  {t['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
