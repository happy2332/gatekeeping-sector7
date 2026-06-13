"""Seed the running app with 100 sample vehicles for demo purposes.

Hits the public HTTP API the same way real users will. Run from any machine
that can reach the target URL.

Usage:
    python3 seed_demo.py --target https://gatekeeping-sector7.fly.dev
    python3 seed_demo.py --target http://127.0.0.1:5057  # local

Wipe the DB first if you want a clean slate (this script does NOT wipe).
"""
import argparse
import http.cookiejar
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

random.seed(42)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="https://gatekeeping-sector7.fly.dev")
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", "admin"))
    parser.add_argument("--guard-password", default=os.environ.get("GUARD_PASSWORD", "guard"))
    args = parser.parse_args()

    admin_jar = http.cookiejar.CookieJar()
    guard_jar = http.cookiejar.CookieJar()
    admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(admin_jar))
    guard = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(guard_jar))

    def post(opener, path, form=None, json_body=None):
        url = args.target + path
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers = {"Content-Type": "application/json"}
        else:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with opener.open(req, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    print(f"target: {args.target}")
    s = post(admin, "/login", form={"password": args.admin_password})
    print(f"admin login: HTTP {s}")
    if s not in (200, 302):
        sys.exit(f"admin login failed (status {s})")
    s = post(guard, "/login", form={"password": args.guard_password})
    print(f"guard login: HTTP {s}")

    # 30 unique house numbers, 10 of which have 2 floors → 40 houses total
    nums = sorted(random.sample(range(100, 1000), 30))
    houses = []
    for n in nums[:20]:
        houses.append((str(n), random.choice(FLOORS)))
    for n in nums[20:]:
        for fl in random.sample(FLOORS, 2):
            houses.append((str(n), fl))

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

    # 1 plate per house, then distribute the remaining 60 randomly (max 4 per house)
    house_plates = {i: [] for i in range(len(houses))}
    pi = 0
    for i in range(len(houses)):
        house_plates[i].append(plates[pi]); pi += 1
    for p in plates[pi:]:
        candidates = [i for i in range(len(houses)) if len(house_plates[i]) < 4]
        if not candidates:
            break
        house_plates[random.choice(candidates)].append(p)

    print(f"registering {len(houses)} houses with {sum(len(v) for v in house_plates.values())} vehicles…")
    for i, (num, fl) in enumerate(houses):
        owner = random.choice(NAMES)
        phone = f"98{random.randint(10000000, 99999999)}"
        form = [("number", num), ("floor", fl), ("owner_name", owner), ("phone", phone)]
        for p in house_plates[i]:
            form.append(("plate", p))
        s = post(admin, "/houses/new", form=form)
        if s not in (200, 302):
            print(f"  WARN: house {num}/{fl} returned {s}")

    # build a flat list of (plate, num, fl)
    assignments = []
    for i, (num, fl) in enumerate(houses):
        for p in house_plates[i]:
            assignments.append((p, num, fl))
    random.shuffle(assignments)

    inside_now = assignments[:40]
    round_trip = assignments[40:55]

    print(f"logging {len(inside_now)} resident IN (currently inside)…")
    for plate, num, _ in inside_now:
        post(guard, "/api/log", json_body={
            "plate": plate, "direction": "in", "kind": "resident", "house_number": num,
        })

    print(f"logging {len(round_trip)} resident round-trips (IN then OUT)…")
    for plate, num, _ in round_trip:
        post(guard, "/api/log", json_body={
            "plate": plate, "direction": "in", "kind": "resident", "house_number": num,
        })
        post(guard, "/api/log", json_body={
            "plate": plate, "direction": "out", "kind": "resident", "house_number": num,
        })

    print("logging 10 visitor IN (currently inside) + 5 visitor round-trips…")
    host_pool = [(h[0], h[1]) for h in houses]
    used_visitor = set()
    for _ in range(10):
        # mix of last-4 collision groups + random
        last4 = random.choice(["1234", None, None, "5678", None])
        plate = make_plate(last4)
        while plate in used_visitor:
            plate = make_plate(last4)
        used_visitor.add(plate)
        num, _ = random.choice(host_pool)
        post(guard, "/api/log", json_body={
            "plate": plate, "direction": "in", "kind": "visitor", "house_number": num,
        })
    for _ in range(5):
        plate = make_plate()
        while plate in used_visitor:
            plate = make_plate()
        used_visitor.add(plate)
        num, _ = random.choice(host_pool)
        post(guard, "/api/log", json_body={
            "plate": plate, "direction": "in", "kind": "visitor", "house_number": num,
        })
        post(guard, "/api/log", json_body={
            "plate": plate, "direction": "out", "kind": "visitor", "house_number": num,
        })

    print("seeding complete.")


if __name__ == "__main__":
    main()
