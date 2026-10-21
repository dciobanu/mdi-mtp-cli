import datetime as dt

import pytest

from mtp_cli import model
from mtp_cli.store import Store

GROUP = {"read": {model.GROUP: True}}
STRUCTURE = model.Structure(
    teams=[
        {"name": "Iron Oaks", "regionId": "WST", "divisionName": "North Coast", "id": 501},
        {"name": "Stone Circle", "regionId": "WST", "divisionName": "North Coast", "id": 502},
        {"name": "Red Kites", "regionId": "EST", "divisionName": "Harbor Valley", "id": 503},
    ],
    regions={"WST": "Western", "EST": "Eastern"},
)


def page(lm, **kw):
    return {"lastModified": lm, "created": lm, "permit": GROUP, "owner": 1, **kw}


def make_tracker():
    pages = {
        # Person created, then status changed by someone else's overlay page.
        "/u/a/posts/1": page("2026-01-01T00:00:00Z", isMTP=True, team=501, lastName="Doe",
                             firstName="John", mentor="Roe", status=1, note=""),
        "/u/b/posts/9": page("2026-02-01T00:00:00Z", isMTP=True, subjectPage="/u/a/posts/1", status=2),
        # Milestone event: created not-yet, later marked done via overlay.
        "/u/a/posts/2": page("2026-01-02T00:00:00Z", isMTP=True, goal=11, person="/u/a/posts/1",
                             howDone=1, plannedDate="2026-03-01", completedDate="", note=""),
        "/u/b/posts/10": page("2026-03-05T00:00:00Z", isMTP=True, goal=11, person="/u/a/posts/1",
                              subjectPage="/u/a/posts/2", howDone=2, completedDate="2026-03-04"),
        "/u/a/posts/3": page("2026-01-03T00:00:00Z", isMTP=True, goal=1, person="/u/a/posts/1",
                             howDone=2, completedDate="2026-01-03"),
        # Another division, and a removed record.
        "/u/c/posts/1": page("2026-01-01T00:00:00Z", isMTP=True, team=503, lastName="Smith", firstName="Jane", status=2),
        "/u/c/posts/2": page("2026-01-01T00:00:00Z", isMTP=True, team=502, lastName="Gone", firstName="Guy", status=2),
        "/u/c/posts/3": page("2026-01-05T00:00:00Z", subjectPage="/u/c/posts/2", isRemoved=True),
    }
    return model.Tracker(Store(pages), STRUCTURE)


def test_overlay_merges_latest_values_and_keeps_subject_id():
    t = make_tracker().find_trainee("Doe")
    assert t.id == "/u/a/posts/1"
    assert t.record["status"] == 2 and t.record["mentor"] == "Roe"


def test_search_filters_match_ui():
    tr = make_tracker()
    rows, counts = tr.trainees(status=2, division="North Coast")
    assert [r.name for r in rows] == ["Doe, John"]  # removed record hidden
    assert counts == {"all": 1, 2: 1}
    rows, _ = tr.trainees(status="all", division="*EST")
    assert [r.name for r in rows] == ["Smith, Jane"]
    rows, _ = tr.trainees(status="all", text="western")
    assert [r.name for r in rows] == ["Doe, John"]
    rows, _ = tr.trainees(status="all", include_removed=True, division="North Coast")
    assert {r.name for r in rows} == {"Doe, John", "Gone, Guy"}


def test_latest_milestone_and_done_count():
    d = make_tracker().trainees(status="all", division="North Coast")[0][0].as_dict()
    assert d["doneCount"] == 2
    assert d["latestDate"] == "2026-03-04"
    assert d["latestMilestone"] == "11. Lead Fun at a Team Meeting"


def test_milestone_table():
    tr = make_tracker()
    ms = tr.milestones(tr.find_trainee("John Doe"))
    assert len(ms) == 18  # optional extras hidden when absent
    fun = next(m for m in ms if m.goal["id"] == 11)
    assert fun.done and fun.event_id == "/u/a/posts/2" and fun.position == 11


def test_update_payloads_mirror_ui_save():
    tr = make_tracker()
    t = tr.find_trainee("Doe")
    assert model.person_update_page(t, {"status": 3}) == {
        "subjectPage": "/u/a/posts/1", "isMTP": True, "status": 3, "permit": GROUP}
    fun = next(m for m in tr.milestones(t) if m.goal["id"] == 11)
    assert model.milestone_page(t, fun, {"note": "x"}) == {
        "subjectPage": "/u/a/posts/2", "isMTP": True, "goal": 11, "person": "/u/a/posts/1",
        "note": "x", "permit": GROUP}
    teach = next(m for m in tr.milestones(t) if m.goal["id"] == 7)
    assert model.milestone_page(t, teach, {}, today=dt.date(2026, 4, 17)) == {
        "howDone": 2, "plannedDate": "", "completedDate": "2026-04-17", "note": "",
        "isMTP": True, "goal": 7, "person": "/u/a/posts/1", "permit": GROUP}
    planned = model.milestone_page(t, teach, {"howDone": 1, "plannedDate": "2026-05-01"})
    assert planned["completedDate"] == "" and planned["howDone"] == 1


@pytest.mark.parametrize("text,goal_id", [
    ("6", 7), ("deliver a teach", 7), ("leading fun", 11), ("delivering the teach", 7),
    ("leading ceremony", 12), ("Completion ceremony", 16), ("CPR", 6),
])
def test_resolve_goal(text, goal_id):
    assert model.resolve_goal(text)["id"] == goal_id


def test_code_of_honor_is_ambiguous():
    with pytest.raises(model.NotFound, match="ambiguous"):
        model.resolve_goal("code of honor")


def test_division_and_team_resolution():
    assert STRUCTURE.resolve_division("north coast") == "North Coast"
    assert STRUCTURE.resolve_division("Western") == "*WST"
    assert STRUCTURE.resolve_team("iron oaks")["id"] == 501
    assert STRUCTURE.resolve_team("502")["name"] == "Stone Circle"
