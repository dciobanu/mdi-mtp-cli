"""MTP domain logic, ported from the tracker's own app scripts.

Sources (read from the site via RPC): /scripts/mtp-config (goals, statuses,
fields), /scripts/mtp-app (trainee list + filters), /scripts/milestone
(milestone table and edit form), /scripts/data-table (what Save posts).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .store import Store

GROUP = "/mdi/groups/mtp-all-access"
STRUCTURE_PAGE = "/mdi/structure"

STATUS_OPTIONS = {
    1: "Not yet started",
    2: "In progress",
    3: "Completed program",
    4: "Paused before completing",
    5: "Left before completing",
}

HOW_DONE_OPTIONS = {
    1: "Not yet",
    2: "Done as part of MTP",
    3: "Learned elsewhere",
    4: "Waived by team",
}

# Order matters (it's the display order); ids do not match positions.
GOALS = [
    {"id": 1, "name": "Join MDI", "aliases": []},
    {"id": 2, "name": "Agree to uphold the Code of Honor", "aliases": []},
    {"id": 3, "name": "Agree to Team Standards", "aliases": ["Agree to uphold your team's standards"]},
    {"id": 4, "name": "Commitment Ceremony", "aliases": ["Participant completes the Commitment ceremony"]},
    {"id": 5, "name": "Commit to a measurable goal", "aliases": []},
    {"id": 7, "name": "Deliver a Teach", "aliases": []},
    {"id": 8, "name": "MDI Vision and Mission", "aliases": ["MDI Vision & Mission", "MDI Vision, Mission"]},
    {"id": 9, "name": "Study the Code of Honor", "aliases": ["Code of Honour", "Code of Honor", "Study the Code of Honour"]},
    {"id": 10, "name": "Learn to use the circle", "aliases": []},
    {"id": 6, "name": "Learn CPR", "aliases": []},
    {"id": 11, "name": "Lead Fun at a Team Meeting", "aliases": ["Lead fun at a meeting", "Lead fun at your team meeting"]},
    {"id": 12, "name": "Lead Ceremony at a Team Meeting", "aliases": ["Lead Ceremony at a meeting", "Lead ceremony at your team meeting"]},
    {"id": 13, "name": "Core Team Face to Face", "aliases": ["Attend a Core Team face-to-face meeting", "Attend core team face-to-face"]},
    {"id": 14, "name": "Lead Team Meeting", "aliases": ["Lead a team meeting"]},
    {"id": 15, "name": "Trust and Reveal", "aliases": ["Trust and reveal with your team"]},
    {"id": 17, "name": "Optional Milestone 1", "aliases": []},
    {"id": 18, "name": "Optional Milestone 2", "aliases": []},
    {"id": 16, "name": "MTP Completion Ceremony", "aliases": ["Completion ceremony"]},
    {"id": 19, "name": "Lead a meal", "aliases": [], "optional": True},
    {"id": 20, "name": "Build fire with team", "aliases": [], "optional": True},
    {"id": 21, "name": "Do something special for team", "aliases": [], "optional": True},
]
GOALS_BY_ID = {g["id"]: g for g in GOALS}
COMPLETION_GOAL = 16

# Editable fields as the UI labels them -> record keys.
PERSON_FIELDS = {"team": "Team", "lastName": "Last Name", "firstName": "First Name",
                 "mentor": "Mentor Name", "status": "Status", "note": "Note"}
EVENT_FIELDS = {"howDone": "Complete?", "plannedDate": "Planned Date",
                "completedDate": "Date Completed", "note": "Note"}


class NotFound(LookupError):
    pass


def is_done(event: dict) -> bool:
    if event.get("complete") or event.get("done"):  # legacy flags
        return True
    how = event.get("howDone")
    return isinstance(how, (int, float)) and how >= 2


def display_name(rec: dict) -> str:
    last, first = rec.get("lastName"), rec.get("firstName")
    if not last:
        return first or "no name information"
    return f"{last}, {first}" if first else last


def goal_title(goal_id) -> str:
    for n, g in enumerate(GOALS, 1):
        if g["id"] == goal_id:
            return f"{n}. {g['name']}"
    return ""


def date_str(value) -> str:
    if not value:
        return ""
    return str(value)[:10]


def _parse_date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


@dataclass
class Structure:
    teams: list[dict]
    regions: dict[str, str]

    @classmethod
    def from_page(cls, pg: dict) -> "Structure":
        return cls(teams=pg.get("teamsSorted") or [], regions=pg.get("regions") or {})

    def team(self, num) -> dict | None:
        try:
            num = int(num)
        except (TypeError, ValueError):
            return None
        for t in self.teams:
            if t["id"] == num:
                return t
        if num == 99001:
            return {"name": "(team 99001)", "id": 99001}
        return None

    def divisions(self) -> list[tuple[str, str]]:
        """(value, label) pairs, as offered by the Division dropdown."""
        opts: dict[str, str] = {}
        for key, value in self.regions.items():
            opts["*" + key] = f"All {value} Divisions"
        for t in self.teams:
            div = t.get("divisionName")
            if div and div not in opts:
                region = self.regions.get(t.get("regionId"), "")
                opts[div] = f"{div} ({region})" if region else div
        return [("", "All Divisions")] + sorted(opts.items())

    def filtered_out(self, team: dict, division: str | None) -> bool:
        return bool(team and division and division != "all"
                    and team.get("divisionName") != division
                    and "*" + str(team.get("regionId")) != division)

    def resolve_division(self, text: str | None) -> str | None:
        if not text:
            return None
        values = [v for v, _ in self.divisions() if v]
        low = text.lower()
        for v in values:
            if v.lower() == low:
                return v
        for key, name in self.regions.items():  # region name -> "*<code>"
            if name.lower() == low:
                return "*" + key
        hits = [v for v in values if not v.startswith("*") and low in v.lower()]
        if len(hits) == 1:
            return hits[0]
        raise NotFound(f"unknown division {text!r}" + (f"; did you mean: {', '.join(hits)}" if hits else
                                                           "; run `mtp divisions` for the list"))

    def resolve_team(self, text: str, division: str | None = None) -> dict:
        if str(text).isdigit():
            t = self.team(text)
            if t:
                return t
        low = str(text).lower()
        pool = [t for t in self.teams if not self.filtered_out(t, division)]
        exact = [t for t in pool if t["name"].lower() == low]
        hits = exact or [t for t in pool if low in t["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise NotFound(f"no team matches {text!r}")
        raise NotFound(f"team {text!r} is ambiguous: " +
                           ", ".join(f"{t['name']} ({t.get('divisionName')}, id {t['id']})" for t in hits))


@dataclass
class Milestone:
    position: int
    goal: dict
    event_id: str | None = None
    event: dict | None = None

    @property
    def done(self) -> bool:
        return bool(self.event) and is_done(self.event)

    @property
    def how_done(self) -> str:
        if not self.event:
            return "No data"
        how = self.event.get("howDone")
        if how is None:
            return "Done (legacy)" if is_done(self.event) else "No data"
        return HOW_DONE_OPTIONS.get(how, str(how))

    @property
    def data_entry_error(self) -> bool:
        return bool(self.event and self.event.get("completedDate") and not self.done)

    def as_dict(self) -> dict:
        e = self.event or {}
        return {
            "position": self.position,
            "goalId": self.goal["id"],
            "milestone": self.goal["name"],
            "optional": bool(self.goal.get("optional")),
            "done": self.done,
            "howDone": self.how_done,
            "completedDate": date_str(e.get("completedDate")) or None,
            "plannedDate": date_str(e.get("plannedDate")) or None,
            "note": e.get("note") or None,
            "eventId": self.event_id,
            "dataEntryError": self.data_entry_error,
        }


@dataclass
class Trainee:
    id: str
    record: dict
    team: dict
    region: str
    events: list[dict] = field(default_factory=list)  # completed ones, oldest first

    @property
    def name(self) -> str:
        return display_name(self.record)

    @property
    def status_code(self):
        return self.record.get("status")

    @property
    def status(self) -> str:
        return STATUS_OPTIONS.get(self.status_code, "no data")

    @property
    def latest(self) -> dict | None:
        return self.events[-1] if self.events else None

    def as_dict(self) -> dict:
        latest = self.latest or {}
        return {
            "id": self.id,
            "name": self.name,
            "lastName": self.record.get("lastName"),
            "firstName": self.record.get("firstName"),
            "team": self.team.get("name"),
            "teamId": self.team.get("id"),
            "division": self.team.get("divisionName"),
            "region": self.region,
            "mentor": self.record.get("mentor"),
            "status": self.status,
            "statusCode": self.status_code,
            "note": self.record.get("note") or None,
            "doneCount": len(self.events),
            "latestDate": date_str(latest.get("completedDate")) or None,
            "latestMilestone": "(complete)" if self.status_code == 3 else goal_title(latest.get("goal")),
        }


class Tracker:
    def __init__(self, store: Store, structure: Structure):
        self.store = store
        self.structure = structure
        self._events = store.subject_query({"isMTP": True, "goal": {"exists": True}})

    def trainees(self, status="2", division: str | None = None, text: str | None = None,
                 include_removed: bool = False) -> tuple[list[Trainee], dict]:
        """Mirror of findTrainees() in /scripts/mtp-app: the main list and its status counts."""
        people = self.store.subject_query({"isMTP": True, "lastName": {"exists": True}},
                                          include_removed=include_removed)
        events_by_person: dict[str, list[dict]] = {}
        for ev in self._events.values():
            events_by_person.setdefault(ev.get("person"), []).append(ev)

        counts: dict = {}
        out: list[Trainee] = []
        for pid, rec in people.items():
            team = self.structure.team(rec.get("teamNumber") or rec.get("teamId") or rec.get("team"))
            if not team or self.structure.filtered_out(team, division):
                continue
            rec = dict(rec)
            done = [e for e in events_by_person.get(pid, []) if is_done(e)]
            if any(e.get("goal") == COMPLETION_GOAL for e in done) and not rec.get("status"):
                rec["status"] = 3
            if not rec.get("status"):
                rec["status"] = "other"
            counts["all"] = counts.get("all", 0) + 1
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            if status != "all" and str(status) != str(rec["status"]):
                continue
            done.sort(key=lambda e: _parse_date(e.get("completedDate")) or dt.date.min)
            t = Trainee(pid, rec, team, self.structure.regions.get(team.get("regionId"), ""), done)
            if text:
                s = text.lower()
                hay = [t.name, team.get("name") or "", team.get("divisionName") or "", t.region]
                if not any(s in h.lower() for h in hay):
                    continue
            out.append(t)
        out.sort(key=lambda t: t.name.lower())
        return out, counts

    def find_trainee(self, query: str, division: str | None = None) -> Trainee:
        """Resolve a trainee by subject id or by (part of) their name, across all statuses."""
        everyone, _ = self.trainees(status="all", division=division)
        for t in everyone:
            if query == t.id or query in (t.record.get("subjectPages") or []):
                return t
        low = query.lower().replace(",", " ")
        words = low.split()
        exact = [t for t in everyone if t.name.lower() == query.lower()
                 or f"{t.record.get('firstName', '')} {t.record.get('lastName', '')}".lower().strip() == low.strip()]
        hits = exact or [t for t in everyone if all(w in t.name.lower() for w in words)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise NotFound(f"no trainee matches {query!r}")
        lines = [f"  {t.name}  [{t.team.get('name')}, {t.team.get('divisionName')}, {t.status}]  {t.id}" for t in hits]
        raise NotFound(f"{query!r} matches {len(hits)} trainees; use a fuller name or the id:\n" + "\n".join(lines))

    def milestones(self, trainee: Trainee) -> list[Milestone]:
        """Mirror of MilestoneList(): one row per goal, optional goals only when present."""
        by_goal: dict[int, tuple[str, dict]] = {}
        for eid, ev in self._events.items():
            if ev.get("person") == trainee.id:
                by_goal[ev.get("goal")] = (eid, ev)
        rows = []
        for pos, g in enumerate(GOALS, 1):
            eid, ev = by_goal.get(g["id"], (None, None))
            if g.get("optional") and not ev:
                continue
            rows.append(Milestone(pos, g, eid, ev))
        return rows


_STOPWORDS = {"a", "an", "the", "at", "to", "of", "with", "your", "my"}


def resolve_goal(text: str) -> dict:
    """Accept a position (1-21, as numbered in the UI) or a name/alias fragment."""
    text = str(text).strip()
    if text.isdigit():
        n = int(text)
        if 1 <= n <= len(GOALS):
            return GOALS[n - 1]
        raise NotFound(f"milestone number must be 1-{len(GOALS)}")
    low = text.lower()
    exact = [g for g in GOALS if g["name"].lower() == low]
    # An alias only wins outright if the phrase isn't also part of another milestone's name
    # ("code of honor" is an alias of #8 but also names #2, so it stays ambiguous).
    hits = exact or [g for g in GOALS if low in g["name"].lower() or any(low in a.lower() for a in g["aliases"])]
    if not hits:
        # Looser: every significant word, minus "-ing" ("leading fun", "delivering the teach").
        words = [w[:-3] if w.endswith("ing") and len(w) > 5 else w
                 for w in re.findall(r"[a-z0-9]+", low) if w not in _STOPWORDS]
        hits = [g for g in GOALS if words and all(
            any(w in t.lower() for t in [g["name"], *g["aliases"]]) for w in words)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise NotFound(f"no milestone matches {text!r}; run `mtp milestones` for the list")
    raise NotFound(f"milestone {text!r} is ambiguous: " +
                       "; ".join(f"{GOALS.index(g) + 1}. {g['name']}" for g in hits))


def _with_permit(page: dict) -> dict:
    page["permit"] = {"read": {GROUP: True}}
    return page


def person_update_page(trainee: Trainee, changes: dict) -> dict:
    """The page the Save button posts when editing a trainee field (DataTable edit mode)."""
    page = {"subjectPage": trainee.id}
    if trainee.record.get("isMTP") is not None:
        page["isMTP"] = trainee.record["isMTP"]
    page.update(changes)
    return _with_permit(page)


def new_person_page(fields: dict) -> dict:
    """The page posted by "Add Trainee"."""
    page = {"team": None, "lastName": "", "firstName": "", "mentor": "", "status": None, "note": ""}
    page.update(fields)
    page["isMTP"] = True
    return _with_permit(page)


def milestone_page(trainee: Trainee, ms: Milestone, changes: dict, today: dt.date | None = None) -> dict:
    """The page posted when saving a milestone: an overlay on an existing event, or a new event."""
    if ms.event_id:
        page = {"subjectPage": ms.event_id}
        for k in ("isMTP", "goal", "person"):
            if ms.event.get(k) is not None:
                page[k] = ms.event[k]
        page.update(changes)
        return _with_permit(page)
    # New event: the UI form defaults to "Done as part of MTP", completed today.
    how = changes.get("howDone", 2)
    completed = changes.get("completedDate")
    if completed is None:
        completed = (today or dt.date.today()).isoformat() if isinstance(how, int) and how >= 2 else ""
    page = {
        "howDone": how,
        "plannedDate": changes.get("plannedDate", ""),
        "completedDate": completed,
        "note": changes.get("note", ""),
        "isMTP": True,
        "goal": ms.goal["id"],
        "person": trainee.id,
    }
    return _with_permit(page)


def removal_page(subject_id: str, removed: bool) -> dict:
    return _with_permit({"subjectPage": subject_id, "isRemoved": removed})
