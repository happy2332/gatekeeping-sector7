import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import (
    Flask, Response, g, render_template, request, redirect, session,
    url_for, jsonify, flash,
)

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist_str():
    """Current wall time in IST, formatted to match SQLite's stored format."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def now_ist():
    """Naive datetime in IST wall time (no tz attached, matches stored format)."""
    return datetime.now(IST).replace(tzinfo=None)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GK_DB") or os.path.join(APP_DIR, "gatekeeping.db")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("GK_SECRET") or secrets.token_hex(16)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
GUARD_PASSWORD = os.environ.get("GUARD_PASSWORD", "guard")


def current_role():
    return session.get("role", "resident")


def role_at_least(min_role):
    rank = {"resident": 0, "guard": 1, "admin": 2}
    return rank.get(current_role(), 0) >= rank[min_role]


def guard_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not role_at_least("guard"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not role_at_least("admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_role():
    role = current_role()
    return {
        "role": role,
        "is_resident": role == "resident",
        "is_guard": role_at_least("guard"),
        "is_admin": role_at_least("admin"),
    }


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@app.template_filter("as_date")
def filter_as_date(value):
    dt = _parse_ts(value)
    return dt.strftime("%d %b %Y") if dt else (value or "")


@app.template_filter("as_time12")
def filter_as_time12(value):
    dt = _parse_ts(value)
    if not dt:
        return value or ""
    return dt.strftime("%I:%M %p").lstrip("0")


@app.context_processor
def inject_phone_helper():
    def phone_visible(house):
        # house may be a sqlite3.Row or dict-like
        try:
            masked = house["phone_masked"]
        except (KeyError, IndexError):
            masked = 0
        return not masked or role_at_least("guard")
    return {"phone_visible": phone_visible}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS houses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL,
            owner_name TEXT,
            phone TEXT,
            floor TEXT,
            phone_masked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
            plate TEXT NOT NULL,
            make_model TEXT,
            colour TEXT,
            UNIQUE(plate)
        );

        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            house_id INTEGER REFERENCES houses(id),
            vehicle_id INTEGER REFERENCES vehicles(id),
            plate TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('resident', 'visitor', 'unknown')),
            direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
            visitor_name TEXT,
            visitor_phone TEXT,
            note TEXT,
            ts TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_movements_ts ON movements(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_movements_plate ON movements(plate);
        CREATE INDEX IF NOT EXISTS idx_vehicles_house ON vehicles(house_id);
        """
    )
    cols = {row[1] for row in db.execute("PRAGMA table_info(houses)").fetchall()}
    if "floor" not in cols:
        db.execute("ALTER TABLE houses ADD COLUMN floor TEXT")
    if "phone_masked" not in cols:
        db.execute("ALTER TABLE houses ADD COLUMN phone_masked INTEGER NOT NULL DEFAULT 0")

    # Migration: drop legacy UNIQUE constraint on houses.number so the same
    # number can appear once per floor. We replace it with two partial indexes.
    legacy = db.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND tbl_name='houses' AND name LIKE 'sqlite_autoindex%'
        """
    ).fetchall()
    if legacy:
        db.executescript(
            """
            CREATE TABLE houses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL,
                owner_name TEXT,
                phone TEXT,
                floor TEXT
            );
            INSERT INTO houses_new (id, number, owner_name, phone, floor)
                SELECT id, number, owner_name, phone, floor FROM houses;
            DROP TABLE houses;
            ALTER TABLE houses_new RENAME TO houses;
            """
        )

    db.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_house_with_floor
            ON houses(number, floor) WHERE floor IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_house_no_floor
            ON houses(number) WHERE floor IS NULL;
        """
    )
    db.commit()
    db.close()


def normalise_plate(plate: str) -> str:
    return "".join(ch for ch in (plate or "").upper() if ch.isalnum())


@app.route("/")
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    kind_filter = request.args.get("kind", "").strip().lower()
    if kind_filter not in ("resident", "visitor", "unknown"):
        kind_filter = ""
    params = []
    where_extra = ""
    if q:
        like = f"%{q.upper()}%"
        where_extra = (
            "AND (UPPER(m.plate) LIKE ?"
            "  OR UPPER(h.number) LIKE ?"
            "  OR UPPER(m.visitor_name) LIKE ?"
            "  OR UPPER(h.owner_name) LIKE ?)"
        )
        params = [like, like, like, like]
    if kind_filter:
        where_extra += " AND m.kind = ?"
        params.append(kind_filter)
    inside = db.execute(
        f"""
        SELECT m.plate, m.kind, m.ts, m.house_id,
               h.number AS house_number, h.floor AS house_floor,
               h.owner_name AS house_owner,
               h.phone AS house_phone,
               h.phone_masked AS house_phone_masked,
               m.visitor_name
        FROM movements m
        LEFT JOIN houses h ON h.id = m.house_id
        WHERE m.id IN (
            SELECT MAX(id) FROM movements GROUP BY plate
        )
        AND m.direction = 'in'
        {where_extra}
        ORDER BY m.ts DESC
        """,
        params,
    ).fetchall()
    recent = db.execute(
        """
        SELECT m.*, h.number AS house_number, h.floor AS house_floor
        FROM movements m
        LEFT JOIN houses h ON h.id = m.house_id
        ORDER BY m.id DESC
        LIMIT 20
        """
    ).fetchall()
    return render_template("index.html", inside=inside, recent=recent, q=q, kind_filter=kind_filter)


@app.route("/houses")
def houses_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    floor_filter = request.args.get("floor", "").strip()
    # Special sentinel to filter for no-floor houses
    no_floor = floor_filter == "_none_"

    clauses = []
    params = []
    if q:
        like = f"%{q.upper()}%"
        clauses.append(
            "h.id IN ("
            "  SELECT h2.id FROM houses h2"
            "   LEFT JOIN vehicles v2 ON v2.house_id = h2.id"
            "   WHERE UPPER(h2.number) LIKE ?"
            "      OR UPPER(h2.owner_name) LIKE ?"
            "      OR UPPER(v2.plate) LIKE ?"
            ")"
        )
        params += [like, like, like]
    if no_floor:
        clauses.append("h.floor IS NULL")
    elif floor_filter:
        clauses.append("h.floor = ?")
        params.append(floor_filter)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = db.execute(
        f"""
        SELECT h.*, COUNT(v.id) AS vehicle_count
        FROM houses h LEFT JOIN vehicles v ON v.house_id = h.id
        {where}
        GROUP BY h.id
        ORDER BY h.number, h.floor
        """,
        params,
    ).fetchall()

    # Distinct floors for the filter pills (skip nulls)
    floors = [r["floor"] for r in db.execute(
        "SELECT DISTINCT floor FROM houses WHERE floor IS NOT NULL ORDER BY floor"
    ).fetchall()]
    has_floorless = db.execute(
        "SELECT 1 FROM houses WHERE floor IS NULL LIMIT 1"
    ).fetchone() is not None

    return render_template("houses.html", houses=rows, q=q,
                           floors=floors, has_floorless=has_floorless,
                           floor_filter=floor_filter)


@app.route("/houses/<int:house_id>", methods=["GET", "POST"])
def house_detail(house_id):
    if request.method == "POST" and not role_at_least("admin"):
        flash("Admin login required to edit houses", "error")
        return redirect(url_for("login", next=request.path))
    db = get_db()
    house = db.execute("SELECT * FROM houses WHERE id = ?", (house_id,)).fetchone()
    if not house:
        flash("House not found", "error")
        return redirect(url_for("houses_list"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_vehicle":
            plate = normalise_plate(request.form.get("plate", ""))
            make_model = request.form.get("make_model", "").strip()
            colour = request.form.get("colour", "").strip()
            if not plate:
                flash("Plate required", "error")
            else:
                try:
                    db.execute(
                        "INSERT INTO vehicles (house_id, plate, make_model, colour) VALUES (?, ?, ?, ?)",
                        (house_id, plate, make_model or None, colour or None),
                    )
                    db.commit()
                    flash(f"Added {plate}", "ok")
                except sqlite3.IntegrityError:
                    flash(f"{plate} is already registered", "error")
        elif action == "delete_vehicle":
            vid = int(request.form.get("vehicle_id"))
            db.execute("DELETE FROM vehicles WHERE id = ? AND house_id = ?", (vid, house_id))
            db.commit()
            flash("Vehicle removed", "ok")
        elif action == "update_house":
            new_floor = request.form.get("floor", "").strip() or None
            siblings = db.execute(
                "SELECT id, floor FROM houses WHERE number = ? AND id != ?",
                (house["number"], house_id),
            ).fetchall()
            sibling_has_floor = any(r["floor"] is not None for r in siblings)
            sibling_no_floor = any(r["floor"] is None for r in siblings)
            if new_floor is None and sibling_has_floor:
                flash(f"House {house['number']} has other floor entries — keep this one's floor too", "error")
            elif new_floor is not None and sibling_no_floor:
                flash(f"House {house['number']} also has a no-floor entry — can't mix", "error")
            else:
                try:
                    db.execute(
                        "UPDATE houses SET owner_name = ?, phone = ?, floor = ?, phone_masked = ? WHERE id = ?",
                        (
                            request.form.get("owner_name", "").strip() or None,
                            request.form.get("phone", "").strip() or None,
                            new_floor,
                            1 if request.form.get("phone_masked") else 0,
                            house_id,
                        ),
                    )
                    db.commit()
                    flash("House updated", "ok")
                except sqlite3.IntegrityError:
                    flash(f"House {house['number']} (floor {new_floor}) already exists", "error")
        return redirect(url_for("house_detail", house_id=house_id))

    vehicles = db.execute(
        "SELECT * FROM vehicles WHERE house_id = ? ORDER BY plate", (house_id,)
    ).fetchall()
    return render_template("house_detail.html", house=house, vehicles=vehicles)


@app.route("/houses/new", methods=["POST"])
@admin_required
def house_create():
    number = request.form.get("number", "").strip().upper()
    if not number:
        flash("House number required", "error")
        return redirect(url_for("houses_list"))
    phone = request.form.get("phone", "").strip()
    if not phone:
        flash("Phone number is required", "error")
        return redirect(url_for("houses_list"))
    floor = request.form.get("floor", "").strip() or None
    db = get_db()

    existing = db.execute("SELECT floor FROM houses WHERE number = ?", (number,)).fetchall()
    has_with_floor = any(r["floor"] is not None for r in existing)
    has_without_floor = any(r["floor"] is None for r in existing)
    if floor is None and has_with_floor:
        flash(f"House {number} already has floor entries — please specify a floor", "error")
        return redirect(url_for("houses_list"))
    if floor is not None and has_without_floor:
        flash(f"House {number} is already registered without a floor — remove that entry first or skip the floor field", "error")
        return redirect(url_for("houses_list"))

    try:
        cur = db.execute(
            "INSERT INTO houses (number, owner_name, phone, floor, phone_masked) VALUES (?, ?, ?, ?, ?)",
            (
                number,
                request.form.get("owner_name", "").strip() or None,
                phone,
                floor,
                1 if request.form.get("phone_masked") else 0,
            ),
        )
        db.commit()
        return redirect(url_for("house_detail", house_id=cur.lastrowid))
    except sqlite3.IntegrityError:
        if floor is None:
            flash(f"House {number} already exists", "error")
        else:
            flash(f"House {number} (floor {floor}) already exists", "error")
        return redirect(url_for("houses_list"))


@app.route("/gate")
@guard_required
def gate():
    db = get_db()
    houses = db.execute(
        "SELECT id, number, owner_name, floor FROM houses ORDER BY number, floor"
    ).fetchall()
    return render_template("gate.html", houses=houses)


@app.route("/api/vehicles/search")
def api_vehicle_search():
    q = normalise_plate(request.args.get("q", ""))
    if len(q) < 2:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        """
        SELECT v.id AS vehicle_id, v.plate, h.id AS house_id,
               h.number AS house_number, h.floor, h.owner_name
        FROM vehicles v JOIN houses h ON h.id = v.house_id
        WHERE UPPER(v.plate) LIKE ?
        ORDER BY (CASE WHEN UPPER(v.plate) LIKE ? THEN 0 ELSE 1 END), v.plate
        LIMIT 10
        """,
        (f"%{q}%", f"%{q}"),
    ).fetchall()
    out = []
    for r in rows:
        last = db.execute(
            "SELECT direction FROM movements WHERE plate = ? ORDER BY id DESC LIMIT 1",
            (r["plate"],),
        ).fetchone()
        out.append({
            "vehicle_id": r["vehicle_id"],
            "plate": r["plate"],
            "house_id": r["house_id"],
            "house_number": r["house_number"],
            "floor": r["floor"],
            "owner_name": r["owner_name"],
            "currently_inside": bool(last and last["direction"] == "in"),
        })
    return jsonify(out)


@app.route("/api/house/<int:house_id>")
def api_house(house_id):
    db = get_db()
    house = db.execute("SELECT * FROM houses WHERE id = ?", (house_id,)).fetchone()
    if not house:
        return jsonify({"error": "not found"}), 404
    vehicles = db.execute(
        "SELECT id, plate, make_model, colour FROM vehicles WHERE house_id = ? ORDER BY plate",
        (house_id,),
    ).fetchall()
    last_status = {}
    for v in vehicles:
        row = db.execute(
            "SELECT direction FROM movements WHERE plate = ? ORDER BY id DESC LIMIT 1",
            (v["plate"],),
        ).fetchone()
        last_status[v["plate"]] = row["direction"] if row else "out"
    return jsonify(
        {
            "id": house["id"],
            "number": house["number"],
            "floor": house["floor"],
            "owner_name": house["owner_name"],
            "phone": house["phone"],
            "vehicles": [
                {
                    "id": v["id"],
                    "plate": v["plate"],
                    "make_model": v["make_model"],
                    "colour": v["colour"],
                    "currently_inside": last_status[v["plate"]] == "in",
                }
                for v in vehicles
            ],
        }
    )


@app.post("/api/log")
@guard_required
def api_log():
    data = request.get_json(force=True)
    plate = normalise_plate(data.get("plate", ""))
    direction = data.get("direction")
    kind = data.get("kind")
    if direction not in ("in", "out") or kind not in ("resident", "visitor", "unknown") or not plate:
        return jsonify({"error": "bad request"}), 400

    db = get_db()
    last = db.execute(
        "SELECT direction FROM movements WHERE plate = ? ORDER BY id DESC LIMIT 1",
        (plate,),
    ).fetchone()
    last_dir = last["direction"] if last else "out"
    if direction == "in" and last_dir == "in":
        return jsonify({"error": f"{plate} is already inside"}), 409
    if direction == "out" and last_dir == "out":
        return jsonify({"error": f"{plate} is not inside"}), 409

    house_id = data.get("house_id")
    house_number = (data.get("house_number") or "").strip().upper()
    if not house_id and house_number:
        row = db.execute("SELECT id FROM houses WHERE number = ?", (house_number,)).fetchone()
        house_id = row["id"] if row else None

    vehicle_id = None
    if kind == "resident":
        v = db.execute("SELECT id, house_id FROM vehicles WHERE plate = ?", (plate,)).fetchone()
        if v:
            vehicle_id = v["id"]
            house_id = house_id or v["house_id"]

    db.execute(
        """
        INSERT INTO movements (house_id, vehicle_id, plate, kind, direction, visitor_name, visitor_phone, note, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            house_id,
            vehicle_id,
            plate,
            kind,
            direction,
            (data.get("visitor_name") or "").strip() or None,
            (data.get("visitor_phone") or "").strip() or None,
            (data.get("note") or "").strip() or None,
            now_ist_str(),
        ),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/log")
def log_view():
    db = get_db()
    q = request.args.get("q", "").strip()
    kind_filter = request.args.get("kind", "").strip().lower()
    if kind_filter not in ("resident", "visitor", "unknown"):
        kind_filter = ""
    status_filter = request.args.get("status", "").strip().lower()
    if status_filter not in ("in", "out"):
        status_filter = ""
    params = []
    clauses = []
    if q:
        like = f"%{q.upper()}%"
        clauses.append(
            "(UPPER(m.plate) LIKE ?"
            " OR UPPER(h.number) LIKE ?"
            " OR UPPER(m.visitor_name) LIKE ?"
            " OR UPPER(h.owner_name) LIKE ?)"
        )
        params += [like, like, like, like]
    if kind_filter:
        clauses.append("m.kind = ?")
        params.append(kind_filter)
    if status_filter:
        clauses.append("m.direction = ?")
        params.append(status_filter)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        f"""
        SELECT m.*, h.number AS house_number, h.floor AS house_floor,
               h.owner_name AS house_owner,
               h.phone AS house_phone,
               h.phone_masked AS house_phone_masked
        FROM movements m LEFT JOIN houses h ON h.id = m.house_id
        {where}
        ORDER BY m.id DESC
        LIMIT 200
        """,
        params,
    ).fetchall()
    return render_template("log.html", rows=rows, q=q,
                           kind_filter=kind_filter, status_filter=status_filter)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, ADMIN_PASSWORD):
            session["role"] = "admin"
            flash("Logged in as admin", "ok")
            return redirect(request.args.get("next") or url_for("admin"))
        if secrets.compare_digest(password, GUARD_PASSWORD):
            session["role"] = "guard"
            flash("Logged in as guard", "ok")
            return redirect(request.args.get("next") or url_for("gate"))
        flash("Incorrect password", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.pop("role", None)
    flash("Logged out", "ok")
    return redirect(url_for("index"))


# Back-compat redirects for the old admin URLs
@app.route("/admin/login")
def admin_login_redirect():
    return redirect(url_for("login", next=request.args.get("next")))


@app.post("/admin/logout")
def admin_logout_redirect():
    return logout()


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    stats = {
        "houses": db.execute("SELECT COUNT(*) c FROM houses").fetchone()["c"],
        "vehicles": db.execute("SELECT COUNT(*) c FROM vehicles").fetchone()["c"],
        "movements": db.execute("SELECT COUNT(*) c FROM movements").fetchone()["c"],
        "currently_inside": db.execute(
            """
            SELECT COUNT(*) c FROM movements m
            WHERE m.id IN (SELECT MAX(id) FROM movements GROUP BY plate)
              AND m.direction = 'in'
            """
        ).fetchone()["c"],
    }
    houses = db.execute(
        """
        SELECT h.*, COUNT(v.id) AS vehicle_count
        FROM houses h LEFT JOIN vehicles v ON v.house_id = h.id
        GROUP BY h.id ORDER BY h.number, h.floor
        """
    ).fetchall()
    return render_template("admin.html", stats=stats, houses=houses)


@app.post("/admin/houses/<int:house_id>/delete")
@admin_required
def admin_delete_house(house_id):
    db = get_db()
    h = db.execute("SELECT number, floor FROM houses WHERE id = ?", (house_id,)).fetchone()
    if not h:
        flash("House not found", "error")
        return redirect(url_for("admin"))
    db.execute("DELETE FROM houses WHERE id = ?", (house_id,))
    db.commit()
    label = h["number"] + (f" floor {h['floor']}" if h["floor"] else "")
    flash(f"Deleted house {label} and its vehicles", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/logs/clear")
@admin_required
def admin_clear_logs():
    confirm = request.form.get("confirm", "")
    if confirm != "CLEAR":
        flash("Type CLEAR to confirm clearing all logs", "error")
        return redirect(url_for("admin"))
    db = get_db()
    n = db.execute("SELECT COUNT(*) c FROM movements").fetchone()["c"]
    db.execute("DELETE FROM movements")
    db.commit()
    flash(f"Cleared {n} log entries", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/logs/clear-older")
@admin_required
def admin_clear_older():
    try:
        days = int(request.form.get("days", "30"))
    except ValueError:
        flash("Days must be a number", "error")
        return redirect(url_for("admin"))
    if days < 1:
        flash("Days must be at least 1", "error")
        return redirect(url_for("admin"))
    cutoff = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    cur = db.execute("DELETE FROM movements WHERE ts < ?", (cutoff,))
    db.commit()
    flash(f"Deleted {cur.rowcount} log entries older than {days} day(s)", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/logs/export.csv")
@admin_required
def admin_export_csv():
    db = get_db()
    rows = db.execute(
        """
        SELECT m.ts, m.plate, m.direction, m.kind,
               h.number AS house_number, h.floor,
               m.visitor_name, m.visitor_phone, m.note
        FROM movements m LEFT JOIN houses h ON h.id = m.house_id
        ORDER BY m.id DESC
        """
    ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "vehicle", "direction", "type",
                "house", "floor", "visitor_name", "visitor_phone", "note"])
    for r in rows:
        w.writerow([r["ts"], r["plate"], r["direction"], r["kind"],
                    r["house_number"] or "", r["floor"] or "",
                    r["visitor_name"] or "", r["visitor_phone"] or "", r["note"] or ""])
    stamp = now_ist().strftime("%Y%m%d-%H%M%S")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="gatekeeping-logs-{stamp}.csv"'},
    )


@app.cli.command("init-db")
def init_db_cmd():
    init_db()
    print(f"Initialised {DB_PATH}")


@app.cli.command("seed")
def seed_cmd():
    init_db()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    seed = [
        ("A-101", "Sharma",    "9876500001", [("DL3CAB1234", "Maruti Swift", "White")]),
        ("A-102", "Kapoor",    "9876500002", [("DL8CAF7788", "Honda City", "Silver")]),
        ("A-103", "Verma",     "9876500003", []),
        ("B-201", "Iyer",      "9876500004", [("DL12CK4321", "Hyundai Creta", "Black"),
                                                ("DL12CB9090", "Activa", "Grey")]),
        ("B-202", "Banerjee",  "9876500005", [("HR26DK5566", "Toyota Innova", "White")]),
        ("C-301", "Khan",      "9876500006", [("DL5CN1212", "Nissan Magnite", "Red")]),
    ]
    for number, owner, phone, vehicles in seed:
        cur = db.execute(
            "INSERT OR IGNORE INTO houses (number, owner_name, phone) VALUES (?, ?, ?)",
            (number, owner, phone),
        )
        if cur.rowcount == 0:
            hid = db.execute("SELECT id FROM houses WHERE number = ?", (number,)).fetchone()["id"]
        else:
            hid = cur.lastrowid
        for plate, mm, colour in vehicles:
            db.execute(
                "INSERT OR IGNORE INTO vehicles (house_id, plate, make_model, colour) VALUES (?, ?, ?, ?)",
                (hid, plate, mm, colour),
            )
    db.commit()
    db.close()
    print("Seeded 6 houses with sample vehicles.")


# Make sure the schema exists when the module is imported by gunicorn or Flask.
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port, debug=True)
