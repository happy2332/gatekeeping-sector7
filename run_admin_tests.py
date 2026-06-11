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


def get_house_id(number, floor=None):
    _, body = follow("/houses")
    pat = r'<strong>' + re.escape(number) + r'</strong>(?:</a>)?'
    if floor:
        pat += r'\s*</td>\s*<td[^>]*>' + re.escape(floor) + r'</td>'
    pat += r'.*?/houses/(\d+)'
    m = re.search(pat, body, re.S)
    return int(m.group(1)) if m else None


# ----- setup data -----
section("SETUP")
# Log in as admin to seed houses + vehicles, then log out before AUTH section.
post("/login", form={"password": ADMIN_PASSWORD})
post("/houses/new", form={"number": "A-101", "owner_name": "Sharma"})
post("/houses/new", form={"number": "A-102", "owner_name": "Kapoor", "floor": "1"})
post("/houses/new", form={"number": "B-201", "owner_name": "Iyer"})
a101 = get_house_id("A-101")
a102 = get_house_id("A-102", "1")
b201 = get_house_id("B-201")
post(f"/houses/{a101}", form={"action": "add_vehicle", "plate": "DL3CAB1234"})
post(f"/houses/{a102}", form={"action": "add_vehicle", "plate": "DL8CAF7788"})
post(f"/houses/{b201}", form={"action": "add_vehicle", "plate": "HR26DK9999"})
post("/api/log", json_body={"plate": "DL3CAB1234", "direction": "in", "kind": "resident", "house_id": a101})
post("/api/log", json_body={"plate": "DL3CAB1234", "direction": "out", "kind": "resident", "house_id": a101})
post("/api/log", json_body={"plate": "X1Y2", "direction": "in", "kind": "unknown"})
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
status, _, _ = get("/houses")
check("resident: GET /houses works without login", status == 200)
status, body, _ = get("/houses")
check("resident: 'Add a house' form NOT shown without admin login",
      "Add a house" not in body or "edit →" not in body)
# Resident cannot post log
status, _, _ = post("/api/log",
    json_body={"plate": "ZZZ", "direction": "in", "kind": "unknown"})
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
status, _, _ = post("/houses/new", form={"number": "Z-999"})
check("guard cannot create houses (redirected)", status in (301, 302))
# But guard CAN log via api/log
status, _, _ = post("/api/log",
    json_body={"plate": "GUARD1", "direction": "in", "kind": "unknown"})
check("guard CAN POST /api/log", status == 200)
# Re-login as admin for the rest of the suite
post("/logout")
post("/login", form={"password": ADMIN_PASSWORD})


# ----- delete house -----
section("DELETE HOUSE")
post(f"/admin/houses/{b201}/delete")
# Re-fetch admin (a second GET) so the flash about "Deleted house B-201..." is gone
follow("/admin")
_, body = follow("/admin")
m = re.search(r'<table class="data".*?</table>', body, re.S)
admin_table = m.group(0) if m else ""
check("delete house: B-201 no longer in admin's house table",
      "B-201" not in admin_table, f"table excerpt: {admin_table[:200]}")
# Same for the /houses list (also has flashes)
follow("/houses")
_, body = follow("/houses")
m = re.search(r'<table class="data".*?</table>', body, re.S)
houses_table = m.group(0) if m else ""
check("delete house: B-201 no longer in /houses table",
      "B-201" not in houses_table)
# Cascading delete: B-201's only registered vehicle (HR26DK9999) should not be searchable on /gate
status, body, _ = get("/api/vehicles/search?q=HR26DK")
check("delete house: cascading delete drops the registered vehicle",
      json.loads(body) == [])
# /log still renders (no 500 from dangling refs)
_, body = follow("/log")
check("delete house: /log still renders", "<table" in body)


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
