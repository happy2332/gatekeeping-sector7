# Session handoff — Gatekeeping (Sector 7 Pocket C)

This file captures everything a fresh Claude session needs to pick up where the
previous one left off: what the app does, the design choices made, what's been
built so far, and the open follow-ups.

> If you're a new Claude session: read this first, then `OPERATIONS.md`, then
> the source. Don't guess at history — `git log` is the source of truth for
> what shipped and when.

---

## What this app is

A mobile-friendly web app for the gatekeeper at a residential pocket
(Sector 7 Pocket C). One screen at the gate to log vehicles in/out, a small
admin tab for managing houses and clearing logs, and read-only views for
residents.

Origin problem (verbatim from the user's first message): "no effective
mechanism to track vehicles and visitors entering or leaving the pocket …
unauthorized parking, vehicles being left at unknown locations … residents
repeatedly raising complaints on the group without any lasting resolution."

## Live deployment

- **URL:** https://gatekeeping-sector7.fly.dev
- **Host:** Fly.io free tier, region `bom` (Mumbai)
- **Persistent volume:** `gk_data` (1 GB) mounted at `/data`, daily snapshots × 5 retained
- **Min machines running:** 1 (so the guard doesn't hit cold starts)
- **GitHub:** https://github.com/happy2332/gatekeeping-sector7

Local dev: `.venv/bin/python app.py` (port 5057 by default).

---

## Stack and shape

- **Flask** + **SQLite**, single-process, gunicorn in production.
- Templates with Jinja2, vanilla CSS, vanilla JS (no framework, no build step).
- 18 source files, ~700 lines of Python, mobile-first responsive design.
- `app.py` holds everything: routes, schema, migrations, role helpers, Jinja
  filters and context processors. Don't split it up unless it crosses ~1500
  lines — keeping it in one file is part of the deal.

### Files at a glance

```
app.py                      # all routes + DB code
fly.toml                    # Fly app config (region bom, volume gk_data)
Dockerfile                  # python:3.12-slim + tzdata for IST
render.yaml                 # ❌ removed; we tried Render, ran into free-tier disk limit
requirements.txt            # Flask + gunicorn
static/style.css            # mobile-first; ≤640px collapses tables to cards
static/sortable.js          # click-to-sort headers on any <table class="data sortable">
templates/base.html         # topbar nav, role-aware
templates/login.html        # single password field; role chosen by which secret matches
templates/gate.html         # vehicle search + IN/OUT (guard, admin)
templates/index.html        # "Currently inside" + Recent activity (everyone)
templates/log.html          # full movement log + search (everyone)
templates/houses.html       # houses list + search; add-house form (admin only)
templates/house_detail.html # house info, vehicles, edit form (admin only)
templates/admin.html        # delete house, clear/export logs
run_tests.py                # 35 tests: house + vehicle CRUD
run_gate_tests.py           # 56 tests: gate flow, search, IN/OUT, validation
run_admin_tests.py          # 35 tests: admin tab, role boundaries
OPERATIONS.md               # ops cheat sheet
SESSION.md                  # this file
```

---

## The three roles (load-bearing decision)

The user explicitly chose this model:

> Resident is open / no login. Single login page — role chosen by password.
> Residents see ALL plates and ALL logs (society-wide visibility).

| URL                                  | Resident (no login) | Guard | Admin |
|--------------------------------------|---------------------|-------|-------|
| `/` Inside                           | ✅                  | ✅    | ✅    |
| `/log`                               | ✅                  | ✅    | ✅    |
| `/houses` (read-only)                | ✅                  | ✅    | ✅    |
| House detail page (incl. tap-to-call)| ✅                  | ✅    | ✅    |
| Vehicle search                       | ✅                  | ✅    | ✅    |
| `/gate` (log IN/OUT)                 | ❌                  | ✅    | ✅    |
| Add/edit/delete vehicles             | ❌                  | ❌    | ✅    |
| Add/edit/delete houses               | ❌                  | ❌    | ✅    |
| Admin tab (delete house, clear logs) | ❌                  | ❌    | ✅    |

Login at `/login`. The password the user enters chooses the role:
- matches `ADMIN_PASSWORD` → admin
- matches `GUARD_PASSWORD` → guard
- else → "Incorrect password"

Helper decorators in `app.py`: `@guard_required`, `@admin_required`, plus
`role_at_least()` for inline checks. The `inject_role` context processor
exposes `is_resident`, `is_guard`, `is_admin` to all templates.

---

## Data model

```
houses
  id PK
  number TEXT NOT NULL
  owner_name TEXT
  phone TEXT
  floor TEXT                      -- nullable; multi-floor houses possible
  phone_masked INTEGER NOT NULL DEFAULT 0
  -- partial unique indexes (NOT a single UNIQUE on number):
  --   uniq_house_with_floor: UNIQUE(number, floor) WHERE floor IS NOT NULL
  --   uniq_house_no_floor:   UNIQUE(number)        WHERE floor IS NULL

vehicles
  id PK
  house_id FK → houses(id) ON DELETE CASCADE
  plate TEXT UNIQUE              -- normalised: uppercase, strip non-alphanumeric
  make_model TEXT                -- legacy column, no longer in UI; kept for backward compat
  colour TEXT                    -- same

movements
  id PK
  house_id FK → houses(id) (nullable; null for unknown vehicles)
  vehicle_id FK → vehicles(id) (nullable; null for visitors / unknown)
  plate TEXT
  kind TEXT CHECK IN ('resident','visitor','unknown')
  direction TEXT CHECK IN ('in','out')
  visitor_name TEXT
  visitor_phone TEXT
  note TEXT
  ts TEXT NOT NULL               -- IST wall-time, Python-supplied
```

### Schema migrations are automatic on startup

`init_db()` runs at import time (so gunicorn picks it up, not just `__main__`).
It uses `PRAGMA table_info` to detect missing columns and `ALTER TABLE` them in.
A one-shot table rebuild drops the legacy `UNIQUE(number)` constraint when it
detects an autoindex from the original schema. Don't write classic migration
files — the in-app pattern works for this app's size.

### Key invariants enforced in routes (not in schema)

- A house number can have multiple floor entries, OR one floor-less entry,
  but never both. Enforced in `house_create()` and `house_detail()` POST.
- A plate can only belong to one house (UNIQUE constraint on `vehicles.plate`).
- A plate that is currently inside cannot be logged IN again, and vice versa.
  `/api/log` returns 409 with an error message; the gate UI also disables the
  irrelevant button per row.

---

## Notable design choices and why

### Plate normalisation
Plates are stripped of all non-alphanumeric characters and uppercased on input
(`normalise_plate()`). `dl 3c ab 1234` → `DL3CAB1234`. This means:
- The same physical car can't be added twice to different houses.
- The gate search is case- and space-insensitive.

### IST timestamps stored, not derived
We compute `now_ist_str()` in Python with a fixed `+05:30` offset and pass the
string to SQLite. Earlier the schema used `datetime('now', 'localtime')` which
on Fly's UTC containers stored UTC times. The Dockerfile also installs `tzdata`
and sets `TZ=Asia/Kolkata` as a belt-and-braces fix.

> Existing rows inserted before commit `9d67652` were stamped in UTC. They
> are NOT retroactively shifted. If the user wants them converted, the
> migration is in the chat history but was not run.

### Phone masking
Per-house `phone_masked` boolean. When set:
- Admin and guard always see the phone (with tap-to-call `tel:` link).
- Residents see "hidden" instead.
- The toggle is a checkbox on Add-house and Edit-house forms.
- A Jinja context processor exposes `phone_visible(house)` so templates
  don't repeat the rule.

### Mobile responsive tables
At ≤640px, every `<table class="data">` collapses to a vertical card layout
via CSS. Each `<td>` has a `data-label="..."` attribute that the CSS shows as
a small uppercase prefix on the left. Saved scrolling on the houses list
(6 columns) without a JS framework.

### Session secret
`GK_SECRET` env var if set; otherwise `secrets.token_hex(16)` per process.
On Fly, `GK_SECRET` is set so admin sessions survive restarts. **Don't
remove the fallback** — it keeps `python app.py` working locally without env vars.

### Why no users table
The user opted for role-by-password. Per-user accounts add a lot of UI
(create / list / disable users) for an MVP that has 1 admin and 1–2 guards.
If/when the society grows or wants per-guard audit trails, switch to a real
`users` table. Until then, do not add it.

---

## Build timeline (what got shipped, in order)

Anchored by `git log --oneline` so a future session can reconstruct context:

1. **Initial scaffold** (`a25c155`) — Flask app, SQLite, gate/houses/log/inside
   pages, basic mobile CSS, sample data. Single role, no auth.
2. **Multi-floor house support** — `(number, floor)` partial-unique indexes;
   per-route check that prevents mixing floored and floor-less entries for
   the same number.
3. **Vehicle search at the gate** — `/api/vehicles/search` returns up to 10
   plates matching a partial query (≥2 chars). The gate page got a debounced
   live-suggestion list. Disabled IN/OUT buttons per `currently_inside`.
4. **Three roles** — single `/login` page, role chosen by which password
   matches. `@guard_required`, `@admin_required`. Templates updated to
   hide admin actions from residents.
5. **Admin tab** — delete house (cascading vehicle delete), clear all logs
   (typed `CLEAR` confirmation), clear logs older than N days, CSV export.
6. **Sortable + searchable tables** — `static/sortable.js` (vanilla JS, ~50
   lines). All four data tables sortable; Inside/Log/Houses each have a
   server-side `?q=` filter.
7. **Status column rename** — `Dir` → `Status`. Date and Time split into
   separate columns; Time in 12-hour format with am/pm.
8. **Render → Fly switch** (`0f2ac3a`) — Render free tier dropped persistent
   disks, so `render.yaml` was removed and `fly.toml` + `Dockerfile` added.
9. **Operations doc** (`b8325e6`) — `OPERATIONS.md`.
10. **Mobile-friendly tables + house search** (`d086621`) — table → card on
    narrow screens, Houses tab now searches by house, owner, or vehicle.
11. **Phone masking** (`74ec76d`).
12. **IST timezone** (`9d67652`).

---

## Tests

136 total across three suites. Each suite needs an empty DB.

- `run_tests.py` — 35 tests: house + vehicle CRUD, multi-floor rules, edge cases.
- `run_gate_tests.py` — 56 tests: gate UI, search API, IN/OUT, visitor flow,
  unknown flow, API validation, log view, IN/OUT guard.
- `run_admin_tests.py` — 35 tests: resident/guard/admin role boundaries,
  delete house, clear logs, CSV export.

Run pattern:
```bash
kill $(lsof -nP -iTCP:5058 -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null
rm -f /tmp/gk_test.db
GK_DB=/tmp/gk_test.db PORT=5058 ADMIN_PASSWORD=admin GUARD_PASSWORD=guard \
  .venv/bin/python app.py > /tmp/gk_test_server.log 2>&1 &
sleep 1.5
.venv/bin/python run_tests.py
# repeat with rm -f /tmp/gk_test.db between suites
```

The tests parse rendered HTML with regex. When you change templates, expect
to update the patterns. Common gotchas the regexes already account for:
- `<td>` may have `data-label="..."` attributes
- House number cells are wrapped in `<a>` tags
- Multi-floor rows have additional `<td>` columns

---

## Open follow-ups (in rough priority order)

1. **Replace placeholder Fly secrets.** `ADMIN_PASSWORD=admin` /
   `GUARD_PASSWORD=guard` were set during deploy. The user said they'd update
   them but I have no confirmation that's been done. Verify with
   `flyctl secrets list` and `flyctl secrets set ...` if needed.
2. **Revoke the GitHub PAT.** During initial setup the user pasted a
   fine-grained PAT (`github_pat_11ACB7EDY0...`) into chat to push the repo.
   It needs to be deleted at https://github.com/settings/tokens?type=beta.
   This was flagged twice in the original session but never confirmed done.
3. **Backfill: shift old movement rows from UTC → IST?** Rows inserted before
   commit `9d67652` are in UTC. The migration is a one-shot SQL update:
   ```sql
   UPDATE movements
   SET ts = strftime('%Y-%m-%d %H:%M:%S', ts, '+5 hours', '+30 minutes')
   WHERE ts < '<deploy time of 9d67652>';
   ```
   Only run if the user confirms all pre-9d67652 rows really were stamped on
   Fly (UTC), not on the local Mac (which would be IST already).
4. **Backups.** Fly retains 5 daily volume snapshots automatically. Consider:
   - A weekly cron on the user's Mac that runs `flyctl ssh sftp get` to pull
     `/data/gatekeeping.db`.
   - Or a route that triggers a CSV email.
5. **Photo of the visitor's vehicle.** Frequently asked-for in society apps —
   guard takes a phone-camera shot when entering an unknown plate.
   `<input type="file" accept="image/*" capture="environment">` then store in
   the volume.
6. **Resident notification when a visitor arrives.** WhatsApp/SMS link with
   the visitor's plate + name. Twilio or a Telegram bot.
7. **Per-guard accounting.** If multiple guards share the gate phone we lose
   any "who logged this" attribution. Switch to a users table when this matters.
8. **Auto-clear stale "currently inside" entries.** A vehicle logged IN with
   no later OUT stays inside forever. Should we auto-OUT at 04:00 IST? Or
   warn admin after N hours? User has not yet decided.

---

## Things to NOT do unless asked

- Don't introduce a frontend framework. The vanilla JS works.
- Don't split `app.py` into modules. ~700 lines is fine in one file.
- Don't add user accounts. Role-by-password is the explicit choice.
- Don't add per-user audit trail columns. Same reason.
- Don't rebuild the schema from scratch — use the in-app `ALTER TABLE` pattern.
- Don't drop the legacy `make_model` / `colour` columns. They're empty and
  unused but the migration would make this file longer than the savings.
- Don't add CSS preprocessors / build steps. Keep `static/style.css` plain.
- Don't add database connection pooling or migrate to Postgres. SQLite is
  fine for ~100 events/day on the volume Fly already provisioned.

---

## Quick orientation for a new session

If the user comes back with a feature request, do this in order:

1. Read this file. (You did, good.)
2. Check `git log --oneline` to see anything I shipped after this was written.
3. Skim `app.py` — the code is the source of truth.
4. Look at the most recent template the user is talking about.
5. Make the change. Run the relevant test suite. Deploy with
   `flyctl deploy --remote-only` from the project root.
6. If the user mentions "Claude said earlier we'd do X" and you have no
   record of X, ask them — don't make it up.

---

## Contact / scope reminders

- **User:** Happy Mittal (`happy2332@gmail.com` for personal repo, also has
  Amazon work identity `mithappy@amazon.com` set globally — don't let that
  slip into commits in this repo).
- **Repo identity is per-repo.** `git config user.email` inside this dir
  must show `happy2332@gmail.com`, not the Amazon one.
- **The user is technical** but not a full-time web dev. Explain trade-offs
  briefly, name files and commands explicitly, don't generate hand-wavy plans.
