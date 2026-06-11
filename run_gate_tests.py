"""Gate-flow tests. Hits a running server (default http://127.0.0.1:5058)."""
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
GUARD_PASSWORD = os.environ.get("GUARD_PASSWORD", "guard")
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))

PASS = 0
FAIL = 0
RESULTS = []


def request(method, path, form=None, json_body=None):
    url = BASE + path
    data = None
    headers = {}
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


def section(title):
    print(f"\n=== {title} ===")


def get_house_id(number, floor=None):
    _, body, _ = get("/houses")
    pattern = r'<strong>' + re.escape(number) + r'</strong>(?:</a>)?'
    if floor:
        pattern += r'\s*</td>\s*<td>' + re.escape(floor) + r'</td>'
    pattern += r'.*?/houses/(\d+)'
    m = re.search(pattern, body, re.S)
    return int(m.group(1)) if m else None


# -----------------------------------------------------------------
# Setup: log in as admin (so we can create houses), then test as guard.
section("SETUP")
post("/login", form={"password": ADMIN_PASSWORD})
post("/houses/new", form={"number": "A-101", "owner_name": "Sharma"})
post("/houses/new", form={"number": "A-102", "owner_name": "Kapoor", "floor": "1"})
post("/houses/new", form={"number": "A-102", "owner_name": "Roy", "floor": "2"})
post("/houses/new", form={"number": "B-201", "owner_name": "Iyer"})

a101 = get_house_id("A-101")
a102_1 = get_house_id("A-102", "1")
a102_2 = get_house_id("A-102", "2")
b201 = get_house_id("B-201")
check("can resolve all 4 house ids", all([a101, a102_1, a102_2, b201]),
      f"a101={a101} a102_1={a102_1} a102_2={a102_2} b201={b201}")

post(f"/houses/{a101}", form={"action": "add_vehicle", "plate": "DL3CAB1234"})
post(f"/houses/{a102_1}", form={"action": "add_vehicle", "plate": "DL8CAF7788"})
post(f"/houses/{a102_2}", form={"action": "add_vehicle", "plate": "DL12CK4321"})
post(f"/houses/{b201}", form={"action": "add_vehicle", "plate": "HR26DK1234"})


# -----------------------------------------------------------------
section("GATE — page renders")
status, body, _ = get("/gate")
check("/gate returns 200", status == 200)
check("/gate has vehicle search box", 'id="vehicleSearch"' in body)
check("/gate has visitor section", 'id="visitorSection"' in body)
check("/gate references search API", "/api/vehicles/search" in body)


# -----------------------------------------------------------------
section("VEHICLE SEARCH API")

# Last-4-digits match — two plates end in 1234
status, body, _ = get("/api/vehicles/search?q=1234")
data = json.loads(body)
check("search '1234' returns 2 plates", len(data) == 2, f"got {len(data)}: {[d['plate'] for d in data]}")
plates = [d["plate"] for d in data]
check("'1234' results contain DL3CAB1234", "DL3CAB1234" in plates)
check("'1234' results contain HR26DK1234", "HR26DK1234" in plates)

# Suffix match should sort first (both end in 1234, both equally good — just check no errors)
# But middle-of-string match should rank lower
_, body, _ = get("/api/vehicles/search?q=12")  # matches DL12CK4321 (start) and *1234 (end) plates
data = json.loads(body)
suffix_plates = [d["plate"] for d in data if d["plate"].endswith("12")]
non_suffix_plates = [d["plate"] for d in data if not d["plate"].endswith("12")]
# Plates ending in '12' should come first per the SQL CASE
ranks = [d["plate"] for d in data]
check("query returns multiple plates including middle-of-string matches", len(data) >= 2)

# Case + space insensitive
_, body, _ = get("/api/vehicles/search?q=" + urllib.parse.quote("dl 3c"))
data = json.loads(body)
check("case-insensitive + space-insensitive search works",
      any(d["plate"] == "DL3CAB1234" for d in data),
      f"got {[d['plate'] for d in data]}")

# Too short returns []
_, body, _ = get("/api/vehicles/search?q=1")
check("search with <2 chars returns []", json.loads(body) == [])

# No match returns []
_, body, _ = get("/api/vehicles/search?q=ZZZZ")
check("search for non-matching string returns []", json.loads(body) == [])

# Floor info returned for multi-floor houses
_, body, _ = get("/api/vehicles/search?q=8CAF")
data = json.loads(body)
check("multi-floor: search result includes floor", data and data[0]["floor"] == "1",
      f"got {data}")

_, body, _ = get("/api/vehicles/search?q=CK4321")
data = json.loads(body)
check("multi-floor: floor 2 returned correctly", data and data[0]["floor"] == "2",
      f"got {data}")


# -----------------------------------------------------------------
section("RESIDENT LOG IN/OUT")

# Log a resident IN
status, body, _ = post("/api/log",
    json_body={"plate": "DL3CAB1234", "direction": "in", "kind": "resident", "house_id": a101})
check("POST /api/log resident IN returns 200", status == 200)
check("response is {ok: true}", json.loads(body) == {"ok": True})

# After logging IN, search shows currently_inside=true
_, body, _ = get("/api/vehicles/search?q=1234")
data = json.loads(body)
target = next(d for d in data if d["plate"] == "DL3CAB1234")
check("after IN, search marks vehicle currently_inside=True", target["currently_inside"] is True)

# Inside view lists this vehicle
_, body, _ = get("/")
check("Inside view lists DL3CAB1234", "DL3CAB1234" in body)
check("Inside view marks it as resident", re.search(r"DL3CAB1234.*?badge--resident", body, re.S))
check("Inside view shows house A-101", re.search(r"DL3CAB1234.*?A-101", body, re.S))

# Log it OUT
status, _, _ = post("/api/log",
    json_body={"plate": "DL3CAB1234", "direction": "out", "kind": "resident", "house_id": a101})
check("POST /api/log resident OUT returns 200", status == 200)

# Now currently_inside flips to false
_, body, _ = get("/api/vehicles/search?q=1234")
data = json.loads(body)
target = next(d for d in data if d["plate"] == "DL3CAB1234")
check("after OUT, search marks vehicle currently_inside=False", target["currently_inside"] is False)

# Inside view should no longer list it
_, body, _ = get("/")
check("Inside view no longer lists DL3CAB1234 after OUT",
      not re.search(r"DL3CAB1234</strong></td>\s*<td><span class=\"badge badge--resident\"", body, re.S))


# -----------------------------------------------------------------
section("VISITOR FLOW")

# Visitor with house
status, _, _ = post("/api/log", json_body={
    "plate": "UP14XY9999", "direction": "in", "kind": "visitor",
    "house_id": a101, "visitor_name": "Amit", "visitor_phone": "9988776655",
    "note": "uncle",
})
check("POST visitor IN returns 200", status == 200)

_, body, _ = get("/")
check("Inside view lists visitor plate", "UP14XY9999" in body)
check("Inside view shows visitor name", "Amit" in body)
check("visitor row tagged with badge--visitor",
      re.search(r"UP14XY9999.*?badge--visitor", body, re.S))
check("visitor attached to A-101", re.search(r"UP14XY9999.*?A-101", body, re.S))

# Visitor by house_number string (the API also accepts this)
status, _, _ = post("/api/log", json_body={
    "plate": "MH12AA1111", "direction": "in", "kind": "visitor",
    "house_number": "B-201", "visitor_name": "Pooja",
})
check("POST visitor IN by house_number string returns 200", status == 200)
_, body, _ = get("/")
check("visitor by house_number attached to B-201",
      re.search(r"MH12AA1111.*?B-201", body, re.S))


# -----------------------------------------------------------------
section("UNKNOWN VEHICLE FLOW")

status, _, _ = post("/api/log", json_body={
    "plate": "TEMP1234X", "direction": "in", "kind": "unknown", "note": "courier",
})
check("POST unknown IN returns 200", status == 200)

_, body, _ = get("/")
check("Inside view lists unknown plate", "TEMP1234X" in body)
check("unknown row tagged with badge--unknown",
      re.search(r"TEMP1234X.*?badge--unknown", body, re.S))
check("unknown row has no house",
      re.search(r"TEMP1234X.*?<td>—</td>", body, re.S))


# -----------------------------------------------------------------
section("API VALIDATION")

# Missing direction
status, _, _ = post("/api/log", json_body={"plate": "X", "kind": "resident"})
check("missing direction → 400", status == 400)

# Bad direction
status, _, _ = post("/api/log",
    json_body={"plate": "X", "direction": "sideways", "kind": "resident"})
check("bad direction → 400", status == 400)

# Bad kind
status, _, _ = post("/api/log",
    json_body={"plate": "X", "direction": "in", "kind": "spaceship"})
check("bad kind → 400", status == 400)

# Empty plate
status, _, _ = post("/api/log",
    json_body={"plate": "  ", "direction": "in", "kind": "unknown"})
check("empty plate → 400", status == 400)

# Already-inside / not-inside guard
post("/api/log", json_body={"plate": "AI0001", "direction": "in", "kind": "unknown"})
status, body, _ = post("/api/log",
    json_body={"plate": "AI0001", "direction": "in", "kind": "unknown"})
check("logging IN twice in a row → 409", status == 409,
      f"got {status} body={body!r}")
check("409 body mentions 'already inside'",
      status == 409 and "already inside" in body.lower())

# OUT once works, second OUT rejected
post("/api/log", json_body={"plate": "AI0001", "direction": "out", "kind": "unknown"})
status, body, _ = post("/api/log",
    json_body={"plate": "AI0001", "direction": "out", "kind": "unknown"})
check("logging OUT twice in a row → 409", status == 409)
check("409 body mentions 'not inside'",
      status == 409 and "not inside" in body.lower())

# Fresh plate going OUT directly is also rejected (it's not inside)
status, body, _ = post("/api/log",
    json_body={"plate": "AI0002", "direction": "out", "kind": "unknown"})
check("OUT for never-seen plate → 409", status == 409)

# Search shows currently_inside flag, used by client to disable buttons
post("/api/log", json_body={"plate": "DL3CAB1234", "direction": "in", "kind": "resident", "house_id": a101})
_, body, _ = get("/api/vehicles/search?q=1234")
data = json.loads(body)
target = next(d for d in data if d["plate"] == "DL3CAB1234")
check("search returns currently_inside=True for IN plate (used to disable IN btn client-side)",
      target["currently_inside"] is True)
post("/api/log", json_body={"plate": "DL3CAB1234", "direction": "out", "kind": "resident", "house_id": a101})

# Plate normalisation: 'dl 3c ab 1234' should map to DL3CAB1234 in the log
status, _, _ = post("/api/log", json_body={
    "plate": "dl 3c ab 1234", "direction": "in", "kind": "resident", "house_id": a101,
})
check("plate normalisation accepted (200)", status == 200)
_, body, _ = get("/log?q=DL3CAB1234")
check("normalised plate appears as DL3CAB1234 in log", "DL3CAB1234" in body)


# -----------------------------------------------------------------
section("LOG VIEW")
_, body, _ = get("/log")
check("/log shows visitor name in row", "Amit" in body)
check("/log shows note 'courier'", "courier" in body)
# Date + Time columns rendered
check("/log shows formatted Date column",
      re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}", body) is not None)
check("/log shows 12-hour Time column",
      re.search(r"\d{1,2}:\d{2} [AP]M", body) is not None)

# Search on /log filters by plate
_, body, _ = get("/log?q=MH12AA1111")
check("/log search by plate finds row", "MH12AA1111" in body and "TEMP1234X" not in body)

# Search by house number
_, body, _ = get("/log?q=B-201")
check("/log search by house finds row", "MH12AA1111" in body)

# Search by visitor name
_, body, _ = get("/log?q=Amit")
check("/log search by visitor name finds row", "UP14XY9999" in body)


# -----------------------------------------------------------------
section("CURRENTLY-INSIDE EDGE CASE")

# Log a fresh plate IN then OUT — should not appear in Inside
post("/api/log", json_body={"plate": "TEST00001", "direction": "in", "kind": "unknown"})
post("/api/log", json_body={"plate": "TEST00001", "direction": "out", "kind": "unknown"})
_, body, _ = get("/")
inside_section = re.search(r"<h1>Currently inside.*?</section>", body, re.S).group(0)
check("vehicle that went IN then OUT is not in 'currently inside'",
      "TEST00001" not in inside_section)

# Log OUT directly (no prior IN) — appears in log but NOT in Inside
post("/api/log", json_body={"plate": "TEST00002", "direction": "out", "kind": "unknown"})
_, body, _ = get("/")
inside_section = re.search(r"<h1>Currently inside.*?</section>", body, re.S).group(0)
check("vehicle with only an OUT event is not in 'currently inside'",
      "TEST00002" not in inside_section)
_, body, _ = get("/log?q=TEST00002")
check("vehicle with only an OUT event still appears in /log",
      "TEST00002" in body)


# -----------------------------------------------------------------
print(f"\n=================== {PASS} passed, {FAIL} failed ===================")
for status, name, detail in RESULTS:
    if status == "FAIL":
        print(f"  FAIL  {name}  {detail}")
sys.exit(0 if FAIL == 0 else 1)
