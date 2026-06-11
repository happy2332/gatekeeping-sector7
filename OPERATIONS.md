# Operations — Gatekeeping (Sector 7 Pocket C)

Live URL: **https://gatekeeping-sector7.fly.dev**
Hosting: Fly.io free tier · region `bom` (Mumbai) · 1 GB volume `gk_data` mounted at `/data`
Source: https://github.com/happy2332/gatekeeping-sector7

All commands assume your current directory is the project root:

```bash
cd /Users/mithappy/work/gatekeeping_sector7
```

---

## Reset / change passwords

Each `secrets set` triggers a tiny redeploy (~30 sec). Logged-in sessions are unaffected unless you also rotate `GK_SECRET`.

```bash
# Admin password only
flyctl secrets set ADMIN_PASSWORD='new-strong-password'

# Guard password only
flyctl secrets set GUARD_PASSWORD='new-guard-password'

# Both at once
flyctl secrets set ADMIN_PASSWORD='...' GUARD_PASSWORD='...'
```

See currently configured secrets (names only, never values):

```bash
flyctl secrets list
```

Rotate the Flask session key — immediately logs everyone out. Use if you suspect a session leak:

```bash
flyctl secrets set GK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

---

## Common operations

| What you want to do | Command |
|---|---|
| Deploy after editing code | `git commit -am "msg" && flyctl deploy --remote-only` |
| See live logs | `flyctl logs` |
| Open the Fly dashboard | `flyctl dashboard` |
| Restart the app | `flyctl apps restart gatekeeping-sector7` |
| List secrets | `flyctl secrets list` |
| Set/change a secret | `flyctl secrets set KEY=value` |
| SSH into the running container | `flyctl ssh console` |
| Snapshot the DB inside the volume | `flyctl ssh console -C 'cp /data/gatekeeping.db /data/gatekeeping.backup.db'` |
| Download the DB to your Mac | `flyctl ssh sftp get /data/gatekeeping.db ./local-backup.db` |
| Upload a fresh DB to the volume | `flyctl ssh sftp shell` then `put ./local.db /data/gatekeeping.db` |

---

## Local development

```bash
.venv/bin/python app.py
# default port 5057, override with PORT=8080
# default DB at ./gatekeeping.db, override with GK_DB=/path/to.db
# default passwords admin / guard, override with ADMIN_PASSWORD / GUARD_PASSWORD
```

URL on your laptop: http://127.0.0.1:5057
URL from your phone (same Wi-Fi): `http://<your-mac-ip>:5057` — find IP with `ipconfig getifaddr en0`.

### Run the test suites

Each suite expects a clean DB and runs against an isolated test server on port 5058. Run them one at a time:

```bash
# Stop any test server still running
kill $(lsof -nP -iTCP:5058 -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null
rm -f /tmp/gk_test.db

# Boot a clean server
GK_DB=/tmp/gk_test.db PORT=5058 ADMIN_PASSWORD=admin GUARD_PASSWORD=guard \
  .venv/bin/python app.py > /tmp/gk_test_server.log 2>&1 &
sleep 1.5

# Run any one of the three suites
.venv/bin/python run_tests.py        # 34 tests: houses + vehicles CRUD
.venv/bin/python run_gate_tests.py   # 56 tests: gate flow, search, IN/OUT, validation
.venv/bin/python run_admin_tests.py  # 35 tests: admin tab, role boundaries

# Cleanup
kill $(lsof -nP -iTCP:5058 -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null
rm -f /tmp/gk_test.db
```

Total: 125 tests across the three suites. Each suite needs an empty DB — running two back-to-back without `rm -f /tmp/gk_test.db` will fail because of stale fixtures.

---

## Backups

Fly takes daily volume snapshots (5 retained, free). To pull a manual backup to your Mac whenever you want:

```bash
flyctl ssh sftp get /data/gatekeeping.db ./gatekeeping-backup-$(date +%Y%m%d).db
```

For a longer retention CSV of just the movement log: log in as admin in the browser → **Admin** → **Download CSV**.

---

## Roles and what each can do

| URL | Resident (no login) | Guard | Admin |
|---|---|---|---|
| `/` (Inside) | ✅ | ✅ | ✅ |
| `/log` | ✅ | ✅ | ✅ |
| `/houses` (read-only) | ✅ | ✅ | ✅ |
| House detail page (incl. tap-to-call phone) | ✅ | ✅ | ✅ |
| Vehicle search | ✅ | ✅ | ✅ |
| `/gate` (log IN/OUT) | ❌ | ✅ | ✅ |
| Add/edit/delete vehicles | ❌ | ❌ | ✅ |
| Add/edit/delete houses | ❌ | ❌ | ✅ |
| Clear logs / CSV export / delete house | ❌ | ❌ | ✅ |

Login at `/login`. The password entered chooses the role:
- matches `ADMIN_PASSWORD` → admin
- matches `GUARD_PASSWORD` → guard
- else → "Incorrect password"

Residents don't sign in.

---

## Fly free-tier limits to watch

- **3 small VMs** (256 MB each). We use 1.
- **3 GB of volume storage**. We allocated 1 GB — plenty for SQLite holding many years of logs.
- **160 GB outbound transfer/month**. A society of 100 phones won't come close.
- **Always-on** (`min_machines_running = 1` in `fly.toml`) so the guard doesn't hit a cold start.

If usage stays in free limits, the credit card on file is never charged.

---

## When something is wrong

| Symptom | First thing to try |
|---|---|
| App not loading | `flyctl logs` to see the live error stream |
| 500 errors | `flyctl logs --no-tail` and read the most recent traceback |
| Forgot admin password | `flyctl secrets set ADMIN_PASSWORD='...'`  — instant reset |
| DB looks corrupted | Restore from snapshot via `flyctl volumes snapshots list` and `flyctl volumes snapshots restore` |
| App stuck after deploy | `flyctl apps restart gatekeeping-sector7` |
| Need to roll back | `flyctl releases` to list, `flyctl releases rollback <id>` |
