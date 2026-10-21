"""Load the tracker's pages and merge them into "subjects", the way the web app does.

writable.site never edits a record in place. Every save adds a new page, and
pages that name the same `subjectPage` are clumped together; the current state
of a record is all of its pages overlaid oldest -> newest by `lastModified`.

Which pages count is decided by a trust crawl ("credQuery" in the app): start at
the user's home folder, follow `ratingOf` links (e.g. /u/me/cred/1 -> the MTP
group, the group's member list -> each member's home) and collect everything
under every folder reached.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .rpc import Client

# Keys that describe a page rather than the record, so they're not overlaid.
META_KEYS = {
    "about", "referencePages", "subjectPage", "subjectPages", "eTag",
    "created", "permit", "owner", "pageId", "score",
}


async def crawl(client: Client, concurrency: int = 24) -> tuple[dict[str, dict], dict[str, float]]:
    """Return ({pageId: data}, {pageId: score}) for every page the user's trust graph reaches."""
    home = client.user["home"]
    pages: dict[str, dict] = {home: (await client.read(home)) or {}}
    scores: dict[str, float] = {home: 1.0}
    sem = asyncio.Semaphore(concurrency)

    async def under(root: str) -> dict[str, dict]:
        async with sem:
            return await client.query({"path": {"ancestor": root}})

    async def read(pid: str) -> dict | None:
        async with sem:
            return await client.read(pid)

    frontier = [home]
    while frontier:
        results = await asyncio.gather(*(under(r) for r in frontier))
        links: dict[str, float] = {}
        for root, found in zip(frontier, results):
            for pid, data in found.items():
                if not data or pid in pages:
                    continue
                pages[pid] = data
                scores[pid] = scores[root]
                target = data.get("ratingOf")
                if target and target not in pages:
                    s = scores[root] * (data.get("rating") or 0)
                    if s >= 0:
                        links[target] = max(s, links.get(target, s))
        targets = list(links)
        reads = await asyncio.gather(*(read(t) for t in targets))
        frontier = []
        for target, data in zip(targets, reads):
            if data is not None and target not in pages:
                pages[target] = data
                scores[target] = links[target]
                frontier.append(target)
    return pages, scores


@dataclass
class Clump:
    ids: list[str] = field(default_factory=list)


def _deep_merge(old, new):
    if not isinstance(new, dict) or not isinstance(old, dict):
        return new
    out = dict(old)
    for k, v in new.items():
        out[k] = _deep_merge(out.get(k), v)
    return out


class Store:
    """In-memory view of the crawled pages with the app's subject semantics."""

    def __init__(self, pages: dict[str, dict], scores: dict[str, float] | None = None):
        self.pages = pages
        self.scores = scores or {}
        self._clump_of: dict[str, list[str]] = {}
        self._build_clumps()
        self._merged: dict[int, tuple[str, dict]] = {}

    def _build_clumps(self) -> None:
        clump_of = self._clump_of
        for pid, data in self.pages.items():
            refs = []
            if data.get("subjectPage"):
                refs.append(data["subjectPage"])
            refs.extend(data.get("subjectPages") or [])
            for ref in refs:
                a = clump_of.get(pid)
                b = clump_of.setdefault(ref, [ref])
                if a is None:
                    b.append(pid)
                    clump_of[pid] = b
                elif a is not b:
                    for x in a:
                        b.append(x)
                        clump_of[x] = b
            clump_of.setdefault(pid, [pid])

    def _lm(self, pid: str) -> str:
        return (self.pages.get(pid) or {}).get("lastModified") or ""

    def _best_id(self, ids: list[str]) -> str:
        # Mirrors EqualityMap clump.bestId: highest trust score, then earliest page.
        best, best_score, best_lm = None, None, None
        for pid in ids:
            s, lm = self.scores.get(pid, 0), self._lm(pid)
            if (best_score is None or s >= best_score) and (best_lm is None or lm < best_lm):
                best, best_score, best_lm = pid, s, lm
        return best or ids[0]

    def merged(self, any_id: str) -> tuple[str, dict] | None:
        """Return (subjectId, merged record) for the clump containing any_id."""
        ids = self._clump_of.get(any_id)
        if ids is None:
            return None
        key = id(ids)
        if key not in self._merged:
            present = [i for i in ids if i in self.pages]
            if not present:
                return None
            ordered = sorted(present, key=lambda i: (self._lm(i) == "", self._lm(i)))
            rec: dict = {}
            source: dict[str, str] = {}
            for pid in ordered:
                for k, v in self.pages[pid].items():
                    if k in META_KEYS:
                        continue
                    rec[k] = _deep_merge(rec.get(k), v)
                    source[k] = pid
            best = self._best_id(list(dict.fromkeys(ids)))
            rec["subjectPage"] = best
            rec["subjectPages"] = ordered
            rec["sourcePage"] = source
            self._merged[key] = (best, rec)
        return self._merged[key]

    def subject_read(self, subject_id: str, include_removed: bool = False) -> dict | None:
        m = self.merged(subject_id)
        if not m or (m[1].get("isRemoved") and not include_removed):
            return None
        return m[1]

    def subject_query(self, flt: dict, include_removed: bool = False) -> dict[str, dict]:
        out: dict[str, dict] = {}
        seen: set[int] = set()
        for ids in self._clump_of.values():
            if id(ids) in seen:
                continue
            seen.add(id(ids))
            m = self.merged(ids[0])
            if not m:
                continue
            best, rec = m
            if rec.get("isRemoved") and not include_removed:
                continue
            if matches(rec, flt):
                out[best] = rec
        return out


def matches(rec: dict, flt: dict) -> bool:
    for k, cond in flt.items():
        if isinstance(cond, dict) and "exists" in cond:
            if (k in rec and rec[k] is not None) != bool(cond["exists"]):
                return False
        elif rec.get(k) != cond:
            return False
    return True
