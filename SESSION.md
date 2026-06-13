# Session handoff — Gatekeeper (Sector 7 Pocket C)

What a fresh Claude session needs to pick up where the previous one left off:
what the app does, the design choices, what's shipped, and the open follow-ups.

> Read this first, then `OPERATIONS.md`, then the source. Don't guess at
> history — `git log --oneline` is the source of truth for what shipped and when.

---

## What this app is

A mobile-friendly web app for the gatekeeper at a residential pocket
(Sector 7 Pocket C). The model is **vehicle-centric**: every plate registered
to a house is a row in `/vehicles`; the guard logs IN/OUT at `/gate`; the live
"who is inside" view is at `/`; the timeline is at `/log`.

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

Local dev: `.venv/bin/python app.py` (default port 5057, override with PORT).

---

## Stack and shape

- **Flask** + **SQLite**, single-process, gunicorn in production.
- Templates with Jinja2, vanilla CSS, vanilla JS (no framework, no build step).
- ~12 source files, ~900 lines of Python, mobile-first responsive design.
- `app.py` holds everything: routes, schema, role helpers, Jinja filters,
  context processors. Don't split unless it crosses ~1500 lines — single-file
  is part of the deal.

### Files at a glance

```
app.py                       # all routes + DB code + validators + filters
fly.toml                     # Fly config (region bom, volume gk_data)
Dockerfile                   # python:3.12-slim + tzdata for IST
requirements.txt             # Flask + gunicorn
seed_demo.py                 # one-shot 100-vehicle demo seeder (HTTP + flyctl ssh)

static/style.css             # mobile-first; ≤640px collapses tables to cards
static/sortable.js           # desktop click-to-sort + mobile "Sort by…" dropdown

templates/base.html          # topbar (gradient + brand mark), nav, footer
templates/login.html         # single password field; role chosen by match
templates/gate.html          # vehicle search + visitor form (guard/admin only)
templates/index.html         # "Currently inside" + Recent activity (everyone)
templates/log.html           # full movement log + filters/search (everyone)
templates/vehicles.html      # vehicle list (everyone) + edit/delete for admin
templates/vehicle_new.html   # dedicated "Add a vehicle" form (everyone)
templates/vehicle_edit.html  # admin-only per-vehicle edit form
templates/admin.html         # stats, CSV export, clear-logs (admin only)

run_tests.py                 # 34 tests: house + vehicle CRUD, validators
run_gate_tests.py            # 53 tests: gate flow, search, IN/OUT
run_admin_tests.py           # 37 tests: admin tab, role boundaries

OPERATIONS.md                # ops cheat sheet
SESSION.md                   # this file
```

---

## The three roles

The user explicitly chose this model:

> Resident is open / no login. Single login page — role chosen by password.
> Residents see ALL plates and ALL logs (society-wide visibility).

| URL                                  | Resident (no login) | Guard | Admin |
|--------------------------------------|---------------------|-------|-------|
| `/` (Inside)                         | ✅                  | ✅    | ✅    |
| `/log`                               | ✅                  | ✅    | ✅    |
| `/vehicles`                          | ✅                  | ✅    | ✅    |
| `/vehicles/new` (register)           | ✅                  | ✅    | ✅    |
| Vehicle search via `/api/...`        | ✅                  | ✅    | ✅    |
| `/gate` (log IN/OUT)                 | ❌ → /login         | ✅    | ✅    |
| Edit a vehicle (`/admin/vehicles/<id>/edit`) | ❌          | ❌    | ✅    |
| Delete a vehicle (`/admin/vehicles/<id>/delete`) | ❌      | ❌    | ✅    |
| Admin tab (CSV export, clear logs)   | ❌                  | ❌    | ✅    |

Login at `/login`. Password matches `ADMIN_PASSWORD` → admin; matches
`GUARD_PASSWORD` → guard; else "Incorrect password". Residents never sign in.

`@guard_required` and `@admin_required` decorators in `app.py`; `role_at_least()`
for inline checks; `inject_role` context processor exposes `is_resident`,
`is_guard`, `is_admin` to templates.

---

## Data model

```
houses
  id PK
  number TEXT NOT NULL CHECK(CAST(number AS INTEGER) BETWEEN 100 AND 999)
  owner_name TEXT NOT NULL
  phone TEXT NOT NULL
  floor TEXT NOT NULL CHECK(LOWER(floor) IN
    ('ground','first','second','third','fourth'))
  phone_masked INTEGER NOT NULL DEFAULT 0
  UNIQUE(number, floor)

vehicles
  id PK
  house_id FK → houses(id) ON DELETE CASCADE
  plate TEXT UNIQUE         -- normalised: uppercase, strip non-alphanumeric
  make_model TEXT           -- legacy column, no longer in UI
  colour TEXT               -- same

movements
  id PK
  house_id FK → houses(id) (nullable; legacy / unknown)
  vehicle_id FK → vehicles(id) (nullable; visitors aren't registered)
  plate TEXT
  kind TEXT CHECK IN ('resident','visitor','unknown')
  direction TEXT CHECK IN ('in','out')
  visitor_name TEXT          -- legacy, no longer captured by UI
  visitor_phone TEXT         -- same
  note TEXT                  -- same
  ts TEXT NOT NULL           -- IST wall-time, Python-supplied
```

### Schema is strict and authoritative

The CHECK + UNIQUE constraints are enforced at the SQLite level; no app-side
gymnastics needed. Validators (`validate_house_number`, `validate_floor`) in
`app.py` reject bad input before the DB ever sees it, with friendly flash
messages.

### Houses auto-delete with their last vehicle

The model is fully vehicle-centric: a house only exists while at least one
vehicle is registered to it. On the admin "remove vehicle" path, if the
deleted vehicle was the house's last one, the house row is dropped too.
Movement-log rows that referenced that house keep their plate text (display
shows the plate; house lookup goes via `vehicle.house_id` which is now NULL).

### `/houses/<id>` no longer exists

There is no per-house view anywhere. Everything is reachable from the
vehicle list. `/houses` is also gone (404). URL is `/vehicles`.

---

## Notable design choices and why

### Plate normalisation
Plates are stripped of all non-alphanumeric characters and uppercased on input
(`normalise_plate()`). `dl 3c ab 1234` → `DL3CAB1234`. Means the same physical
car can't be added twice to different houses, and gate search is case- and
space-insensitive.

### IST timestamps stored, not derived
We compute `now_ist_str()` in Python with a fixed `+05:30` offset and pass the
string to SQLite. Earlier the schema used `datetime('now', 'localtime')` which
on Fly's UTC containers stored UTC times. The Dockerfile also installs `tzdata`
and sets `TZ=Asia/Kolkata` as a belt-and-braces fix.

### Phone masking is feature-flagged off
Per-house `phone_masked` boolean exists in the schema, but the toggle is
hidden in the UI (env var `GK_PHONE_MASKING`, default off). Every role sees
every phone. To re-enable: `flyctl secrets set GK_PHONE_MASKING=1`.

### Mobile responsive tables
At ≤640px every `<table class="data">` collapses to a vertical stack of cards.
Each `<td>` carries `data-label="..."` which the CSS shows as a small
uppercase prefix. Wide tables (e.g. `/log` with 8 columns) are wrapped in
`<div class="table-wrap">` for horizontal scroll-on-overflow on desktop.

### Mobile sort dropdown
At ≤640px the table headers are hidden (cards layout), so click-to-sort
isn't reachable. `sortable.js` injects a `<select>` "Sort by…" above each
sortable table at mobile widths. Each sortable column appears twice (↑ asc,
↓ desc). Reuses the same `applySort()` function as desktop.

### Why house numbers 100–999 + 5 floors
The user's pocket has flat numbers in the 100–999 range and at most 5 levels.
The form uses an `<input type="number" min="100" max="999">` plus a `<datalist>`
of all 900 candidates so the browser does type-to-search out of the box. Floor
is a `<select>` of fixed values (`ground`, `first`, `second`, `third`, `fourth`).
Owner name and phone are required.

### Smart "Add a vehicle" form
The form lives at `/vehicles/new`. As the user picks a number and floor, it
hits `/api/houses/lookup` and either:
- Auto-fills owner+phone (read-only) and switches the button to
  "Add vehicle to N (Floor)" if the unit already exists.
- Stays in new-house mode otherwise.

This lets a resident with a previously-registered house add a second car
without re-typing owner/phone.

### Admin edit on `/vehicles`
`/admin/vehicles/<id>/edit` lets admin change the plate, owner, phone, or
move the vehicle to a different (number, floor). Plate uniqueness checked
case+space-insensitively. Plate rename also updates `movements.plate` so the
log keeps finding the vehicle. Move-to-different-house updates the vehicle's
`house_id`; if the source house's last vehicle just left, the source house is
auto-deleted (per the rule above).

### Session secret
`GK_SECRET` env var if set; otherwise `secrets.token_hex(16)` per process.
On Fly, `GK_SECRET` is set so admin sessions survive restarts. Don't remove
the fallback — it keeps `python app.py` working locally without env vars.

### Why no users table
Role-by-password was an explicit choice. Per-user accounts add a lot of UI
(create / list / disable users) for an MVP that has 1 admin and 1–2 guards.
If/when the society grows or wants per-guard audit trails, switch to a real
`users` table. Until then, do not add it.

---

## UI polish

`static/style.css` was deliberately polished but kept utilitarian:

- Topbar: indigo→purple gradient with a small gold accent mark beside the
  brand title.
- Cards: soft shadow + 1px hairline border; rounded corners.
- Buttons: subtle vertical gradients (green for IN, red for OUT, dark for
  default), tiny press animation.
- Badges: 1px coloured border in the badge's family.
- Filter pills: brand-coloured gradient on the active pill.
- Table rows: subtle hover highlight on desktop.
- Footer: small attribution line with mailto + WhatsApp on every page.

No design system dependency, no build step, no JS framework.

---

## Filters and counters on each tab

- `/` (Inside) — filter pills (Resident / Visitor / All); search by vehicle,
  house, owner; counter badge shows `inside_total / vehicles_total`.
- `/log` — filter pills (kind + status); search; only Time / House / Owner
  are sortable.
- `/vehicles` — search; only House and Owner sortable; counter badge shows
  total vehicles; muted line shows total houses registered.

---

## Tests

124 total across three suites. Each suite expects an empty DB.

Run pattern (one suite at a time):
```bash
kill $(lsof -nP -iTCP:5058 -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null
rm -f /tmp/gk_test.db
GK_DB=/tmp/gk_test.db PORT=5058 ADMIN_PASSWORD=admin GUARD_PASSWORD=guard \
  .venv/bin/python app.py > /tmp/gk_test_server.log 2>&1 &
sleep 1.5
.venv/bin/python run_tests.py
# repeat for run_gate_tests.py, run_admin_tests.py with rm -f between
```

The tests parse rendered HTML with regex. When you change templates, expect
to update patterns. Common gotchas the regexes account for:
- `<td>` may have `data-label="..."` attributes
- House lookups use `/api/houses/lookup?number=&floor=` (no `/houses/<id>` URL exists)
- Some tests use a no-redirect opener (`_bare`) when posting to admin routes
  so curl doesn't try to re-POST the redirect target.

---

## Open follow-ups (rough priority)

1. **App name decided: "Gatekeeper".** Brand title in topbar reads
   "Gatekeeper · Sector 7 Pocket C" everywhere. Short, simple to pronounce,
   self-explanatory for elders.
2. **Replace placeholder Fly secrets.** `ADMIN_PASSWORD` and `GUARD_PASSWORD`
   were set to `admin` / `guard` during initial deploy. Confirm with the user
   if they've been rotated; rotate via `flyctl secrets set ...` if not.
3. **GitHub PAT.** A fine-grained PAT was pasted in chat earlier this session
   to push the initial commit. Should be revoked at
   https://github.com/settings/tokens?type=beta if not already.
4. **Phone number on public footer.** The footer includes a WhatsApp link to
   the user's personal number. User explicitly opted in. If they ever want it
   pulled, edit `templates/base.html`.
5. **Backups.** Fly retains 5 daily volume snapshots automatically. For
   external backups, `flyctl ssh sftp get /data/gatekeeping.db <local>`.
6. **Photo of visitor's vehicle.** Frequently asked-for; not built. Would use
   `<input type="file" accept="image/*" capture="environment">`.
7. **Resident notification when visitor arrives.** Telegram bot or Twilio.
8. **Per-guard accounting.** Same as the "no users table" decision.
9. **Auto-clear stale "currently inside".** A vehicle logged IN with no later
   OUT stays inside forever. Open question: auto-OUT at 04:00 IST? Warn admin
   after N hours?

---

## Things to NOT do unless asked

- Don't introduce a frontend framework. The vanilla JS works.
- Don't split `app.py`. ~900 lines is fine in one file.
- Don't add user accounts. Role-by-password is the explicit choice.
- Don't bring back per-house pages or links. Vehicle-centric is the model.
- Don't drop the legacy `make_model` / `colour` / `visitor_name` columns —
  they're empty for new rows but the migration cost would exceed the savings.
- Don't add CSS preprocessors. Keep `static/style.css` plain.
- Don't add Postgres or connection pooling. SQLite is fine for ~100 events/day.

---

## Quick orientation for a new session

1. Read this file. (Done.)
2. `git log --oneline -20` to see anything shipped after this was written.
3. Skim `app.py` — the code is the source of truth.
4. Look at the most recent template the user is talking about.
5. Make the change. Run the relevant test suite. Deploy with
   `flyctl deploy --remote-only` from the project root.
6. If the user mentions "Claude said earlier we'd do X" and you have no
   record of X, ask them — don't make it up.

---

## Contact / scope reminders

- **User:** Happy Mittal (`happy2332@gmail.com` for the personal repo;
  Amazon work identity `mithappy@amazon.com` is the global git default —
  don't let it slip into commits in this repo).
- **Repo identity is per-repo.** `git config user.email` inside this dir
  must show `happy2332@gmail.com`, not the Amazon one.
- **Co-author / second name** in the footer: Arnab Samanta.
- **The user is technical** but not a full-time web dev. Explain trade-offs
  briefly, name files and commands explicitly, no hand-wavy plans.
