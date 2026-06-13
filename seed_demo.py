"""Seed the running app with 100 sample vehicles + realistic movement timestamps.

Two phases:
  1. HTTP — register 40 houses + 100 vehicles via the public API.
  2. SQL — write movement rows directly to the SQLite file with custom
     timestamps spread realistically across the last 7 days. Done this
     way because the public /api/log endpoint always uses now().

For the SQL phase, the script prints a Python program and you pipe it
through flyctl ssh:
    python3 seed_demo.py --emit-sql | flyctl ssh console --command "python3 -"

Or run end-to-end against prod:
    python3 seed_demo.py --target https://gatekeeping-sector7.fly.dev --reseed-prod

Locally (no Fly):
    python3 seed_demo.py --target http://127.0.0.1:5057 --local-db ./gatekeeping.db
"""
import argparse
import http.cookiejar
import json
import os
import random
import sqlite3
import subprocess
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

random.seed(42)

IST = timezone(timedelta(hours=5, minutes=30))

STATES = ["DL", "HR", "UP", "KA", "MH", "RJ", "PB", "TN", "MP", "GJ"]
RTOS = [f"{i:02d}{chr(65+j)}" for i in range(1, 30) for j in range(5)]
LETTERS = ["AB", "BC", "CD", "EF", "GH", "JK", "MN", "PQ", "RS", "TU", "FM", "XY"]
NAMES = [
    "Mukesh Gupta", "Anita Sharma", "Rajesh Iyer", "Priya Banerjee",
    "Vikram Singh", "Neha Patel", "Arjun Mehta", "Kavita Roy",
    "Sanjay Verma", "Ritu Kapoor", "Suresh Reddy", "Pooja Nair",
    "Amit Khan", "Deepa Chopra", "Rahul Joshi", "Sunita Das",
    "Manoj Pillai", "Geeta Saxena", "Ashok Bose", "Lalita Rao",
    "Vinod Trivedi", "Neelam Desai", "Pradeep Yadav", "Anjali Mishra",
    "Ravi Shankar", "Smita Khanna", "Naveen Bhat", "Maya Tiwari",
    "Karan Bedi", "Shalini Chatterjee", "Gopal Iyengar", "Rekha Mathur",
]
FLOORS = ["ground", "first", "second", "third", "fourth"]


def make_plate(last4=None):
    return (
        random.choice(STATES)
        + random.choice(RTOS)
        + random.choice(LETTERS)
        + (last4 or f"{random.randint(0, 9999):04d}")
    )


def realistic_ts(now, days_back=7):
    """Pick a random timestamp in the last `days_back` days, biased toward
    morning rush (8–10), lunch dip (13–14), and evening rush (18–21)."""
    delta_days = random.uniform(0, days_back)
    base = now - timedelta(days=delta_days)
    # Pick a typical activity hour
    bucket = random.choices(
        [(8, 10), (13, 14), (18, 21), (10, 18), (21, 23), (5, 8)],
        weights=[3, 1, 4, 2, 1, 1],
    )[0]
    hour = random.uniform(*bucket)
    base = base.replace(hour=int(hour), minute=random.randint(0, 59),
                        second=random.randint(0, 59), microsecond=0)
    return base


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_dataset():
    """Build houses + vehicles + movements (with realistic ts) but don't apply them yet."""
    nums = sorted(random.sample(range(100, 1000), 30))
    houses = []  # list of (number, floor, owner, phone)
    for n in nums[:20]:
        houses.append((str(n), random.choice(FLOORS), random.choice(NAMES),
                       f"98{random.randint(10000000, 99999999)}"))
    for n in nums[20:]:
        for fl in random.sample(FLOORS, 2):
            houses.append((str(n), fl, random.choice(NAMES),
                           f"98{random.randint(10000000, 99999999)}"))

    # 100 plates with intentional last-4 collisions
    plates = []
    for last4, count in [("1234", 5), ("8872", 4), ("5678", 3), ("9999", 3)]:
        while sum(1 for p in plates if p.endswith(last4)) < count:
            p = make_plate(last4)
            if p not in plates:
                plates.append(p)
    while len(plates) < 100:
        p = make_plate()
        if p not in plates:
            plates.append(p)
    random.shuffle(plates)

    # Distribute plates across houses (1–4 each, 1 minimum)
    house_plates = {i: [] for i in range(len(houses))}
    pi = 0
    for i in range(len(houses)):
        house_plates[i].append(plates[pi]); pi += 1
    for p in plates[pi:]:
        candidates = [i for i in range(len(houses)) if len(house_plates[i]) < 4]
        if not candidates:
            break
        house_plates[random.choice(candidates)].append(p)

    # Build movement rows with realistic timestamps.
    # We'll have a mix: residents that are currently inside, residents that did
    # round-trips (IN < OUT), visitors currently inside, visitors with round-trips.
    now = datetime.now(IST).replace(tzinfo=None)
    assignments = []  # list of (plate, num, floor)
    for i, (num, fl, _, _) in enumerate(houses):
        for p in house_plates[i]:
            assignments.append((p, num, fl))
    random.shuffle(assignments)

    movements = []  # (plate, kind, direction, house_number, ts)

    # 40 residents currently inside — single IN, sometime in the last 7 days
    for plate, num, _ in assignments[:40]:
        movements.append((plate, "resident", "in", num, realistic_ts(now)))

    # 15 resident round-trips (IN earlier, OUT later same day)
    for plate, num, _ in assignments[40:55]:
        in_ts = realistic_ts(now)
        # OUT 1–12 hours after IN
        out_ts = in_ts + timedelta(hours=random.uniform(1, 12),
                                   minutes=random.randint(0, 59))
        # Don't let OUT go into the future
        if out_ts > now:
            out_ts = now - timedelta(minutes=random.randint(5, 60))
        if out_ts <= in_ts:
            out_ts = in_ts + timedelta(minutes=15)
        movements.append((plate, "resident", "in", num, in_ts))
        movements.append((plate, "resident", "out", num, out_ts))

    # 10 visitor IN (currently inside)
    used_visitor = set()
    host_pool = [h[0] for h in houses]
    for _ in range(10):
        last4 = random.choice(["1234", None, None, "5678", None])
        plate = make_plate(last4)
        while plate in used_visitor:
            plate = make_plate(last4)
        used_visitor.add(plate)
        num = random.choice(host_pool)
        movements.append((plate, "visitor", "in", num, realistic_ts(now, days_back=2)))

    # 5 visitor round-trips
    for _ in range(5):
        plate = make_plate()
        while plate in used_visitor:
            plate = make_plate()
        used_visitor.add(plate)
        num = random.choice(host_pool)
        in_ts = realistic_ts(now, days_back=3)
        out_ts = in_ts + timedelta(minutes=random.randint(20, 240))
        if out_ts > now:
            out_ts = now - timedelta(minutes=random.randint(5, 30))
        if out_ts <= in_ts:
            out_ts = in_ts + timedelta(minutes=15)
        movements.append((plate, "visitor", "in", num, in_ts))
        movements.append((plate, "visitor", "out", num, out_ts))

    # Sort movements by timestamp so the auto-increment id reflects chronology
    movements.sort(key=lambda m: m[4])

    return houses, house_plates, movements


def http_register_houses(target, admin_password, houses, house_plates):
    """Phase 1: HTTP register houses + vehicles (timestamps don't matter here)."""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def post(path, form):
        data = urllib.parse.urlencode(form, doseq=True).encode()
        req = urllib.request.Request(
            target + path, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with op.open(req, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    print(f"target: {target}")
    s = post("/login", {"password": admin_password})
    print(f"admin login: HTTP {s}")
    if s not in (200, 302):
        sys.exit(f"admin login failed (status {s})")

    print(f"registering {len(houses)} houses with {sum(len(v) for v in house_plates.values())} vehicles…")
    for i, (num, fl, owner, phone) in enumerate(houses):
        form = [("number", num), ("floor", fl), ("owner_name", owner), ("phone", phone)]
        for p in house_plates[i]:
            form.append(("plate", p))
        s = post("/houses/new", form=form)
        if s not in (200, 302):
            print(f"  WARN: house {num}/{fl} returned {s}")


def emit_sql_program(movements):
    """Print a self-contained Python program that, when executed via
    `flyctl ssh console -C 'python3 -'`, inserts the movement rows."""
    rows_repr = repr([
        (plate, kind, direction, num, fmt_ts(ts))
        for (plate, kind, direction, num, ts) in movements
    ])
    return textwrap.dedent(f"""
        import sqlite3
        rows = {rows_repr}
        c = sqlite3.connect('/data/gatekeeping.db')
        c.row_factory = sqlite3.Row
        v_by_plate = {{r['plate']: r['id'] for r in c.execute('SELECT id, plate FROM vehicles')}}
        v_to_house = {{r['id']: r['house_id'] for r in c.execute('SELECT id, house_id FROM vehicles')}}
        h_by_num = {{}}
        for r in c.execute('SELECT id, number FROM houses'):
            h_by_num.setdefault(r['number'], []).append(r['id'])
        c.execute('DELETE FROM movements')
        inserted = 0
        for plate, kind, direction, num, ts in rows:
            vid = v_by_plate.get(plate)
            hid = v_to_house.get(vid) if vid else (h_by_num.get(num, [None])[0])
            c.execute(
                'INSERT INTO movements (house_id, vehicle_id, plate, kind, direction, ts) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (hid, vid, plate, kind, direction, ts),
            )
            inserted += 1
        c.commit()
        print('inserted', inserted, 'movements')
        print('houses:', c.execute('SELECT COUNT(*) FROM houses').fetchone()[0])
        print('vehicles:', c.execute('SELECT COUNT(*) FROM vehicles').fetchone()[0])
        print('movements:', c.execute('SELECT COUNT(*) FROM movements').fetchone()[0])
        inside = c.execute(
            "SELECT COUNT(*) FROM movements WHERE id IN (SELECT MAX(id) FROM movements GROUP BY plate) AND direction='in'"
        ).fetchone()[0]
        print('currently inside:', inside)
    """).strip()

# Convert sqlite3.Row to subscriptable; we want dict-like access
SETUP_ROW_FACTORY = "import sqlite3\\nimport sqlite3 as _\\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="https://gatekeeping-sector7.fly.dev")
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", "admin"))
    parser.add_argument("--reseed-prod", action="store_true",
                        help="Wipe live DB, register houses via HTTP, then patch movements via flyctl ssh")
    parser.add_argument("--local-db", help="Local SQLite path; if set, write movements directly here instead of via flyctl")
    parser.add_argument("--emit-sql", action="store_true",
                        help="Print the Python program for inserting movements; pipe through flyctl ssh")
    args = parser.parse_args()

    houses, house_plates, movements = build_dataset()

    if args.emit_sql:
        print(emit_sql_program(movements))
        return

    if args.reseed_prod:
        print("--- wiping prod DB ---")
        subprocess.run(
            ["/opt/homebrew/bin/flyctl", "ssh", "console", "-C",
             "rm -f /data/gatekeeping.db /data/gatekeeping.db-journal"],
            check=False,
        )
        print("--- restarting app to recreate empty schema ---")
        subprocess.run(
            ["/opt/homebrew/bin/flyctl", "apps", "restart", "gatekeeping-sector7"],
            check=False,
        )
        import time; time.sleep(8)

    http_register_houses(args.target, args.admin_password, houses, house_plates)

    if args.local_db:
        print(f"--- writing movements directly to {args.local_db} ---")
        c = sqlite3.connect(args.local_db)
        c.row_factory = sqlite3.Row
        v_by_plate = {r["plate"]: r["id"] for r in c.execute("SELECT id, plate FROM vehicles")}
        h_by_num = {}
        for r in c.execute("SELECT id, number FROM houses"):
            h_by_num.setdefault(r["number"], []).append(r["id"])
        c.execute("DELETE FROM movements")
        for plate, kind, direction, num, ts in movements:
            vid = v_by_plate.get(plate)
            if vid is not None:
                hid = c.execute("SELECT house_id FROM vehicles WHERE id = ?", (vid,)).fetchone()["house_id"]
            else:
                hids = h_by_num.get(num, [])
                hid = hids[0] if hids else None
            c.execute(
                "INSERT INTO movements (house_id, vehicle_id, plate, kind, direction, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (hid, vid, plate, kind, direction, fmt_ts(ts)),
            )
        c.commit()
        print(f"inserted {len(movements)} movements")
        return

    # Default path: write the program to a temp file on the prod machine
    # via flyctl ssh sftp, then execute it. Inlining 90 rows blows past the
    # SSH command-line limit, so we ship it as a file.
    print("--- writing movements via flyctl ssh ---")
    program = emit_sql_program(movements)
    # 1. Stage the program locally
    local_path = "/tmp/gk_seed_movements.py"
    with open(local_path, "w") as f:
        f.write(program)
    # 2. Push to /tmp on the Fly machine via sftp
    sftp = subprocess.Popen(
        ["/opt/homebrew/bin/flyctl", "ssh", "sftp", "shell"],
        stdin=subprocess.PIPE, text=True,
    )
    sftp.communicate(f"put {local_path} /tmp/gk_seed_movements.py\n")
    if sftp.returncode != 0:
        sys.exit("flyctl ssh sftp put failed")
    # 3. Execute it
    p = subprocess.run(
        ["/opt/homebrew/bin/flyctl", "ssh", "console", "-C",
         "python3 /tmp/gk_seed_movements.py"],
        check=False,
    )
    if p.returncode != 0:
        sys.exit("flyctl ssh exec failed")


if __name__ == "__main__":
    main()
