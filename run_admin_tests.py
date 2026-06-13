"""Admin-tab tests. Hits a running server (default http://127.0.0.1:5058)."""
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("GK_BASE", "http://127.0.0.1:5058")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
_jar = http.cookiejar.CookieJar()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None


_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_jar),
    NoRedirect(),
)

PASS = 0
FAIL = 0
RESULTS = []


def request(method, path, form=None, json_body=None):
    url = BASE + path
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), e.headers.get("Location", "")


def get(path):  return request("GET", path)
def post(path, **kw): return request("POST", path, **kw)


def follow(path):
    """GET that follows redirects (used to read the page after a POST)."""
    status, body, loc = request("GET", path)
    while status in (301, 302, 303, 307, 308) and loc:
        status, body, loc = request("GET", loc)
    return status, body


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
    RESULTS.append((condition, name, detail))


def section(title): print(f"\n=== {title} ===")


def get_house_id(number, floor):
    status, body, _ = request("GET", f"/api/houses/lookup?number={number}&floor={floor.lower()}")
    data = json.loads(body)
    return data["matches"][0]["id"] if data["matches"] else None


# ----- setup data -----
section("SETUP")
# Log in as admin to seed houses + vehicles, then log out before AUTH section.
post("/login", form={"password": ADMIN_PASSWORD})
post("/houses/new", form={"number": "101", "floor": "ground", "owner_name": "Sharma", "phone": "9000000001", "plate": "DL3CAB1234"})
post("/houses/new", form={"number": "102", "floor": "first",  "owner_name": "Kapoor", "phone": "9000000002", "plate": "DL8CAF7788"})
post("/houses/new", form={"number": "201", "floor": "ground", "owner_name": "Iyer",   "phone": "9000000004", "plate": "HR26DK9999"})
a101 = get_house_id("101", "Ground")
a102 = get_house_id("102", "First")
b201 = get_house_id("201", "Ground")
post("/api/log", json_body={"plate": "DL3CAB1234", "direction": "in", "kind": "resident", "house_id": a101})
post("/api/log", json_body={"plate": "DL3CAB1234", "direction": "out", "kind": "resident", "house_id": a101})
post("/api/log", json_body={"plate": "X1Y2", "direction": "in", "kind": "visitor", "house_id": int(a101)})
post("/logout")
check("setup completed", a101 and a102 and b201)


# ----- auth -----
section("AUTH")
status, body, loc = get("/admin")
check("/admin without login redirects", status in (301, 302) and "/login" in (loc or ""),
      f"status={status} loc={loc}")

status, _, _ = get("/login")
check("/login renders", status == 200)

# Resident (no login) can read /, /log, /houses
status, _, _ = get("/")
check("resident: GET / works without login", status == 200)
status, _, _ = get("/log")
check("resident: GET /log works without login", status == 200)
status, _, _ = get("/vehicles")
check("resident: GET /vehicles works without login", status == 200)
status, body, _ = get("/vehicles")
check("resident: '+ Add vehicle' button IS shown on /vehicles",
      "/vehicles/new" in body and "Add vehicle" in body)
check("resident: 'edit →' link NOT shown",
      "edit →" not in body)
# Resident cannot post log
status, _, _ = post("/api/log",
    json_body={"plate": "ZZZ", "direction": "in", "kind": "visitor", "house_number": "101"})
check("resident cannot POST /api/log",
      status in (301, 302), f"got status={status}")
# Resident cannot reach /gate
status, _, loc = get("/gate")
check("resident cannot GET /gate (redirected)",
      status in (301, 302) and "/login" in (loc or ""))

# Wrong password — flash appears on the POST response itself (200, re-renders form)
status, body, _ = post("/login", form={"password": "wrong"})
check("wrong password keeps user out — flash 'Incorrect password'",
      "incorrect password" in body.lower(), f"status={status}")

status, body, loc = get("/admin")
check("still no /admin access after wrong password",
      status in (301, 302) and "/login" in (loc or ""))

# Correct password
status, body, loc = post("/login", form={"password": ADMIN_PASSWORD})
check("correct password redirects to admin",
      status in (301, 302) and ("/admin" in (loc or "")),
      f"status={status} loc={loc}")
status, body = follow("/admin")
check("/admin renders after login", status == 200 and "Admin" in body)
check("/admin shows house counts", "3 houses" in body)
check("/admin shows movement count", "3 log entries" in body)


# ----- guard role boundary -----
section("GUARD ROLE")
# Re-login as guard
post("/logout")
status, body, _ = post("/login", form={"password": GUARD_PASSWORD if (GUARD_PASSWORD := os.environ.get('GUARD_PASSWORD','guard')) else 'guard'})
status, _, _ = get("/admin")
check("guard cannot reach /admin (redirected to login)", status in (301, 302))
status, _, _ = post("/houses/new", form={"number": "555", "floor": "ground", "owner_name": "X", "phone": "1"})
check("guard CAN create houses (residents can too)", status in (200, 302) and status not in (401, 403))
# But guard CAN log via api/log
status, _, _ = post("/api/log",
    json_body={"plate": "GUARD1", "direction": "in", "kind": "visitor", "house_number": "101"})
check("guard CAN POST /api/log", status == 200)
# Re-login as admin for the rest of the suite
post("/logout")
post("/login", form={"password": ADMIN_PASSWORD})


# ----- delete vehicle (auto-deletes house when last vehicle) -----
section("DELETE VEHICLE")
# Find HR26DK9999's vehicle id from /vehicles
_, vbody = follow("/vehicles")
m = re.search(r'HR26DK9999.*?/admin/vehicles/(\d+)/delete', vbody, re.S)
v_id = int(m.group(1)) if m else None
check("can resolve HR26DK9999 vehicle id", v_id is not None)
post(f"/admin/vehicles/{v_id}/delete")
follow("/vehicles")  # consume flash
_, vbody = follow("/vehicles")
check("vehicle HR26DK9999 removed", "HR26DK9999" not in vbody)
# 201 had only one vehicle, so the house is gone too (auto-delete)
check("house 201 also deleted (was its last vehicle)",
      not re.search(r"<strong>201</strong>", vbody))
# Search confirms it's not registered anywhere
status, body, _ = get("/api/vehicles/search?q=HR26DK")
check("removed plate not searchable",
      json.loads(body) == [])
# /log still renders (movement log entries with the now-deleted house keep working)
_, body = follow("/log")
check("/log still renders after vehicle/house removal", "<table" in body)


# ----- clear older than N days -----
section("CLEAR OLDER THAN")
# Inject a synthetic old entry directly via the DB? We don't have backend access here.
# Instead exercise the route with days=1: nothing in setup is >1 day old, so it should delete 0.
status, _, _ = post("/admin/logs/clear-older", form={"days": "1"})
_, body = follow("/admin")
check("clear-older with days=1 returns to admin", status in (301, 302))
check("clear-older flash mentions 0 entries",
      "deleted 0 log" in body.lower() or "0 entries" in body.lower(),
      f"body excerpt: {re.findall(r'flash[^>]*>([^<]+)', body)}")

# Bad value
status, _, _ = post("/admin/logs/clear-older", form={"days": "abc"})
_, body = follow("/admin")
check("clear-older with bad days flashes error",
      "must be a number" in body.lower())

status, _, _ = post("/admin/logs/clear-older", form={"days": "0"})
_, body = follow("/admin")
check("clear-older with days=0 rejected",
      "at least 1" in body.lower())


# ----- export CSV -----
section("EXPORT CSV")
status, body, _ = get("/admin/logs/export.csv")
check("CSV export returns 200", status == 200)
check("CSV header row present",
      body.startswith("timestamp,vehicle,direction,type,house,floor,visitor_name,visitor_phone,note"),
      body[:120])
check("CSV contains DL3CAB1234 row", "DL3CAB1234" in body)
check("CSV contains X1Y2 row", "X1Y2" in body)


# ----- clear all logs -----
section("CLEAR ALL LOGS")
# Without confirm
status, _, _ = post("/admin/logs/clear", form={"confirm": ""})
_, body = follow("/admin")
check("clear all without 'CLEAR' rejected",
      "type clear" in body.lower())

# Confirm word
status, _, _ = post("/admin/logs/clear", form={"confirm": "CLEAR"})
_, body = follow("/admin")
check("clear all with confirm — flash mentions 'cleared N'",
      "cleared" in body.lower())
_, body = follow("/log")
check("after clear all, /log has no data rows",
      "DL3CAB1234" not in body and "X1Y2" not in body)
_, body = follow("/admin")
check("after clear all, admin shows 0 log entries", "0 log entries" in body)


# ----- logout -----
section("LOGOUT")
status, _, _ = post("/admin/logout")
status, _, loc = get("/admin")
check("after logout, /admin redirects to login",
      status in (301, 302) and "/login" in (loc or ""))


# ----- summary -----
print(f"\n=================== {PASS} passed, {FAIL} failed ===================")
for ok, name, detail in RESULTS:
    if not ok:
        print(f"  FAIL  {name}  {detail}")
sys.exit(0 if FAIL == 0 else 1)
