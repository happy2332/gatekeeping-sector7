"""House + vehicle CRUD tests against the constrained 100..999 / Ground..Fourth model.

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
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))

PASS = 0
FAIL = 0
RESULTS = []


def request(method, path, form=None):
    url = BASE + path
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=5) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), e.headers.get("Location", "")


def get(path):  return request("GET", path)
def post(path, form): return request("POST", path, form=form)


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
        RESULTS.append(("PASS", name, ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        RESULTS.append(("FAIL", name, detail))


def get_houses_html():
    _, body, _ = get("/houses")
    return body


def get_house_id(number, floor):
    _, body, _ = get("/houses")
    pat = (
        r'<strong>' + re.escape(str(number)) + r'</strong>(?:</a>)?'
        r'\s*</td>\s*<td[^>]*>' + re.escape(floor.title()) + r'</td>'
        r'.*?/houses/(\d+)'
    )
    m = re.search(pat, body, re.S)
    return int(m.group(1)) if m else None


def section(title):
    print(f"\n=== {title} ===")


# Admin login (CRUD requires admin or resident; we use admin to also exercise edits)
post("/login", {"password": ADMIN_PASSWORD})

# -------------------------------------------------------------
section("HOUSES — add (validators)")

# Valid create
post("/houses/new", {
    "number": "633", "floor": "ground",
    "owner_name": "Mukesh Gupta", "phone": "9876512345",
})
html = get_houses_html()
check("valid house 633 / Ground / Mukesh Gupta saved",
      "633" in html and "Ground" in html and "Mukesh Gupta" in html)

# Number out of range — too low
_, body, _ = post("/houses/new", {
    "number": "99", "floor": "ground", "owner_name": "X", "phone": "1",
})
check("number=99 rejected", "must be between 100 and 999" in body.lower() or "must be between" in body.lower())

# Number out of range — too high
_, body, _ = post("/houses/new", {
    "number": "1000", "floor": "ground", "owner_name": "X", "phone": "1",
})
check("number=1000 rejected", "must be between" in body.lower())

# Number non-numeric
_, body, _ = post("/houses/new", {
    "number": "abc", "floor": "ground", "owner_name": "X", "phone": "1",
})
check("non-numeric number rejected", "must be between" in body.lower())

# Bad floor
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "basement", "owner_name": "X", "phone": "1",
})
check("invalid floor rejected", "pick a floor" in body.lower())

# Empty floor (the dropdown has 'Pick a floor' as disabled+selected so empty value submitted)
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "", "owner_name": "X", "phone": "1",
})
check("empty floor rejected", "pick a floor" in body.lower())

# Owner missing
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "ground", "phone": "1",
})
check("owner missing rejected", "owner name is required" in body.lower())

# Phone missing
_, body, _ = post("/houses/new", {
    "number": "634", "floor": "ground", "owner_name": "X",
})
check("phone missing rejected", "phone number is required" in body.lower())

# Boundary cases — 100 and 999 OK
post("/houses/new", {"number":"100","floor":"first","owner_name":"Low","phone":"1000000001"})
post("/houses/new", {"number":"999","floor":"fourth","owner_name":"High","phone":"1000000999"})
html = get_houses_html()
check("boundary 100 First saved", "100" in html and "Low" in html)
check("boundary 999 Fourth saved", "999" in html and "High" in html)

# All 5 floors accepted
for fl in ["ground", "first", "second", "third", "fourth"]:
    post("/houses/new", {
        "number": "200" if fl == "ground" else str(200 + ["ground","first","second","third","fourth"].index(fl)),
        "floor": fl, "owner_name": f"O-{fl}", "phone": "9000" + fl[:4],
    })
html = get_houses_html()
for fl in ["Ground", "First", "Second", "Third", "Fourth"]:
    check(f"floor {fl} accepted", fl in html)


# -------------------------------------------------------------
section("HOUSES — multi-floor + dedup")

# Same number, different floors
post("/houses/new", {"number":"700","floor":"ground","owner_name":"700G","phone":"9000700001"})
post("/houses/new", {"number":"700","floor":"first","owner_name":"700-1","phone":"9000700002"})
post("/houses/new", {"number":"700","floor":"second","owner_name":"700-2","phone":"9000700003"})
html = get_houses_html()
check("multi-floor: 700 Ground present", "700G" in html)
check("multi-floor: 700 First present",  "700-1" in html)
check("multi-floor: 700 Second present", "700-2" in html)

# Same (number, floor) → existing-house path; needs a plate
_, body, _ = post("/houses/new", {
    "number": "700", "floor": "first", "owner_name": "Other", "phone": "9999",
})
check("re-add same (number, floor) without plate → error",
      "add at least one vehicle to attach" in body.lower())

# Same (number, floor) WITH a plate → attaches as a new vehicle
post("/houses/new", {
    "number": "700", "floor": "first", "plate": "DL3CAB1234",
})
seven001 = get_house_id(700, "first")
check("can resolve house 700 First id", seven001 is not None)


# -------------------------------------------------------------
section("VEHICLES — dedup")

# Add another vehicle to 700 First in same form
h_id = seven001
post("/houses/new", {
    "number": "700", "floor": "first",
    "plate": "HR26FM8872",
})
_, detail, _ = get(f"/houses/{h_id}")
check("HR26FM8872 attached to 700 First", "HR26FM8872" in detail)

# Try to register the same plate at a different house → conflict
_, body, _ = post("/houses/new", {
    "number": "201", "floor": "first", "owner_name": "Y", "phone": "1",
    "plate": "HR26FM8872",
})
check("duplicate vehicle conflict cites existing house",
      "already registered to 700" in body.lower())

# Case + space insensitive
_, body, _ = post("/houses/new", {
    "number": "202", "floor": "first", "owner_name": "Y", "phone": "1",
    "plate": "hr 26 fm 8872",
})
check("case/space-insensitive vehicle conflict",
      "already registered to 700" in body.lower())

# Self-duplicate within form
_, body, _ = post("/houses/new", {
    "number": "203", "floor": "first", "owner_name": "Y", "phone": "1",
    "plate": ["KA01XX0001", "ka01 xx 0001"],
})
check("self-duplicate within same form rejected",
      "duplicate vehicles in form" in body.lower())


# -------------------------------------------------------------
section("HOUSES — list view")

html = get_houses_html()
nums = re.findall(r'<strong>(\d{3})</strong>', html)
sorted_nums = sorted(set(nums), key=int)
seen_unique = []
for n in nums:
    if n not in seen_unique:
        seen_unique.append(n)
check("houses listed in numeric order",
      seen_unique == sorted(seen_unique, key=int),
      f"got {seen_unique}")


# -------------------------------------------------------------
section("EDIT existing house")

# Admin can edit (clear floor not allowed since it's mandatory). Owner can be changed.
seven002 = get_house_id(700, "second")
post(f"/houses/{seven002}", {
    "action": "update_house", "owner_name": "700-2 Updated",
    "phone": "9000700003", "floor": "second",
})
_, body, _ = get(f"/houses/{seven002}")
check("edit owner persists", "700-2 Updated" in body)


# -------------------------------------------------------------
print(f"\n=================== {PASS} passed, {FAIL} failed ===================")
for status, name, detail in RESULTS:
    if status == "FAIL":
        print(f"  FAIL  {name}  {detail}")
sys.exit(0 if FAIL == 0 else 1)
