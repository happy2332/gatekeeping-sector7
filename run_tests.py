"""Houses + vehicles tests for the constrained 100..999 / Ground..Fourth model.
Vehicle-centric: only /vehicles list view, /houses/new for create, no house detail page.

Hits a running server (default http://127.0.0.1:5058)."""
import http.cookiejar
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("GK_BASE", "http://127.0.0.1:5058")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
_jar = http.cookiejar.CookieJar()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None


# Two openers sharing one cookie jar:
# - _opener follows redirects (so POST /houses/new responses come back as the rendered /vehicles page)
# - _bare doesn't follow (so admin POST /admin/vehicles/<id>/delete doesn't try to re-POST to /vehicles)
_cp = urllib.request.HTTPCookieProcessor(_jar)
_opener = urllib.request.build_opener(_cp)
_bare = urllib.request.build_opener(_cp, _NoRedirect())

PASS = 0
FAIL = 0
RESULTS = []


def request(method, path, form=None, follow=True):
    url = BASE + path
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = _opener if follow else _bare
    try:
        with opener.open(req, timeout=5) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), e.headers.get("Location", "")


def get(path):  return request("GET", path)
def post(path, form): return request("POST", path, form=form)
def post_raw(path, form=None): return request("POST", path, form=form, follow=False)


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
    RESULTS.append((condition, name, detail))


def get_vehicles_html():
    _, body, _ = get("/vehicles")
    return body


def vehicle_id_for(plate):
    _, body, _ = get("/vehicles")
    m = re.search(
        re.escape(plate) + r'.*?/admin/vehicles/(\d+)/delete', body, re.S)
    return int(m.group(1)) if m else None


def section(title):
    print(f"\n=== {title} ===")


# Admin login (we want to be able to delete via /admin/vehicles/.../delete)
post("/login", {"password": ADMIN_PASSWORD})


# -------------------------------------------------------------
section("HOUSES — add (validators)")

# Valid create with one vehicle
post("/houses/new", {
    "number": "633", "floor": "ground",
    "owner_name": "Mukesh Gupta", "phone": "9876512345",
    "plate": "HR26FM8872",
})
html = get_vehicles_html()
check("valid 633/Ground/Mukesh Gupta + plate visible",
      "633" in html and "Ground" in html and "Mukesh Gupta" in html and "HR26FM8872" in html)

# Number out of range — too low
_, body, _ = post("/houses/new", {
    "number": "99", "floor": "ground", "owner_name": "X", "phone": "1",
})
check("number=99 rejected", "must be between" in body.lower())

# Number too high
_, body, _ = post("/houses/new", {
    "number": "1000", "floor": "ground", "owner_name": "X", "phone": "1",
})
check("number=1000 rejected", "must be between" in body.lower())

# Non-numeric
_, body, _ = post("/houses/new", {
    "number": "abc", "floor": "ground", "owner_name": "X", "phone": "1",
})
check("non-numeric rejected", "must be between" in body.lower())

# Bad floor
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "basement", "owner_name": "X", "phone": "1",
})
check("invalid floor rejected", "pick a floor" in body.lower())

# Empty floor
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "", "owner_name": "X", "phone": "1",
})
check("empty floor rejected", "pick a floor" in body.lower())

# Owner missing on new-house path
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "ground", "phone": "1",
})
check("owner missing on new house rejected", "owner name is required" in body.lower())

# Phone missing on new-house path
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "ground", "owner_name": "X",
})
check("phone missing on new house rejected", "phone number is required" in body.lower())

# Boundary cases — 100 and 999 OK
post("/houses/new", {"number": "100", "floor": "first",  "owner_name": "Low",  "phone": "1000000001", "plate": "B100AAA"})
post("/houses/new", {"number": "999", "floor": "fourth", "owner_name": "High", "phone": "1000000999", "plate": "B999AAA"})
html = get_vehicles_html()
check("boundary 100 First saved", "100" in html and "Low" in html)
check("boundary 999 Fourth saved", "999" in html and "High" in html)

# All 5 floor labels show on /vehicles after we use them
for i, fl in enumerate(["ground", "first", "second", "third", "fourth"]):
    n = 200 + i
    post("/houses/new", {
        "number": str(n), "floor": fl,
        "owner_name": f"O{n}", "phone": f"9000{n:04d}",
        "plate": f"FL{n}",
    })
html = get_vehicles_html()
for fl in ["Ground", "First", "Second", "Third", "Fourth"]:
    check(f"floor label '{fl}' rendered", fl in html)


# -------------------------------------------------------------
section("HOUSES — multi-floor + dedup")

post("/houses/new", {"number": "700", "floor": "ground", "owner_name": "G700",  "phone": "9000700001", "plate": "F700G"})
post("/houses/new", {"number": "700", "floor": "first",  "owner_name": "G700-1","phone": "9000700002", "plate": "F700A"})
post("/houses/new", {"number": "700", "floor": "second", "owner_name": "G700-2","phone": "9000700003", "plate": "F700B"})
html = get_vehicles_html()
check("multi-floor: 700 Ground present",  "G700" in html)
check("multi-floor: 700 First present",   "G700-1" in html)
check("multi-floor: 700 Second present",  "G700-2" in html)

# Same (number, floor) → existing-house path; needs a plate
_, body, _ = post("/houses/new", {
    "number": "700", "floor": "first", "owner_name": "Other", "phone": "9999",
})
check("re-add same (number, floor) without plate → error",
      "add at least one vehicle to attach" in body.lower())

# Same (number, floor) WITH a plate → attaches as a new vehicle (owner/phone ignored)
post("/houses/new", {
    "number": "700", "floor": "first", "plate": "F700A2",
})
html = get_vehicles_html()
check("attach plate F700A2 to existing 700 First", "F700A2" in html)


# -------------------------------------------------------------
section("VEHICLES — dedup")

# Cross-house duplicate (HR26FM8872 already registered to 633)
_, body, _ = post("/houses/new", {
    "number": "201", "floor": "first", "owner_name": "Y", "phone": "1",
    "plate": "HR26FM8872",
})
check("duplicate plate cross-house rejected with citation",
      "already registered to 633" in body.lower())

# Case + space insensitive
_, body, _ = post("/houses/new", {
    "number": "202", "floor": "first", "owner_name": "Y", "phone": "1",
    "plate": "hr 26 fm 8872",
})
check("case/space-insensitive duplicate detection",
      "already registered to 633" in body.lower())

# Self-duplicate within a single submitted form
_, body, _ = post("/houses/new", {
    "number": "203", "floor": "first", "owner_name": "Y", "phone": "1",
    "plate": ["KA01XX0001", "ka01 xx 0001"],
})
check("self-duplicate within same form rejected",
      "duplicate vehicles in form" in body.lower())


# -------------------------------------------------------------
section("VEHICLES — admin remove + auto-delete-house")

# 633 has one vehicle (HR26FM8872). Removing it should also delete the house.
hr_vid = vehicle_id_for("HR26FM8872")
check("can resolve HR26FM8872 vehicle id", hr_vid is not None)
post_raw(f"/admin/vehicles/{hr_vid}/delete")
get_vehicles_html()  # consume the flash so the next page is clean
html = get_vehicles_html()
table_html = re.search(r"<table.*?</table>", html, re.S)
table_html = table_html.group(0) if table_html else ""
check("vehicle HR26FM8872 removed from table", "HR26FM8872" not in table_html)
check("house 633 also gone (was its last vehicle)",
      "Mukesh Gupta" not in table_html)

# 700 First has 2 vehicles (F700A and F700A2). Remove one — house must remain.
v1 = vehicle_id_for("F700A2")
post_raw(f"/admin/vehicles/{v1}/delete")
get_vehicles_html()  # consume flash
html = get_vehicles_html()
table_html = re.search(r"<table.*?</table>", html, re.S)
table_html = table_html.group(0) if table_html else ""
check("F700A2 removed from table", "F700A2" not in table_html)
check("700 First (G700-1) still present", "G700-1" in table_html)
check("F700A still present (other vehicle of same house)", "F700A" in table_html)


# -------------------------------------------------------------
section("VEHICLES — list view")

# /vehicles is sorted by plate (alphabetical)
html = get_vehicles_html()
plates = re.findall(r'data-label="Vehicle"><strong>([A-Z0-9]+)</strong>', html)
check("vehicles listed in plate-alphabetical order",
      plates == sorted(plates),
      f"got order={plates[:8]}...")


# -------------------------------------------------------------
section("Search on /vehicles")

# Search by vehicle plate
_, body, _ = get("/vehicles?q=F700")
check("search 'F700' returns matching vehicles",
      "F700A" in body and "F700G" in body and "B100AAA" not in body)

# Search by owner
_, body, _ = get("/vehicles?q=G700-2")
check("search by owner 'G700-2' returns its vehicles",
      "F700B" in body and "F700A" not in body)

# Search by house number
_, body, _ = get("/vehicles?q=100")
check("search by house number '100' includes B100AAA",
      "B100AAA" in body)

# Case-insensitive
_, body, _ = get("/vehicles?q=f700")
check("lowercase search works", "F700A" in body)


# -------------------------------------------------------------
print(f"\n=================== {PASS} passed, {FAIL} failed ===================")
for status, name, detail in RESULTS:
    if not status:
        print(f"  FAIL  {name}  {detail}")
sys.exit(0 if FAIL == 0 else 1)
