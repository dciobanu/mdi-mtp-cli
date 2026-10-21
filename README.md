# MDI MTP CLI

Command-line client for the MDI Membership Training Program tracker at
https://writable.site/mdi/mtp/app. No browser involved: it speaks the site's
own network protocol directly.

## Setup

```bash
pip install -e '.[dev]'
```

Credentials come from the environment (`MTP_USERNAME` / `MTP_PASSWORD`) or from
`username=` / `password=` in `.env` (see `.env.example`). Optional:

- `MTP_DIVISION`: default division for `search`, `teams` and `add-trainee` (e.g. `North Coast`)
- `MTP_SERVER`: override the API endpoint (default `wss://b2.writable.site/_API`)

## Usage

```bash
mtp search                                   # In progress, all divisions (the UI default)
mtp search -d "North Coast"                  # one division
mtp search -d Western -s all                 # a whole region, any status
mtp search -s completed -t "oaks" --csv      # text search, CSV out (also --json)
mtp search -d "North Coast" -m Rivera        # by mentor (extra; not a UI filter)

mtp show "Sam Carter"                        # trainee fields + milestone table (also --json)

mtp milestone Carter "lead fun" --done       # Done as part of MTP, completed today
mtp milestone Carter 15 --done --completed 2026-10-20
mtp milestone Carter "deliver a teach" --planned 2026-11-03 --how-done not-yet
mtp milestone Carter 5 --note "Goal set at team meeting"
mtp milestone Carter 5 --completed none      # clear a date

mtp update Carter --status completed --mentor "Alex Rivera" --note "..."
mtp update Carter --team "Iron Oaks"
mtp add-trainee -d "North Coast" --team "Iron Oaks" --last-name Doe --first-name John --mentor "..."
mtp remove Doe                               # reversible: same as the UI's "remove"
mtp restore Doe

mtp divisions                                # reference lists
mtp teams -d "North Coast"
mtp milestones
```

Trainees can be named as `Last`, `First Last`, `Last, First`, or by record id
(shown by `show` and `search --json`). If a name matches more than one record,
the command lists them and stops. Milestones accept their number (as numbered in
the UI, see `mtp milestones`) or a name fragment; ambiguous phrases such as
"code of honor" are rejected with the candidates.

Every write prints the change (old → new) and asks for confirmation. Pass
`-n/--dry-run` to see the exact page that would be posted, or `-y/--yes` to skip
the prompt.

Field values:

| Option | Values |
|---|---|
| `--status` | `not-started`, `in-progress`, `completed`, `paused`, `left`, `none`, or `1`–`5` |
| `--how-done` | `done` (Done as part of MTP), `not-yet`, `elsewhere` (Learned elsewhere), `waived` (Waived by team), `none` |
| dates | `YYYY-MM-DD`, `today`, `yesterday`, `none` |

## How it works

The tracker is built on writable.site "datapages" and has no REST API. The web
app, and this CLI, use JSON RPC over a WebSocket (`src/mtp_cli/rpc.py`):

- **Login:** `login(username, password)` on the socket.
- **Reading:** the app gathers every page reachable through a trust graph: your
  home folder → `cred` pages → the `/mdi/groups/mtp-all-access` group → each
  member's folder. Then it merges pages that share a `subjectPage` into one
  record, newest `lastModified` winning (`src/mtp_cli/store.py`). Trainees are
  records with `isMTP` and `lastName`. Milestones are records with `goal` and
  `person`. A full load is about 15k pages and takes 2–3 s.
- **Writing:** nothing is edited in place. Save adds a new page
  `{subjectPage: <record>, <changed fields>, permit: {read: {group: true}}}`
  under your own `/u/<you>/posts/`, exactly as the UI's Save button does. A new
  milestone or trainee is a page without `subjectPage`. The edit history in
  the UI shows these pages.

The list, filter, and milestone logic comes from the tracker's own app scripts
(`/scripts/mtp-app`, `/scripts/mtp-config`, `/scripts/milestone`), so results
match the web UI, duplicate records included.

## Tests

```bash
pytest
```
