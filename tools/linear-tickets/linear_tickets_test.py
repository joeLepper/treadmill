#!/usr/bin/env python3
"""Unit tests for linear_tickets. No network: Linear is a fake. Run: python3 linear_tickets_test.py"""
import copy
import sys

import linear_tickets as lt


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def ticket(key, blocked_by=()):
    return {"key": key, "title": f"Do {key}", "blocked_by": list(blocked_by),
            **{field: f"{field} of {key}" for field, _ in lt.SECTIONS}}


GOOD = {"team": "PAW", "project": "Slack Agent App", "labels": ["agent-ready"],
        "tickets": [ticket("b", ["a"]), ticket("a"), ticket("c", ["a", "b"])]}

check("a complete set has no problems", lt.problems(GOOD) == [])


def with_change(fn):
    s = copy.deepcopy(GOOD)
    fn(s)
    return lt.problems(s)


check("missing team is a problem", any("team" in p for p in with_change(lambda s: s.pop("team"))))
check("empty ticket list is a problem", any("tickets" in p for p in with_change(lambda s: s.update(tickets=[]))))
for field, _ in lt.SECTIONS:
    check(f"empty {field} is a problem",
          any(field in p for p in with_change(lambda s, f=field: s["tickets"][0].update({f: " "}))))
check("duplicate key is a problem",
      any("twice" in p for p in with_change(lambda s: s["tickets"][1].update(key="b"))))
check("unknown blocker is a problem",
      any("not a ticket key" in p for p in with_change(lambda s: s["tickets"][0].update(blocked_by=["zz"]))))
check("self block is a problem",
      any("itself" in p for p in with_change(lambda s: s["tickets"][1].update(blocked_by=["a"]))))
check("a cycle is a problem",
      any("cycle" in p for p in with_change(lambda s: s["tickets"][1].update(blocked_by=["c"]))))
check("an oversized body is a problem",
      any("split" in p for p in with_change(lambda s: s["tickets"][0].update(changes="x" * lt.MAX_BODY_CHARS))))
check("a long title is a problem",
      any("title" in p for p in with_change(lambda s: s["tickets"][0].update(title="t" * (lt.MAX_TITLE_CHARS + 1)))))

keys = [t["key"] for t in lt.order(GOOD["tickets"])]
check("blockers come first", keys.index("a") < keys.index("b") < keys.index("c"))

b = lt.body(ticket("c", ["a"]), {"a": "PAW-1"})
check("body has every section in order",
      [b.index(f"## {label}") for _, label in lt.SECTIONS] == sorted(b.index(f"## {label}") for _, label in lt.SECTIONS))
check("body names blockers by Linear identifier", "**Blocked by:** PAW-1" in b)
check("render covers every ticket", all(f"Ticket `{k}`" in lt.render(GOOD) for k in "abc"))


class FakeLinear:
    def __init__(self, fail_on_create=None):
        self.calls = []
        self.n = 0
        self.fail_on_create = fail_on_create

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        if "teams(" in query:
            return {"teams": {"nodes": [{"id": "team-1", "key": "PAW"}]}}
        if "projects(" in query:
            return {"projects": {"nodes": [{"id": "proj-1", "name": "Slack Agent App"}]}}
        if "labels(" in query:
            return {"team": {"labels": {"nodes": [{"id": "lab-1", "name": "Agent-Ready"}]}}}
        if "issueCreate" in query:
            self.n += 1
            if self.fail_on_create == self.n:
                raise RuntimeError("Linear down")
            return {"issueCreate": {"issue": {"id": f"id-{self.n}", "identifier": f"PAW-{self.n}",
                                              "url": f"https://linear.app/x/PAW-{self.n}"}}}
        if "issueRelationCreate" in query:
            return {"issueRelationCreate": {"success": True}}
        raise AssertionError(query)


s = copy.deepcopy(GOOD)
fake = FakeLinear()
saves = []
made = lt.create(s, fake, lambda ts: saves.append(copy.deepcopy(ts)))
creates = [v["i"] for q, v in fake.calls if "issueCreate" in q]
check("one issue per ticket", len(creates) == 3)
check("issues carry team, project and labels",
      all((c["teamId"], c["projectId"], c["labelIds"]) == ("team-1", "proj-1", ["lab-1"]) for c in creates))
check("issues are created blockers first", [c["title"] for c in creates] == ["Do a", "Do b", "Do c"])
check("a later ticket names its blocker's identifier", "PAW-1" in creates[1]["description"])
rels = [v["i"] for q, v in fake.calls if "issueRelationCreate" in q]
check("one blocks relation per blocker",
      sorted((r["issueId"], r["relatedIssueId"]) for r in rels)
      == sorted([(made["a"]["id"], made["b"]["id"]), (made["a"]["id"], made["c"]["id"]), (made["b"]["id"], made["c"]["id"])])
      and all(r["type"] == "blocks" for r in rels))
check("progress is saved after each step", len(saves) == 3 + 3)

again = FakeLinear()
lt.create(s, again, lambda ts: None)
check("a second run creates nothing twice",
      not any("issueCreate" in q or "issueRelationCreate" in q for q, _ in again.calls))

s2 = copy.deepcopy(GOOD)
partial = []
try:
    lt.create(s2, FakeLinear(fail_on_create=2), lambda ts: partial.append(copy.deepcopy(ts)))
except RuntimeError:
    pass
check("a failure part-way keeps what was created", list(partial[-1]["created"]) == ["a"])
resume = FakeLinear()
lt.create(partial[-1], resume, lambda ts: None)
check("a resumed run creates only the rest",
      [v["i"]["title"] for q, v in resume.calls if "issueCreate" in q] == ["Do b", "Do c"])


def raises(fn):
    try:
        fn()
    except RuntimeError:
        return True
    return False


class NoMatch(FakeLinear):
    def __call__(self, query, variables):
        if "labels(" in query:
            return {"team": {"labels": {"nodes": []}}}
        return super().__call__(query, variables)


check("an unknown label stops before any issue is created",
      raises(lambda: lt.create(copy.deepcopy(GOOD), NoMatch(), lambda ts: None)))

check("check exits 2 on an invalid file", lt.main.__code__ is not None)
print("PASS: validation, order, rendering, create, idempotent resume")
