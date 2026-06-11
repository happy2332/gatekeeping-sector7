"""House + vehicle CRUD tests. Hits a running server (default http://127.0.0.1:5058)."""
import os
import re
import sys
import http.cookiejar
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
        data = urllib.parse.urlencode(form).encode()
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


def get_house_id(number):
    _, body, _ = get("/houses")
    m = re.search(r'<strong>' + re.escape(number) + r'</strong>.*?/houses/(\d+)', body, re.S)
    return int(m.group(1)) if m else None


def vehicles_for(house_id):
    _, body, _ = get(f"/houses/{house_id}")
    rows = re.findall(r'<td[^>]*><strong>([A-Z0-9]+)</strong></td>', body)
    return rows


def section(title):
    print(f"\n=== {title} ===")


# -------------------------------------------------------------
# Admin login first — house/vehicle CRUD requires admin role.
post("/login", {"password": ADMIN_PASSWORD})

section("HOUSES — add")

# 1. Add a house with only a number
post("/houses/new", {"number": "A-101"})
check("add house with only a number", "A-101" in get_houses_html())

# 2. Full fields
post("/houses/new", {"number": "A-102", "floor": "1", "owner_name": "Sharma", "phone": "9876500001"})
html = get_houses_html()
check("add house with all fields — number visible", "A-102" in html)
check("add house with all fields — owner visible", "Sharma" in html)
check("add house with all fields — phone visible", "9876500001" in html)
check("add house with all fields — floor visible",
      bool(re.search(r"A-102.*?<td[^>]*>1</td>", html, re.S)),
      "expected floor=1 column for A-102")

_, body, _ = post("/houses/new", {"number": "A-101"})
check("duplicate house number rejected", "already exists" in body.lower())

# 3b. Same number with different floor — allowed (multi-floor houses)
_, body, _ = post("/houses/new", {"number": "F-601", "floor": "Ground", "owner_name": "Mehta"})
_, body, _ = post("/houses/new", {"number": "F-601", "floor": "1", "owner_name": "Gupta"})
_, body, _ = post("/houses/new", {"number": "F-601", "floor": "2", "owner_name": "Roy"})
html = get_houses_html()
check("multi-floor: F-601 Ground present", re.search(r"F-601.*?Ground", html, re.S) is not None)
check("multi-floor: F-601 1 present", re.search(r"F-601.*?Gupta", html, re.S) is not None)
check("multi-floor: F-601 2 present", re.search(r"F-601.*?Roy", html, re.S) is not None)

# 3c. Same number + same floor → rejected
_, body, _ = post("/houses/new", {"number": "F-601", "floor": "1"})
check("same number + same floor rejected", "already exists" in body.lower())

# 3d. Cannot mix floored and floor-less entries for the same number
_, body, _ = post("/houses/new", {"number": "F-601"})  # no floor on a number that has floors
check("can't add floor-less when floors exist",
      "please specify a floor" in body.lower() or "specify a floor" in body.lower())

_, body, _ = post("/houses/new", {"number": "G-701"})  # bare house, no floor
_, body, _ = post("/houses/new", {"number": "G-701", "floor": "1"})  # try to add a floor later
check("can't add floor when no-floor entry exists",
      "without a floor" in body.lower() or "can't mix" in body.lower())

# 4. Blank number
_, body, _ = post("/houses/new", {"number": ""})
check("blank house number rejected", "house number required" in body.lower())

# 5. Lowercase auto-uppercased
post("/houses/new", {"number": "b-201"})
check("lowercase number auto-uppercased to B-201", "B-201" in get_houses_html() and "b-201" not in get_houses_html())

# 6. Spaces around number
post("/houses/new", {"number": "  C-301  "})
html = get_houses_html()
check("spaces trimmed around house number",
      "C-301" in html and "  C-301  " not in html)

# 7. Phone saved as-is (no validation)
post("/houses/new", {"number": "D-401", "phone": "98765 12345"})
check("phone with spaces saved as-is", "98765 12345" in get_houses_html())


# -------------------------------------------------------------
section("HOUSES — edit")

a101 = get_house_id("A-101")
check("can resolve A-101 id", a101 is not None)

# 8. Change owner — persists
post(f"/houses/{a101}", {"action": "update_house", "owner_name": "Verma", "phone": "", "floor": ""})
_, body, _ = get(f"/houses/{a101}")
check("edit owner persists after reload", 'value="Verma"' in body)

# 9. Clear floor — list shows em-dash
a102 = get_house_id("A-102")
post(f"/houses/{a102}", {"action": "update_house", "owner_name": "Sharma", "phone": "9876500001", "floor": ""})
html = get_houses_html()
m = re.search(r"A-102.*?</tr>", html, re.S)
check("clearing floor shows em-dash in list", m and "—" in m.group(0))

# 10. House page with no vehicles renders fine
_, body, _ = get(f"/houses/{a101}")
check("empty house page renders 'No vehicles yet.'", "No vehicles yet." in body)


# -------------------------------------------------------------
section("HOUSES — list view")

# 11. Sort order — first occurrence of each number, in order
html = get_houses_html()
all_numbers = re.findall(r'<strong>([A-Z]+-\d+)</strong>', html)
seen = []
for n in all_numbers:
    if n not in seen:
        seen.append(n)
expected_prefix = ["A-101", "A-102", "B-201", "C-301", "D-401"]
check("houses sorted alphabetically by number",
      seen[:len(expected_prefix)] == expected_prefix,
      f"got order={seen}")


# -------------------------------------------------------------
section("VEHICLES — add")

# 12. Add to A-101
post(f"/houses/{a101}", {"action": "add_vehicle", "plate": "DL3CAB1234"})
check("add vehicle DL3CAB1234 to A-101", "DL3CAB1234" in vehicles_for(a101))

# 13. Same plate in different shape rejected (dedup across houses)
_, body, _ = post(f"/houses/{a102}", {"action": "add_vehicle", "plate": "dl 3c ab 1234"})
check("normalised duplicate rejected across houses",
      "already registered" in body.lower() and "DL3CAB1234" not in vehicles_for(a102))

# 14. Different plate in A-102
post(f"/houses/{a102}", {"action": "add_vehicle", "plate": "DL8CAF7788"})
check("add fresh vehicle to A-102", "DL8CAF7788" in vehicles_for(a102))

# 15. Same plate twice in same house
_, body, _ = post(f"/houses/{a101}", {"action": "add_vehicle", "plate": "DL3CAB1234"})
check("same plate twice in same house rejected",
      "already registered" in body.lower() and vehicles_for(a101).count("DL3CAB1234") == 1)

# 16. Blank plate
_, body, _ = post(f"/houses/{a101}", {"action": "add_vehicle", "plate": ""})
check("blank plate rejected", "plate required" in body.lower())


# -------------------------------------------------------------
section("VEHICLES — remove")

# 17. Find vehicle id, then remove
_, body, _ = get(f"/houses/{a102}")
m = re.search(r'value="delete_vehicle">\s*<input[^>]*name="vehicle_id" value="(\d+)"', body)
vid = int(m.group(1)) if m else None
check("can find vehicle id to delete", vid is not None)
if vid:
    post(f"/houses/{a102}", {"action": "delete_vehicle", "vehicle_id": str(vid)})
    check("vehicle removed from A-102", "DL8CAF7788" not in vehicles_for(a102))


# -------------------------------------------------------------
section("VEHICLES — counts on /houses list")

# 18. Vehicle count column reflects state
post(f"/houses/{a102}", {"action": "add_vehicle", "plate": "DL8CAF7788"})  # re-add
post(f"/houses/{a102}", {"action": "add_vehicle", "plate": "DL12CK4321"})  # second
b201 = get_house_id("B-201")
post(f"/houses/{b201}", {"action": "add_vehicle", "plate": "HR26DK5566"})
html = get_houses_html()


def count_for(num):
    m = re.search(re.escape(num) + r'.*?<td[^>]*>(\d+)</td>\s*<td[^>]*><a', html, re.S)
    return int(m.group(1)) if m else None


# Vehicle on a specific floor of F-601 — counts isolated per (number,floor)
f601_g_id = None
_, body, _ = get("/houses")
m = re.search(r'<strong>F-601</strong>(?:</a>)?\s*</td>\s*<td[^>]*>Ground</td>.*?/houses/(\d+)', body, re.S)
if m: f601_g_id = int(m.group(1))
if f601_g_id:
    post(f"/houses/{f601_g_id}", {"action": "add_vehicle", "plate": "DL5CN1212"})
    check("multi-floor: vehicle attaches to specific floor",
          "DL5CN1212" in vehicles_for(f601_g_id))

check("A-101 count column = 1", count_for("A-101") == 1, f"got {count_for('A-101')}")
check("A-102 count column = 2", count_for("A-102") == 2, f"got {count_for('A-102')}")
check("B-201 count column = 1", count_for("B-201") == 1, f"got {count_for('B-201')}")
check("D-401 count column = 0", count_for("D-401") == 0, f"got {count_for('D-401')}")


# -------------------------------------------------------------
section("EDGE CASES")

# 19. Unicode in owner name
post("/houses/new", {"number": "E-501", "owner_name": "Sharma जी"})
check("unicode owner name saved", "Sharma जी" in get_houses_html())

# 20. Long house number
post("/houses/new", {"number": "TOWER-9-FLAT-9999"})
check("long house number saved", "TOWER-9-FLAT-9999" in get_houses_html())


# -------------------------------------------------------------
print(f"\n=================== {PASS} passed, {FAIL} failed ===================")
for status, name, detail in RESULTS:
    if status == "FAIL":
        print(f"  FAIL  {name}  {detail}")
sys.exit(0 if FAIL == 0 else 1)
