"""`mtp` command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import signal
import sys
from pathlib import Path

from . import model
from .model import (EVENT_FIELDS, GOALS, HOW_DONE_OPTIONS, PERSON_FIELDS, STATUS_OPTIONS,
                    NotFound, Structure, Tracker)
from .rpc import DEFAULT_SERVER, Client, RPCError
from .store import Store, crawl

STATUS_ALIASES = {
    "not-started": 1, "not-yet-started": 1, "in-progress": 2, "active": 2,
    "completed": 3, "complete": 3, "paused": 4, "left": 5,
}
HOW_DONE_ALIASES = {
    "not-yet": 1, "no": 1, "done": 2, "mtp": 2, "elsewhere": 3, "learned-elsewhere": 3,
    "waived": 4, "none": None, "no-data": None,
}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- config / session

def load_dotenv() -> None:
    """Read KEY=VALUE lines from the first .env found, without overriding the environment.

    Looks in the current directory, the source checkout (editable installs), then
    ~/.config/mtp-cli/.env (for `make install`ed copies run from anywhere).
    """
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mtp-cli" / ".env"
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env", config):
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
            return


def credentials() -> tuple[str, str]:
    user = os.environ.get("MTP_USERNAME") or os.environ.get("username")
    pw = os.environ.get("MTP_PASSWORD") or os.environ.get("password")
    if not user or not pw:
        raise UsageError("set MTP_USERNAME and MTP_PASSWORD (or username/password in .env)")
    return user, pw


class Session:
    def __init__(self, client: Client, tracker: Tracker):
        self.client = client
        self.tracker = tracker


async def open_session(client: Client, verbose: bool) -> Session:
    user, pw = credentials()
    try:
        await client.login(user, pw)
    except RPCError as e:
        raise UsageError(f"login failed: {e}") from None
    start = dt.datetime.now()
    pages, scores = await crawl(client)
    structure = Structure.from_page(await client.read(model.STRUCTURE_PAGE) or {})
    if verbose:
        secs = (dt.datetime.now() - start).total_seconds()
        print(f"[loaded {len(pages)} pages in {secs:.1f}s as {client.user['home']}]", file=sys.stderr)
    return Session(client, Tracker(Store(pages, scores), structure))


# ---------------------------------------------------------------- formatting

def table(rows: list[list], headers: list[str]) -> str:
    cells = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in cells])


def dump_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def fmt(v) -> str:
    return "(empty)" if v in (None, "") else repr(v) if isinstance(v, str) else str(v)


# ---------------------------------------------------------------- argument parsing helpers

def parse_status(text: str | None, allow_filters: bool):
    if text is None:
        return None
    t = text.strip().lower().replace(" ", "-")
    if allow_filters and t in ("all", "any"):
        return "all"
    if allow_filters and t in ("other", "no-data"):
        return "other"
    if not allow_filters and t in ("none", "no-data", "null"):
        return None
    if t.isdigit() and int(t) in STATUS_OPTIONS:
        return int(t)
    if t in STATUS_ALIASES:
        return STATUS_ALIASES[t]
    for code, label in STATUS_OPTIONS.items():
        if t == label.lower().replace(" ", "-"):
            return code
    opts = ", ".join(f"{k} ({v})" for k, v in STATUS_OPTIONS.items())
    extra = ", all, other" if allow_filters else ", none"
    raise UsageError(f"unknown status {text!r}; use one of: {opts}{extra} or "
                     f"{', '.join(STATUS_ALIASES)}")


def parse_how_done(text: str):
    t = text.strip().lower().replace(" ", "-")
    if t.isdigit() and int(t) in HOW_DONE_OPTIONS:
        return int(t)
    if t in HOW_DONE_ALIASES:
        return HOW_DONE_ALIASES[t]
    raise UsageError(f"unknown --how-done {text!r}; use one of: " + ", ".join(HOW_DONE_ALIASES))


def parse_date(text: str) -> str:
    t = text.strip().lower()
    if t in ("none", "clear", ""):
        return ""
    if t == "today":
        return dt.date.today().isoformat()
    if t == "yesterday":
        return (dt.date.today() - dt.timedelta(days=1)).isoformat()
    try:
        return dt.date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        raise UsageError(f"bad date {text!r}; use YYYY-MM-DD, today, yesterday or none") from None


def default_division(args) -> str | None:
    return args.division if args.division is not None else os.environ.get("MTP_DIVISION") or None


# ---------------------------------------------------------------- writes

async def confirm_and_post(s: Session, page: dict, summary: list[str], args) -> str | None:
    print("\n".join(summary))
    if args.dry_run:
        print("\n[dry run] would post:")
        dump_json(page)
        return None
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("refusing to write without confirmation; pass --yes")
        if input("Save this change? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return None
    new_id = await s.client.add(page)
    saved = await s.client.read(new_id)
    if not saved:
        raise RPCError(f"posted {new_id} but could not read it back")
    print(f"Saved ({new_id}).")
    return new_id


def change_lines(title: str, old: dict, changes: dict, labels: dict, render=None) -> list[str]:
    render = render or (lambda k, v: fmt(v))
    lines = [title]
    for k, v in changes.items():
        lines.append(f"  {labels.get(k, k)}: {render(k, old.get(k))} -> {render(k, v)}")
    return lines


# ---------------------------------------------------------------- commands

async def cmd_search(s: Session, args) -> int:
    status = parse_status(args.status, allow_filters=True) if args.status else 2
    division = s.tracker.structure.resolve_division(default_division(args))
    rows, counts = s.tracker.trainees(status=status, division=division, text=args.text,
                                      include_removed=args.include_removed)
    if args.mentor:
        rows = [t for t in rows if args.mentor.lower() in (t.record.get("mentor") or "").lower()]
    data = [t.as_dict() for t in rows]
    if args.json:
        dump_json(data)
        return 0
    if args.csv:
        cols = ["name", "lastName", "firstName", "team", "division", "mentor", "status",
                "doneCount", "latestDate", "latestMilestone", "id"]
        w = csv.DictWriter(sys.stdout, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)
        return 0
    status_label = {"all": "Any Status", "other": "Other / No Data"}.get(status, STATUS_OPTIONS.get(status))
    print(f"{len(data)} trainees — status: {status_label}; division: {division or 'All Divisions'}"
          + (f"; search: {args.text!r}" if args.text else ""))
    count_bits = [f"{STATUS_OPTIONS[k]} {counts.get(k, 0)}" for k in STATUS_OPTIONS]
    print("  counts: " + ", ".join(count_bits) + f", other {counts.get('other', 0)}, any {counts.get('all', 0)}\n")
    if data:
        print(table([[d["name"], d["team"], d["division"], d["mentor"], d["status"], d["doneCount"],
                      d["latestDate"] or "unknown", d["latestMilestone"]] for d in data],
                    ["Name", "Team", "Division", "Mentor", "Status", "#Done", "Latest date", "Latest milestone"]))
    return 0


async def cmd_show(s: Session, args) -> int:
    t = s.tracker.find_trainee(args.trainee, s.tracker.structure.resolve_division(args.division))
    ms = s.tracker.milestones(t)
    if args.json:
        dump_json({**t.as_dict(), "milestones": [m.as_dict() for m in ms]})
        return 0
    d = t.as_dict()
    print(f"Trainee: {d['name']}   ({t.id})")
    print(table([[k, v or ""] for k, v in [
        ("Team", f"{d['team']} ({d['division']}, {d['region']})"),
        ("Last Name", d["lastName"]), ("First Name", d["firstName"]),
        ("Mentor Name", d["mentor"]), ("Status", d["status"]), ("Note", d["note"]),
    ]], ["Field", "Value"]))
    print()
    rows = []
    for m in ms:
        notes = []
        if m.event:
            if m.event.get("note"):
                notes.append(m.event["note"])
            if m.event.get("plannedDate"):
                notes.append(f"Planned {model.date_str(m.event['plannedDate'])}")
            if (m.event.get("howDone") or 0) > 2:
                notes.append(m.how_done)
            if m.data_entry_error:
                notes.append("DATA ENTRY ERROR: completion date set but not marked complete")
        completed = model.date_str((m.event or {}).get("completedDate"))
        if m.done and not completed:
            completed = "date unknown"
        rows.append([f"{m.position:>2}", "[x]" if m.done else "[ ]", m.goal["name"], completed, "; ".join(notes)])
    print(table(rows, ["#", "", "Milestone", "Completed", "Notes"]))
    done = sum(1 for m in ms if m.done)
    print(f"\n{done} of {len(ms)} milestones done.")
    return 0


async def cmd_update(s: Session, args) -> int:
    t = s.tracker.find_trainee(args.trainee, s.tracker.structure.resolve_division(args.division))
    changes = {}
    if args.team is not None:
        team = s.tracker.structure.resolve_team(args.team, s.tracker.structure.resolve_division(args.team_division))
        changes["team"] = team["id"]
    for attr, key in (("last_name", "lastName"), ("first_name", "firstName"), ("mentor", "mentor"), ("note", "note")):
        if getattr(args, attr) is not None:
            changes[key] = getattr(args, attr)
    if args.status is not None:
        changes["status"] = parse_status(args.status, allow_filters=False)
    if not changes:
        raise UsageError("nothing to update; pass at least one field option (see --help)")

    def render(k, v):
        if k == "status":
            return STATUS_OPTIONS.get(v, "No data")
        if k == "team":
            team = s.tracker.structure.team(v)
            return f"{team['name']} ({team.get('divisionName')})" if team else fmt(v)
        return fmt(v)

    old = dict(t.record)
    if old.get("status") == "other":
        old["status"] = None
    summary = change_lines(f"Update trainee {t.name} ({t.id}):", old, changes, PERSON_FIELDS, render)
    await confirm_and_post(s, model.person_update_page(t, changes), summary, args)
    return 0


async def cmd_milestone(s: Session, args) -> int:
    t = s.tracker.find_trainee(args.trainee, s.tracker.structure.resolve_division(args.division))
    goal = model.resolve_goal(args.milestone)
    ms = next((m for m in s.tracker.milestones(t) if m.goal["id"] == goal["id"]), None)
    if ms is None:  # optional goal with no event yet
        ms = model.Milestone(GOALS.index(goal) + 1, goal)
    changes = {}
    if args.done:
        changes["howDone"] = 2
        if args.completed is None and not (ms.event or {}).get("completedDate"):
            changes["completedDate"] = dt.date.today().isoformat()
    if args.how_done is not None:
        changes["howDone"] = parse_how_done(args.how_done)
    if args.completed is not None:
        changes["completedDate"] = parse_date(args.completed)
    if args.planned is not None:
        changes["plannedDate"] = parse_date(args.planned)
    if args.note is not None:
        changes["note"] = args.note
    if not changes:
        raise UsageError("nothing to change; pass --done, --how-done, --completed, --planned or --note")

    page = model.milestone_page(t, ms, changes)
    render = lambda k, v: HOW_DONE_OPTIONS.get(v, "No data") if k == "howDone" else fmt(v)
    verb = "Update" if ms.event_id else "Create"
    shown = changes if ms.event_id else {k: page[k] for k in EVENT_FIELDS if page[k] not in (None, "")}
    summary = change_lines(f"{verb} milestone {ms.position}. {goal['name']} for {t.name}:",
                           ms.event or {}, shown, EVENT_FIELDS, render)
    after = {**(ms.event or {}), **{k: page[k] for k in EVENT_FIELDS if k in page}}
    if after.get("completedDate") and not model.is_done(after):
        summary.append("  warning: completion date set but not marked complete "
                       "(the tracker flags this as a data entry error)")
    await confirm_and_post(s, page, summary, args)
    return 0


async def cmd_add_trainee(s: Session, args) -> int:
    team = s.tracker.structure.resolve_team(args.team, s.tracker.structure.resolve_division(default_division(args)))
    fields = {"team": team["id"], "lastName": args.last_name, "firstName": args.first_name,
              "mentor": args.mentor or "", "note": args.note or "",
              "status": parse_status(args.status, allow_filters=False) if args.status else 1}
    summary = [f"Add trainee {args.last_name}, {args.first_name} to {team['name']} ({team.get('divisionName')}):"]
    summary += [f"  {PERSON_FIELDS[k]}: {STATUS_OPTIONS.get(v, 'No data') if k == 'status' else fmt(v)}"
                for k, v in fields.items() if k != "team"]
    await confirm_and_post(s, model.new_person_page(fields), summary, args)
    return 0


async def cmd_remove(s: Session, args, removed: bool) -> int:
    division = s.tracker.structure.resolve_division(args.division)
    if removed:
        t = s.tracker.find_trainee(args.trainee, division)
    else:
        everyone, _ = s.tracker.trainees(status="all", division=division, include_removed=True)
        hits = [x for x in everyone if x.record.get("isRemoved") and
                (args.trainee == x.id or all(w in x.name.lower() for w in args.trainee.lower().replace(",", " ").split()))]
        if len(hits) != 1:
            raise NotFound(f"{len(hits)} removed trainees match {args.trainee!r}" +
                           "".join(f"\n  {x.name}  {x.id}" for x in hits))
        t = hits[0]
    verb = "Remove" if removed else "Restore"
    summary = [f"{verb} trainee {t.name} ({t.team.get('name')}, {t.id})"]
    if removed:
        summary.append("  (hidden from the usual views; restorable from Recent changes or `mtp restore`)")
    await confirm_and_post(s, model.removal_page(t.id, removed), summary, args)
    return 0


async def cmd_divisions(s: Session, args) -> int:
    opts = s.tracker.structure.divisions()
    if args.json:
        dump_json([{"value": v, "label": label} for v, label in opts])
    else:
        print(table([[v or '""', label] for v, label in opts], ["Value", "Label"]))
    return 0


async def cmd_teams(s: Session, args) -> int:
    division = s.tracker.structure.resolve_division(default_division(args))
    teams = [t for t in s.tracker.structure.teams if not s.tracker.structure.filtered_out(t, division)]
    if args.json:
        dump_json(teams)
    else:
        print(table([[t["id"], t["name"], t.get("divisionName"), t.get("regionId")] for t in teams],
                    ["Id", "Team", "Division", "Region"]))
    return 0


def cmd_milestones_offline(args) -> int:
    if args.json:
        dump_json([{"position": i, **g} for i, g in enumerate(GOALS, 1)])
    else:
        print(table([[i, g["name"], "optional" if g.get("optional") else "", ", ".join(g["aliases"])]
                     for i, g in enumerate(GOALS, 1)], ["#", "Milestone", "", "Also matches"]))
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mtp", description="Search and update the MDI MTP tracker (writable.site).")
    p.add_argument("-v", "--verbose", action="store_true", help="print load timing to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    def writer(sp):
        sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
        sp.add_argument("-n", "--dry-run", action="store_true", help="show what would be posted, change nothing")

    def who(sp):
        sp.add_argument("trainee", help="name (\"Last\", \"First Last\", \"Last, First\") or record id")
        sp.add_argument("-d", "--division", help="narrow the name lookup to a division or region (see `mtp divisions`)")

    sp = sub.add_parser("search", help="list trainees, using the same filters as the web UI")
    sp.add_argument("-s", "--status", help="in-progress (default), not-started, completed, paused, left, other, all, or 1-5")
    sp.add_argument("-d", "--division", help="division or region name/value from `mtp divisions`; default $MTP_DIVISION or all")
    sp.add_argument("-t", "--text", help="free-text search over name, team, division and region (the UI search box)")
    sp.add_argument("-m", "--mentor", help="only trainees whose mentor contains this text")
    sp.add_argument("--include-removed", action="store_true", help="include removed records")
    fmt_group = sp.add_mutually_exclusive_group()
    fmt_group.add_argument("--json", action="store_true")
    fmt_group.add_argument("--csv", action="store_true")

    sp = sub.add_parser("show", help="show a trainee's details and milestone table")
    who(sp)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("update", help="edit a trainee's fields (team, names, mentor, status, note)")
    who(sp)
    sp.add_argument("--team", help="team name or id")
    sp.add_argument("--team-division", help="division to disambiguate --team")
    sp.add_argument("--last-name")
    sp.add_argument("--first-name")
    sp.add_argument("--mentor")
    sp.add_argument("--status", help="not-started, in-progress, completed, paused, left, none, or 1-5")
    sp.add_argument("--note")
    writer(sp)

    sp = sub.add_parser("milestone", help="update (or create) one milestone for a trainee")
    who(sp)
    sp.add_argument("milestone", help="number from `mtp milestones` or a name fragment (\"lead fun\")")
    sp.add_argument("--done", action="store_true", help="mark Done as part of MTP (completed today unless --completed)")
    sp.add_argument("--how-done", help="done, not-yet, elsewhere, waived, none")
    sp.add_argument("--completed", help="completion date: YYYY-MM-DD, today, yesterday, none")
    sp.add_argument("--planned", help="planned date: YYYY-MM-DD, today, none")
    sp.add_argument("--note")
    writer(sp)

    sp = sub.add_parser("add-trainee", help="add a new trainee (the UI's Add Trainee)")
    sp.add_argument("--team", required=True, help="team name or id")
    sp.add_argument("-d", "--division", help="division used to resolve --team")
    sp.add_argument("--last-name", required=True)
    sp.add_argument("--first-name", required=True)
    sp.add_argument("--mentor")
    sp.add_argument("--status", help="default not-started")
    sp.add_argument("--note")
    writer(sp)

    for name, text in (("remove", "hide a trainee record (reversible)"), ("restore", "restore a removed trainee")):
        sp = sub.add_parser(name, help=text)
        who(sp)
        writer(sp)

    sp = sub.add_parser("divisions", help="list division filter values")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("teams", help="list teams")
    sp.add_argument("-d", "--division")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("milestones", help="list milestone numbers and names")
    sp.add_argument("--json", action="store_true")
    return p


COMMANDS = {
    "search": cmd_search, "show": cmd_show, "update": cmd_update, "milestone": cmd_milestone,
    "add-trainee": cmd_add_trainee, "divisions": cmd_divisions, "teams": cmd_teams,
    "remove": lambda s, a: cmd_remove(s, a, True), "restore": lambda s, a: cmd_remove(s, a, False),
}


async def run(args) -> int:
    async with Client(os.environ.get("MTP_SERVER", DEFAULT_SERVER)) as client:
        session = await open_session(client, args.verbose)
        return await COMMANDS[args.command](session, args)


def main(argv=None) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave when piped into head/less
    args = build_parser().parse_args(argv)
    if args.command == "milestones":
        return cmd_milestones_offline(args)
    load_dotenv()
    try:
        return asyncio.run(run(args))
    except (UsageError, NotFound) as e:
        print(f"mtp: {e}", file=sys.stderr)
        return 2
    except (RPCError, OSError) as e:
        print(f"mtp: server error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
