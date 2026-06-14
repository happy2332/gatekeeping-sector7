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

# Feature flag: when False, the per-house "Hide phone from residents" toggle
# is hidden from forms and ignored on save, and every role sees every phone
# number. Flip to True (or set env var GK_PHONE_MASKING=1) to re-enable. The
# schema column phone_masked is preserved either way so the data isn't lost.
PHONE_MASKING_ENABLED = os.environ.get("GK_PHONE_MASKING", "").lower() in ("1", "true", "yes")

# House numbers are integers 100..999. Floors are one of a fixed list.
HOUSE_NUMBER_MIN = 100
HOUSE_NUMBER_MAX = 999
ALL_FLOORS = ("ground", "first", "second", "third", "fourth")
FLOOR_LABELS = {"ground": "Ground", "first": "First", "second": "Second",
                "third": "Third", "fourth": "Fourth"}


def validate_house_number(raw):
    s = (raw or "").strip()
    if not s.isdigit():
        return None
    n = int(s)
    return str(n) if HOUSE_NUMBER_MIN <= n <= HOUSE_NUMBER_MAX else None


def validate_floor(raw):
    s = (raw or "").strip().lower()
    return s if s in ALL_FLOORS else None


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


def role_home_url():
    """Where to send a user after a successful POST. Admin sees the vehicles
    list; guards land on the gate; residents go back to the search page."""
    if role_at_least("admin"):
        return url_for("vehicles_list")
    if role_at_least("guard"):
        return url_for("gate")
    return url_for("index")


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
def inject_house_constants():
    return {
        "house_number_min": HOUSE_NUMBER_MIN,
        "house_number_max": HOUSE_NUMBER_MAX,
        "all_floors": ALL_FLOORS,
        "floor_labels": FLOOR_LABELS,
    }


@app.context_processor
def inject_phone_helper():
    def phone_visible(house):
        if not PHONE_MASKING_ENABLED:
            return True
        try:
            masked = house["phone_masked"]
        except (KeyError, IndexError):
            masked = 0
        return not masked or role_at_least("guard")
    return {
        "phone_visible": phone_visible,
        "phone_masking_enabled": PHONE_MASKING_ENABLED,
    }


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
            number TEXT NOT NULL CHECK(CAST(number AS INTEGER) BETWEEN 100 AND 999),
            owner_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            floor TEXT NOT NULL CHECK(LOWER(floor) IN ('ground','first','second','third','fourth')),
            phone_masked INTEGER NOT NULL DEFAULT 0,
            UNIQUE(number, floor)
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
            visitor_house TEXT,
            ts TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_movements_ts ON movements(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_movements_plate ON movements(plate);
        CREATE INDEX IF NOT EXISTS idx_vehicles_house ON vehicles(house_id);
        """
    )
    # Backfill the visitor_house column on older databases.
    cols = {row[1] for row in db.execute("PRAGMA table_info(movements)").fetchall()}
    if "visitor_house" not in cols:
        db.execute("ALTER TABLE movements ADD COLUMN visitor_house TEXT")
    db.commit()
    db.close()


def normalise_plate(plate: str) -> str:
    return "".join(ch for ch in (plate or "").upper() if ch.isalnum())


@app.route("/")
def index():
    """Public landing — exact match shows a card; otherwise show partial matches."""
    db = get_db()
    q_raw = request.args.get("q", "").strip()
    q_norm = normalise_plate(q_raw)
    found = None
    partial_matches = []
    not_found = False

    if q_norm:
        # 1) Exact match in registered (resident) vehicles.
        found = db.execute(
            """
            SELECT v.plate AS plate,
                   h.number AS house_number, h.floor AS house_floor,
                   h.owner_name, h.phone, h.phone_masked,
                   'resident' AS kind,
                   (
                     SELECT direction FROM movements
                     WHERE UPPER(plate) = UPPER(v.plate)
                     ORDER BY id DESC LIMIT 1
                   ) AS last_direction
            FROM vehicles v JOIN houses h ON h.id = v.house_id
            WHERE UPPER(v.plate) = ?
            """,
            (q_norm,),
        ).fetchone()

        # 2) Exact match in visitor movements (plates not in the vehicles table).
        # If house_id is set, owner+phone come from the registered houses row.
        # If house_id is NULL, the guard typed visitor_name/visitor_phone/
        # visitor_house onto the movement itself.
        if not found:
            found = db.execute(
                """
                SELECT m.plate AS plate,
                       h.number AS house_number,
                       h.floor AS house_floor,
                       COALESCE(h.owner_name, m.visitor_name) AS owner_name,
                       COALESCE(h.phone, m.visitor_phone) AS phone,
                       h.phone_masked AS phone_masked,
                       m.visitor_house AS unregistered_house,
                       'visitor' AS kind,
                       m2.direction AS last_direction
                FROM movements m
                LEFT JOIN houses h ON h.id = m.house_id
                JOIN movements m2 ON m2.id = (
                    SELECT MAX(id) FROM movements
                    WHERE UPPER(plate) = ?
                )
                WHERE UPPER(m.plate) = ?
                ORDER BY m.id DESC
                LIMIT 1
                """,
                (q_norm, q_norm),
            ).fetchone()

        # 3) No exact hit — show partial matches (vehicles + visitor plates).
        if not found:
            like = f"%{q_norm}%"
            # Registered vehicles
            res_rows = db.execute(
                """
                SELECT v.plate AS plate,
                       h.number AS house_number, h.floor AS house_floor,
                       h.owner_name,
                       'resident' AS kind,
                       (
                         SELECT direction FROM movements
                         WHERE UPPER(plate) = UPPER(v.plate)
                         ORDER BY id DESC LIMIT 1
                       ) AS last_direction
                FROM vehicles v JOIN houses h ON h.id = v.house_id
                WHERE UPPER(v.plate) LIKE ?
                ORDER BY v.plate
                LIMIT 25
                """,
                (like,),
            ).fetchall()
            seen = {r["plate"] for r in res_rows}
            # Visitor plates from movement log (latest row per plate, not already in vehicles).
            vis_rows = db.execute(
                """
                SELECT m.plate AS plate,
                       h.number AS house_number, h.floor AS house_floor,
                       COALESCE(h.owner_name, m.visitor_name) AS owner_name,
                       m.visitor_house AS unregistered_house,
                       'visitor' AS kind,
                       m.direction AS last_direction
                FROM movements m
                LEFT JOIN houses h ON h.id = m.house_id
                WHERE m.id IN (SELECT MAX(id) FROM movements GROUP BY plate)
                  AND UPPER(m.plate) LIKE ?
                ORDER BY m.plate
                LIMIT 25
                """,
                (like,),
            ).fetchall()
            partial_matches = list(res_rows)
            for r in vis_rows:
                if r["plate"] not in seen:
                    partial_matches.append(r)
                    seen.add(r["plate"])
            partial_matches.sort(key=lambda r: r["plate"])
            not_found = not partial_matches

    return render_template(
        "search_landing.html",
        q=q_raw, found=found, partial_matches=partial_matches, not_found=not_found,
    )


@app.route("/inside")
@admin_required
def inside_view():
    db = get_db()
    q = request.args.get("q", "").strip()
    kind_filter = request.args.get("kind", "").strip().lower()
    if kind_filter not in ("resident", "visitor"):
        kind_filter = ""
    params = []
    where_extra = ""
    if q:
        like = f"%{q.upper()}%"
        where_extra = (
            "AND (UPPER(m.plate) LIKE ?"
            "  OR UPPER(h.number) LIKE ?"
            "  OR UPPER(h.owner_name) LIKE ?"
            "  OR UPPER(m.visitor_name) LIKE ?"
            "  OR UPPER(m.visitor_house) LIKE ?)"
        )
        params = [like, like, like, like, like]
    if kind_filter:
        where_extra += " AND m.kind = ?"
        params.append(kind_filter)
    inside = db.execute(
        f"""
        SELECT m.plate, m.kind, m.ts, m.house_id,
               h.number AS house_number, h.floor AS house_floor,
               COALESCE(h.owner_name, m.visitor_name) AS house_owner,
               COALESCE(h.phone, m.visitor_phone) AS house_phone,
               h.phone_masked AS house_phone_masked,
               m.visitor_name,
               m.visitor_house AS unregistered_house
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
        SELECT m.*, h.number AS house_number, h.floor AS house_floor,
               m.visitor_house AS unregistered_house
        FROM movements m
        LEFT JOIN houses h ON h.id = m.house_id
        ORDER BY m.id DESC
        LIMIT 20
        """
    ).fetchall()
    inside_total = db.execute(
        """
        SELECT COUNT(*) AS c FROM movements
        WHERE id IN (SELECT MAX(id) FROM movements GROUP BY plate)
          AND direction = 'in'
        """
    ).fetchone()["c"]
    vehicles_total = db.execute("SELECT COUNT(*) AS c FROM vehicles").fetchone()["c"]
    return render_template(
        "inside.html",
        inside=inside, recent=recent, q=q, kind_filter=kind_filter,
        inside_total=inside_total,
        vehicles_total=vehicles_total,
    )


@app.route("/vehicles")
@admin_required
def vehicles_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    vehicles_total = db.execute("SELECT COUNT(*) AS c FROM vehicles").fetchone()["c"]
    houses_total = db.execute("SELECT COUNT(*) AS c FROM houses").fetchone()["c"]

    base_select = """
        SELECT v.id AS vehicle_id, v.plate,
               h.id AS house_id, h.number AS house_number, h.floor AS house_floor,
               h.owner_name, h.phone, h.phone_masked
        FROM vehicles v JOIN houses h ON h.id = v.house_id
    """
    if q:
        like = f"%{q.upper()}%"
        rows = db.execute(
            base_select +
            """
            WHERE UPPER(v.plate) LIKE ?
               OR UPPER(h.number) LIKE ?
               OR UPPER(h.owner_name) LIKE ?
            ORDER BY v.plate
            """,
            (like, like, like),
        ).fetchall()
    else:
        rows = db.execute(base_select + " ORDER BY v.plate").fetchall()

    return render_template("vehicles.html", vehicles=rows, q=q,
                           vehicles_total=vehicles_total, houses_total=houses_total)


@app.route("/vehicles/new")
def vehicle_new():
    return render_template("vehicle_new.html")


@app.route("/houses/new", methods=["POST"])
def house_create():
    number = validate_house_number(request.form.get("number", ""))
    if number is None:
        flash(f"House number must be between {HOUSE_NUMBER_MIN} and {HOUSE_NUMBER_MAX}", "error")
        return redirect(role_home_url())
    floor = validate_floor(request.form.get("floor", ""))
    if floor is None:
        flash("Please pick a floor", "error")
        return redirect(role_home_url())
    # Owner / phone are validated *only* on the new-house path. The existing-house
    # path doesn't need them — owner+phone are taken from the existing record.
    owner_name = request.form.get("owner_name", "").strip()
    phone = request.form.get("phone", "").strip()
    db = get_db()

    # Collect and normalise vehicle entries (form sends them as plate[] inputs).
    raw_plates = request.form.getlist("plate")
    plates = []  # list of (raw, normalised)
    for raw in raw_plates:
        norm = normalise_plate(raw)
        if norm:
            plates.append((raw.strip(), norm))

    # Detect duplicates within the same submitted form.
    seen = {}
    self_dups = []
    for raw, norm in plates:
        if norm in seen and seen[norm] != raw:
            self_dups.append(f"{raw} duplicates {seen[norm]} in this form")
        else:
            seen[norm] = raw
    if self_dups:
        flash("Duplicate vehicles in form: " + "; ".join(self_dups), "error")
        return redirect(role_home_url())

    # Detect plates that already belong to other houses.
    conflicts = []
    if plates:
        norms = [n for _, n in plates]
        placeholders = ",".join("?" * len(norms))
        rows = db.execute(
            f"""
            SELECT v.plate AS plate, h.number AS h_num, h.floor AS h_floor,
                   h.owner_name AS h_owner
            FROM vehicles v JOIN houses h ON h.id = v.house_id
            WHERE UPPER(v.plate) IN ({placeholders})
            """,
            norms,
        ).fetchall()
        for r in rows:
            f_label = FLOOR_LABELS.get(r["h_floor"], r["h_floor"]) if r["h_floor"] else ""
            host = f"{r['h_num']} ({f_label})" if f_label else r["h_num"]
            owner = r["h_owner"] or "(no owner)"
            conflicts.append(f"{r['plate']} is already registered to {host} — {owner}")
    if conflicts:
        flash("Vehicle conflict: " + "; ".join(conflicts), "error")
        return redirect(role_home_url())

    # Look up whether this (number, floor) already exists.
    same_unit = db.execute(
        "SELECT id, floor, owner_name FROM houses WHERE number = ? AND floor = ?",
        (number, floor),
    ).fetchone()

    floor_label = FLOOR_LABELS.get(floor, floor)
    label = f"{number} ({floor_label})"

    # Path A: house exists. Just attach the vehicles, ignore owner/phone fields.
    if same_unit:
        if not plates:
            owner = same_unit["owner_name"]
            flash(f"House {label} is already registered to {owner}. Add at least one vehicle to attach.", "error")
            return redirect(role_home_url())
        try:
            for _, norm in plates:
                db.execute(
                    "INSERT INTO vehicles (house_id, plate) VALUES (?, ?)",
                    (same_unit["id"], norm),
                )
            db.commit()
        except sqlite3.IntegrityError as e:
            db.rollback()
            flash(f"Could not attach vehicles: {e}", "error")
            return redirect(role_home_url())
        flash(f"Added {len(plates)} vehicle{'s' if len(plates) != 1 else ''} to house {label}", "ok")
        return redirect(role_home_url())

    # Path B: brand-new house. Owner + phone are required here.
    if not owner_name:
        flash("Owner name is required", "error")
        return redirect(role_home_url())
    if not phone:
        flash("Phone number is required", "error")
        return redirect(role_home_url())

    try:
        cur = db.execute(
            "INSERT INTO houses (number, owner_name, phone, floor, phone_masked) VALUES (?, ?, ?, ?, ?)",
            (
                number,
                owner_name,
                phone,
                floor,
                1 if (PHONE_MASKING_ENABLED and request.form.get("phone_masked")) else 0,
            ),
        )
        new_house_id = cur.lastrowid
        for _, norm in plates:
            db.execute(
                "INSERT INTO vehicles (house_id, plate) VALUES (?, ?)",
                (new_house_id, norm),
            )
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        flash(f"Could not save house: {e}", "error")
        return redirect(role_home_url())

    extra = f" with {len(plates)} vehicle{'s' if len(plates) != 1 else ''}" if plates else ""
    flash(f"Registered house {label}{extra}", "ok")
    return redirect(role_home_url())


@app.route("/gate")
@guard_required
def gate():
    db = get_db()
    houses = db.execute(
        "SELECT id, number, owner_name, floor FROM houses ORDER BY number, floor"
    ).fetchall()
    return render_template("gate.html", houses=houses)


@app.get("/api/houses/lookup")
def api_house_lookup():
    """Used by the resident registration form: as they type the house number
    (and optionally floor), tell them whether it already exists so the form can
    auto-fill owner+phone and turn the submit into 'add vehicle to this house'.
    """
    number = request.args.get("number", "").strip().upper()
    floor = request.args.get("floor", "").strip()
    if not number:
        return jsonify({"matches": []})
    db = get_db()
    if floor:
        rows = db.execute(
            "SELECT id, number, floor, owner_name, phone FROM houses "
            "WHERE number = ? AND floor = ?",
            (number, floor),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, number, floor, owner_name, phone FROM houses "
            "WHERE number = ? ORDER BY floor",
            (number,),
        ).fetchall()
    return jsonify({"matches": [
        {
            "id": r["id"],
            "number": r["number"],
            "floor": r["floor"],
            "owner_name": r["owner_name"],
            "phone": r["phone"],
        } for r in rows
    ]})


@app.route("/api/vehicles/search")
def api_vehicle_search():
    q = normalise_plate(request.args.get("q", ""))
    if len(q) < 2:
        return jsonify([])
    db = get_db()

    # Registered (resident) vehicles — primary source.
    registered = db.execute(
        """
        SELECT v.id AS vehicle_id, v.plate, h.id AS house_id,
               h.number AS house_number, h.floor, h.owner_name,
               'resident' AS kind
        FROM vehicles v JOIN houses h ON h.id = v.house_id
        WHERE UPPER(v.plate) LIKE ?
        ORDER BY (CASE WHEN UPPER(v.plate) LIKE ? THEN 0 ELSE 1 END), v.plate
        LIMIT 10
        """,
        (f"%{q}%", f"%{q}"),
    ).fetchall()
    seen_plates = {r["plate"] for r in registered}

    # Visitor plates seen in recent movements (not in vehicles table).
    visitor_rows = db.execute(
        """
        SELECT m.plate,
               h.id AS house_id, h.number AS house_number, h.floor, h.owner_name,
               m.kind
        FROM movements m
        LEFT JOIN houses h ON h.id = m.house_id
        WHERE m.kind = 'visitor'
          AND UPPER(m.plate) LIKE ?
          AND m.id IN (SELECT MAX(id) FROM movements GROUP BY plate)
        ORDER BY (CASE WHEN UPPER(m.plate) LIKE ? THEN 0 ELSE 1 END), m.id DESC
        LIMIT 10
        """,
        (f"%{q}%", f"%{q}"),
    ).fetchall()

    rows = list(registered)
    for r in visitor_rows:
        if r["plate"] in seen_plates:
            continue
        seen_plates.add(r["plate"])
        rows.append(r)
        if len(rows) >= 10:
            break

    out = []
    for r in rows:
        last = db.execute(
            "SELECT direction FROM movements WHERE plate = ? ORDER BY id DESC LIMIT 1",
            (r["plate"],),
        ).fetchone()
        out.append({
            "vehicle_id": r["vehicle_id"] if "vehicle_id" in r.keys() else None,
            "plate": r["plate"],
            "house_id": r["house_id"],
            "house_number": r["house_number"],
            "floor": r["floor"],
            "owner_name": r["owner_name"],
            "kind": r["kind"],
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
    if direction not in ("in", "out") or kind not in ("resident", "visitor") or not plate:
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
    house_number_raw = data.get("house_number")
    house_floor_raw = data.get("house_floor")

    # Visitor metadata for an unregistered house (kept on the movement row;
    # NEVER auto-creates a houses row — that's owner-driven only).
    visitor_house_label = None
    visitor_name_for_movement = None
    visitor_phone_for_movement = None

    if not house_id and house_number_raw:
        number = validate_house_number(house_number_raw)
        if number is None:
            return jsonify({
                "error": f"House number must be between {HOUSE_NUMBER_MIN} and {HOUSE_NUMBER_MAX}"
            }), 400
        floor = validate_floor(house_floor_raw or "")
        if floor is None:
            return jsonify({"error": "Floor is required"}), 400
        existing = db.execute(
            "SELECT id FROM houses WHERE number = ? AND floor = ?",
            (number, floor),
        ).fetchone()
        if existing:
            house_id = existing["id"]
        else:
            # Visitor at an unregistered (number, floor). Don't create a houses
            # row — only the resident's own /vehicles/new flow does that.
            # Capture owner+phone on the movement so the audit log is complete.
            owner_name = (data.get("owner_name") or "").strip()
            phone_str = (data.get("phone") or "").strip()
            if not owner_name:
                return jsonify({"error": "Owner name is required"}), 400
            if not phone_str:
                return jsonify({"error": "Phone number is required"}), 400
            visitor_house_label = f"{number} {floor}"
            visitor_name_for_movement = owner_name
            visitor_phone_for_movement = phone_str

    vehicle_id = None
    if kind == "resident":
        v = db.execute("SELECT id, house_id FROM vehicles WHERE plate = ?", (plate,)).fetchone()
        if v:
            vehicle_id = v["id"]
            house_id = house_id or v["house_id"]

    if not house_id and not visitor_house_label:
        return jsonify({"error": "House is required for every log entry"}), 400

    db.execute(
        """
        INSERT INTO movements (
            house_id, vehicle_id, plate, kind, direction,
            visitor_name, visitor_phone, visitor_house, note, ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            house_id,
            vehicle_id,
            plate,
            kind,
            direction,
            visitor_name_for_movement,
            visitor_phone_for_movement,
            visitor_house_label,
            (data.get("note") or "").strip() or None,
            now_ist_str(),
        ),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/log")
@admin_required
def log_view():
    db = get_db()
    q = request.args.get("q", "").strip()
    kind_filter = request.args.get("kind", "").strip().lower()
    if kind_filter not in ("resident", "visitor"):
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
            " OR UPPER(h.owner_name) LIKE ?"
            " OR UPPER(m.visitor_name) LIKE ?"
            " OR UPPER(m.visitor_house) LIKE ?)"
        )
        params += [like, like, like, like, like]
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
               COALESCE(h.owner_name, m.visitor_name) AS house_owner,
               COALESCE(h.phone, m.visitor_phone) AS house_phone,
               h.phone_masked AS house_phone_masked,
               m.visitor_house AS unregistered_house
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
    return render_template("admin.html", stats=stats)


@app.route("/admin/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_vehicle(vehicle_id):
    db = get_db()
    row = db.execute(
        """
        SELECT v.id AS vehicle_id, v.plate, v.house_id,
               h.number AS house_number, h.floor AS house_floor,
               h.owner_name, h.phone
        FROM vehicles v JOIN houses h ON h.id = v.house_id
        WHERE v.id = ?
        """,
        (vehicle_id,),
    ).fetchone()
    if not row:
        flash("Vehicle not found", "error")
        return redirect(url_for("vehicles_list"))

    if request.method == "POST":
        new_plate = normalise_plate(request.form.get("plate", ""))
        new_owner = request.form.get("owner_name", "").strip()
        new_phone = request.form.get("phone", "").strip()
        new_number = validate_house_number(request.form.get("number", ""))
        new_floor = validate_floor(request.form.get("floor", ""))

        # Validations
        if not new_plate:
            flash("Vehicle number is required", "error")
            return redirect(url_for("admin_edit_vehicle", vehicle_id=vehicle_id))
        if not new_owner:
            flash("Owner name is required", "error")
            return redirect(url_for("admin_edit_vehicle", vehicle_id=vehicle_id))
        if not new_phone:
            flash("Phone number is required", "error")
            return redirect(url_for("admin_edit_vehicle", vehicle_id=vehicle_id))
        if new_number is None:
            flash(f"House number must be between {HOUSE_NUMBER_MIN} and {HOUSE_NUMBER_MAX}", "error")
            return redirect(url_for("admin_edit_vehicle", vehicle_id=vehicle_id))
        if new_floor is None:
            flash("Please pick a floor", "error")
            return redirect(url_for("admin_edit_vehicle", vehicle_id=vehicle_id))

        # Plate uniqueness (skip self)
        if new_plate != row["plate"]:
            clash = db.execute(
                "SELECT v.plate, h.number, h.floor FROM vehicles v "
                "JOIN houses h ON h.id = v.house_id "
                "WHERE UPPER(v.plate) = UPPER(?) AND v.id != ?",
                (new_plate, vehicle_id),
            ).fetchone()
            if clash:
                fl = FLOOR_LABELS.get(clash["floor"], clash["floor"]) if clash["floor"] else ""
                host = f"{clash['number']} ({fl})" if fl else clash["number"]
                flash(f"Vehicle {new_plate} is already registered to {host}", "error")
                return redirect(url_for("admin_edit_vehicle", vehicle_id=vehicle_id))

        # Resolve target house: existing (number, floor) → reuse; else create new.
        target_house = db.execute(
            "SELECT id FROM houses WHERE number = ? AND floor = ?",
            (new_number, new_floor),
        ).fetchone()
        old_house_id = row["house_id"]
        if target_house:
            target_house_id = target_house["id"]
            # Update owner+phone on the target (admin's edit is authoritative)
            db.execute(
                "UPDATE houses SET owner_name = ?, phone = ? WHERE id = ?",
                (new_owner, new_phone, target_house_id),
            )
        else:
            cur = db.execute(
                "INSERT INTO houses (number, owner_name, phone, floor) VALUES (?, ?, ?, ?)",
                (new_number, new_owner, new_phone, new_floor),
            )
            target_house_id = cur.lastrowid

        # Move + rename the vehicle.
        db.execute(
            "UPDATE vehicles SET house_id = ?, plate = ? WHERE id = ?",
            (target_house_id, new_plate, vehicle_id),
        )

        # If the vehicle was renamed, sync any movement-log rows so the log
        # still searches and displays the new plate.
        if new_plate != row["plate"]:
            db.execute(
                "UPDATE movements SET plate = ? WHERE plate = ?",
                (new_plate, row["plate"]),
            )

        # Movement rows still point at old_house_id (kept as-is, history fact).
        # If old house has no remaining vehicles, drop it.
        if old_house_id != target_house_id:
            remaining = db.execute(
                "SELECT COUNT(*) c FROM vehicles WHERE house_id = ?",
                (old_house_id,),
            ).fetchone()["c"]
            if remaining == 0:
                db.execute("DELETE FROM houses WHERE id = ?", (old_house_id,))

        db.commit()
        flash(f"Updated vehicle {new_plate}", "ok")
        return redirect(url_for("vehicles_list"))

    return render_template("vehicle_edit.html", v=row)


@app.post("/admin/vehicles/<int:vehicle_id>/delete")
@admin_required
def admin_delete_vehicle(vehicle_id):
    db = get_db()
    row = db.execute(
        "SELECT v.plate, h.id AS house_id, h.number AS house_number "
        "FROM vehicles v JOIN houses h ON h.id = v.house_id "
        "WHERE v.id = ?",
        (vehicle_id,),
    ).fetchone()
    if not row:
        flash("Vehicle not found", "error")
        return redirect(url_for("vehicles_list"))
    db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    # If this was the house's last vehicle, drop the house too — fully
    # vehicle-centric model: a house only exists while it has vehicles.
    remaining = db.execute(
        "SELECT COUNT(*) c FROM vehicles WHERE house_id = ?",
        (row["house_id"],),
    ).fetchone()["c"]
    if remaining == 0:
        db.execute("DELETE FROM houses WHERE id = ?", (row["house_id"],))
        db.commit()
        flash(f"Removed vehicle {row['plate']} (house {row['house_number']} had no other vehicles, also deleted)", "ok")
    else:
        db.commit()
        flash(f"Removed vehicle {row['plate']} from house {row['house_number']}", "ok")
    return redirect(url_for("vehicles_list"))


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
