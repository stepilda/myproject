import os
import random
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

# === imports for AI farming chatbot / prediction ===
import json as _json
import urllib.request
import urllib.parse
# ========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder="static", static_url_path="/static")

DB_PATH = os.path.join(BASE_DIR, "agapp.db")

OTP_VALID_MINUTES = 5

# === optional external AI API key (kept server-side only) ===
AGRI_AI_API_KEY = os.environ.get("AGRI_AI_API_KEY", "").strip()
AGRI_AI_API_URL = os.environ.get("AGRI_AI_API_URL", "").strip()
# ================================================================


def get_db():
    """One SQLite connection per request, reused via Flask's app context."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_buyer_profile_columns(db):
    """
    Safe migration: add the new buyer_profile columns if missing.
    Uses PRAGMA table_info so it works on existing databases without
    touching existing data or other tables.
    """
    existing = {r["name"] for r in db.execute("PRAGMA table_info(buyer_profile)").fetchall()}
    new_cols = [
        ("registration_number", "TEXT"),
        ("workers",             "TEXT"),
        ("company_ownership",   "TEXT"),
        ("company_started_year","TEXT"),
        ("company_type",        "TEXT"),
        ("manager_name",        "TEXT"),
        ("official_email",      "TEXT"),
        ("mobile_number",       "TEXT"),
    ]
    for name, coltype in new_cols:
        if name not in existing:
            db.execute(f"ALTER TABLE buyer_profile ADD COLUMN {name} {coltype}")


def init_db():
    """Create all tables if the database doesn't have them yet."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS register (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            mobile        TEXT NOT NULL UNIQUE,
            email         TEXT UNIQUE,
            location      TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL CHECK (role IN ('farmer', 'buyer')),
            land_size     TEXT,
            crops         TEXT,
            business_id   TEXT,
            business_type TEXT,
            created_at    TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS farmer_profile (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_id              INTEGER NOT NULL UNIQUE REFERENCES register(id),
            land_size_acres        TEXT,
            available_land         TEXT,
            crops                  TEXT,
            farming_type           TEXT CHECK (farming_type IN ('own', 'lease', '')),
            water_source           TEXT CHECK (water_source IN ('well', 'borewell', 'rainfed', '')),
            current_crop           TEXT,
            previous_crop          TEXT,
            expected_harvest_date  TEXT,
            quantity_crops         TEXT,
            expected_qty           TEXT,
            farming_method         TEXT CHECK (farming_method IN ('organic', 'normal', '')),
            certificate_number     TEXT,
            experience_years       TEXT,
            alternate_mobile       TEXT,
            preferred_language     TEXT,
            address_detail         TEXT,
            mobile_verified        INTEGER NOT NULL DEFAULT 0,
            updated_at             TEXT
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS buyer_profile (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id           INTEGER NOT NULL UNIQUE REFERENCES register(id),
            company_name       TEXT,
            company_location   TEXT,
            contact_name       TEXT,
            business_id        TEXT,
            business_type      TEXT,
            address_detail     TEXT,
            preferred_language TEXT,
            mobile_verified    INTEGER NOT NULL DEFAULT 0,
            updated_at         TEXT
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS otp_codes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mobile      TEXT NOT NULL,
            otp         TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            verified    INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS crop_listings (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_id         INTEGER NOT NULL REFERENCES register(id),
            crop_name         TEXT NOT NULL,
            quantity          TEXT NOT NULL,
            unit              TEXT NOT NULL DEFAULT 'kg',
            production_date   TEXT,
            ready_date        TEXT NOT NULL,
            price_amount      TEXT NOT NULL,
            price_unit        TEXT NOT NULL DEFAULT 'per kg',
            farming_method    TEXT,
            notes             TEXT,
            status            TEXT NOT NULL DEFAULT 'available'
                              CHECK (status IN ('available', 'sold', 'paused')),
            created_at        TEXT NOT NULL,
            updated_at        TEXT
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS crop_productions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_id        INTEGER NOT NULL REFERENCES register(id),
            crop_name        TEXT NOT NULL,
            quantity         TEXT NOT NULL,
            expected_qty     TEXT,
            production_date  TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_chat_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_id   INTEGER NOT NULL REFERENCES register(id),
            role        TEXT NOT NULL CHECK (role IN ('user','bot')),
            message     TEXT NOT NULL,
            language    TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )

    # Safe migration for existing databases
    _ensure_buyer_profile_columns(db)

    db.commit()
    db.close()


def read_file_from(*rel_paths):
    for rel in rel_paths:
        path = os.path.join(BASE_DIR, *rel) if isinstance(rel, tuple) else os.path.join(BASE_DIR, rel)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return None


def farmer_is_verified(db, farmer_id):
    row = db.execute(
        "SELECT mobile_verified FROM farmer_profile WHERE farmer_id = ?",
        (farmer_id,),
    ).fetchone()
    return bool(row and row["mobile_verified"])


def require_mobile_verified(db, user_id, role, mobile):
    table = "farmer_profile" if role == "farmer" else "buyer_profile"
    id_col = "farmer_id" if role == "farmer" else "buyer_id"
    already = db.execute(
        f"SELECT mobile_verified FROM {table} WHERE {id_col} = ?", (user_id,)
    ).fetchone()
    if already and already["mobile_verified"]:
        return True
    recent = db.execute(
        """
        SELECT 1 FROM otp_codes
        WHERE mobile = ? AND verified = 1 AND expires_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (mobile, datetime.now().isoformat(timespec="seconds")),
    ).fetchone()
    return bool(recent)


@app.route("/")
def home():
    html = read_file_from("index.html", ("templates", "index.html"))
    if html is None:
        return (
            "index.html not found. Place it directly in the same folder as app.py "
            "(or inside a 'templates' subfolder).",
            404,
        )
    return html


@app.route("/farmer/<int:farmer_id>")
def farmer_page(farmer_id):
    db = get_db()
    farmer = db.execute(
        "SELECT id FROM register WHERE id = ? AND role = 'farmer'", (farmer_id,)
    ).fetchone()
    if farmer is None:
        return "Farmer not found.", 404

    html = read_file_from("farmer.html", ("templates", "farmer.html"))
    if html is None:
        return "farmer.html not found.", 404
    return html


@app.route("/buyer")
@app.route("/buyer/<int:buyer_id>")
def buyer_page(buyer_id=None):
    html = read_file_from("buyer.html", ("templates", "buyer.html"))
    if html is None:
        return "buyer.html not found.", 404
    return html


@app.route("/dashboard")
def dashboard_page():
    html = read_file_from("dashboard.html", ("templates", "dashboard.html"))
    if html is None:
        return "dashboard.html not found.", 404
    return html


@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    mobile = (data.get("mobile") or "").strip()
    email = (data.get("email") or "").strip()
    location = (data.get("location") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "").strip().lower()

    if not name or not mobile or not location or not password:
        return jsonify(success=False, error="Name, mobile number, location and password are required."), 400

    if role not in ("farmer", "buyer"):
        return jsonify(success=False, error="Please choose Farmer or Buyer."), 400

    db = get_db()

    existing = db.execute(
        "SELECT id FROM register WHERE mobile = ? OR (email != '' AND email = ?)",
        (mobile, email),
    ).fetchone()
    if existing:
        return jsonify(success=False, error="An account with this mobile number or email already exists."), 409

    row = {
        "name": name,
        "mobile": mobile,
        "email": email,
        "location": location,
        "password_hash": generate_password_hash(password),
        "role": role,
        "land_size": (data.get("landSize") or "").strip(),
        "crops": (data.get("crops") or "").strip(),
        "business_id": (data.get("businessId") or "").strip(),
        "business_type": (data.get("businessType") or "").strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    cur = db.execute(
        """
        INSERT INTO register
            (name, mobile, email, location, password_hash, role,
             land_size, crops, business_id, business_type, created_at)
        VALUES
            (:name, :mobile, :email, :location, :password_hash, :role,
             :land_size, :crops, :business_id, :business_type, :created_at)
        """,
        row,
    )
    db.commit()

    return jsonify(success=True, id=cur.lastrowid)


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    identifier = (data.get("identifier") or "").strip()
    password = data.get("password") or ""

    if not identifier or not password:
        return jsonify(success=False, error="Enter your mobile/email and password."), 400

    db = get_db()
    user = db.execute(
        "SELECT * FROM register WHERE mobile = ? OR email = ?",
        (identifier, identifier),
    ).fetchone()

    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify(success=False, error="Invalid mobile/email or password."), 401

    return jsonify(
        success=True,
        id=user["id"],
        name=user["name"],
        role=user["role"],
    )


@app.route("/registrations")
def registrations():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, mobile, email, location, role, land_size, crops, "
        "business_id, business_type, created_at FROM register ORDER BY id"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/dashboard")
def dashboard_stats():
    db = get_db()
    farmers = db.execute("SELECT COUNT(*) AS n FROM register WHERE role = 'farmer'").fetchone()["n"]
    buyers = db.execute("SELECT COUNT(*) AS n FROM register WHERE role = 'buyer'").fetchone()["n"]
    verified = db.execute(
        "SELECT COUNT(*) AS n FROM farmer_profile WHERE mobile_verified = 1"
    ).fetchone()["n"]
    listings = db.execute(
        "SELECT COUNT(*) AS n FROM crop_listings WHERE status = 'available'"
    ).fetchone()["n"]
    productions = db.execute("SELECT COUNT(*) AS n FROM crop_productions").fetchone()["n"]

    crop_rows = db.execute(
        """
        SELECT crop_name, COUNT(*) AS n
        FROM crop_listings
        WHERE status = 'available'
        GROUP BY crop_name
        ORDER BY n DESC
        LIMIT 8
        """
    ).fetchall()

    recent = db.execute(
        """
        SELECT cl.crop_name, cl.quantity, cl.unit, cl.price_amount, cl.price_unit,
               cl.ready_date, r.name AS farmer_name, r.location AS farmer_location
        FROM crop_listings cl
        JOIN register r ON r.id = cl.farmer_id
        LEFT JOIN farmer_profile fp ON fp.farmer_id = cl.farmer_id
        WHERE cl.status = 'available' AND IFNULL(fp.mobile_verified, 0) = 1
        ORDER BY cl.id DESC
        LIMIT 6
        """
    ).fetchall()

    return jsonify(
        farmers=farmers,
        buyers=buyers,
        verified_farmers=verified,
        available_listings=listings,
        productions=productions,
        crops=[{"name": r["crop_name"], "count": r["n"]} for r in crop_rows],
        recent=[dict(r) for r in recent],
    )


# ----------------------------------------------------------------------
# Farmer profile API
# ----------------------------------------------------------------------

@app.route("/api/farmer/<int:farmer_id>")
def get_farmer(farmer_id):
    db = get_db()
    base = db.execute(
        "SELECT id, name, mobile, email, location, created_at "
        "FROM register WHERE id = ? AND role = 'farmer'",
        (farmer_id,),
    ).fetchone()
    if base is None:
        return jsonify(error="Farmer not found."), 404

    profile = db.execute(
        "SELECT * FROM farmer_profile WHERE farmer_id = ?", (farmer_id,)
    ).fetchone()

    result = dict(base)
    if profile:
        result.update(dict(profile))
    else:
        result.update(
            land_size_acres="", available_land="", crops="", farming_type="",
            water_source="", farming_method="", certificate_number="",
            experience_years="", alternate_mobile="", preferred_language="",
            address_detail="", mobile_verified=0, updated_at=None,
        )
    return jsonify(result)


@app.route("/api/farmer/<int:farmer_id>/profile", methods=["POST"])
def update_farmer_profile(farmer_id):
    data = request.get_json(silent=True) or {}

    db = get_db()
    farmer = db.execute(
        "SELECT id, mobile FROM register WHERE id = ? AND role = 'farmer'", (farmer_id,)
    ).fetchone()
    if farmer is None:
        return jsonify(success=False, error="Farmer not found."), 404

    if not require_mobile_verified(db, farmer_id, "farmer", farmer["mobile"]):
        return jsonify(success=False, error="Please verify your mobile number with OTP before saving."), 403

    fields = {
        "land_size_acres": (data.get("landSizeAcres") or "").strip(),
        "available_land": (data.get("availableLand") or "").strip(),
        "crops": (data.get("crops") or "").strip(),
        "farming_type": (data.get("farmingType") or "").strip().lower(),
        "water_source": (data.get("waterSource") or "").strip().lower(),
        "farming_method": (data.get("farmingMethod") or "").strip().lower(),
        "certificate_number": (data.get("certificateNumber") or "").strip(),
        "experience_years": (data.get("experienceYears") or "").strip(),
        "alternate_mobile": (data.get("alternateMobile") or "").strip(),
        "preferred_language": (data.get("preferredLanguage") or "").strip(),
        "address_detail": (data.get("addressDetail") or "").strip(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    exists = db.execute(
        "SELECT id FROM farmer_profile WHERE farmer_id = ?", (farmer_id,)
    ).fetchone()

    if exists:
        db.execute(
            """
            UPDATE farmer_profile SET
                land_size_acres = :land_size_acres,
                available_land = :available_land,
                crops = :crops,
                farming_type = :farming_type,
                water_source = :water_source,
                farming_method = :farming_method,
                certificate_number = :certificate_number,
                experience_years = :experience_years,
                alternate_mobile = :alternate_mobile,
                preferred_language = :preferred_language,
                address_detail = :address_detail,
                mobile_verified = 1,
                updated_at = :updated_at
            WHERE farmer_id = :farmer_id
            """,
            {**fields, "farmer_id": farmer_id},
        )
    else:
        db.execute(
            """
            INSERT INTO farmer_profile
                (farmer_id, land_size_acres, available_land, crops, farming_type,
                 water_source, farming_method, certificate_number,
                 experience_years, alternate_mobile, preferred_language, address_detail,
                 mobile_verified, updated_at)
            VALUES
                (:farmer_id, :land_size_acres, :available_land, :crops, :farming_type,
                 :water_source, :farming_method, :certificate_number,
                 :experience_years, :alternate_mobile, :preferred_language, :address_detail,
                 1, :updated_at)
            """,
            {**fields, "farmer_id": farmer_id},
        )

    db.commit()
    return jsonify(success=True)


# ----------------------------------------------------------------------
# Crop production details (not the marketplace listing)
# ----------------------------------------------------------------------

@app.route("/api/farmer/<int:farmer_id>/productions", methods=["GET"])
def get_productions(farmer_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM crop_productions WHERE farmer_id = ? ORDER BY production_date DESC, id DESC",
        (farmer_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/farmer/<int:farmer_id>/productions", methods=["POST"])
def create_production(farmer_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    farmer = db.execute(
        "SELECT id FROM register WHERE id = ? AND role = 'farmer'", (farmer_id,)
    ).fetchone()
    if farmer is None:
        return jsonify(success=False, error="Farmer not found."), 404

    crop_name = (data.get("cropName") or "").strip()
    quantity = (data.get("quantity") or "").strip()
    expected_qty = (data.get("expectedQty") or "").strip()
    production_date = (data.get("productionDate") or "").strip()

    if not crop_name or not quantity or not production_date:
        return jsonify(success=False, error="Crop, quantity and date are required."), 400

    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        """
        INSERT INTO crop_productions
            (farmer_id, crop_name, quantity, expected_qty, production_date, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (farmer_id, crop_name, quantity, expected_qty, production_date, now, now),
    )
    db.commit()
    return jsonify(success=True, id=cur.lastrowid)


@app.route("/api/farmer/<int:farmer_id>/productions/<int:prod_id>", methods=["DELETE"])
def delete_production(farmer_id, prod_id):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM crop_productions WHERE id = ? AND farmer_id = ?",
        (prod_id, farmer_id),
    ).fetchone()
    if existing is None:
        return jsonify(success=False, error="Record not found."), 404
    db.execute("DELETE FROM crop_productions WHERE id = ? AND farmer_id = ?", (prod_id, farmer_id))
    db.commit()
    return jsonify(success=True)


# ----------------------------------------------------------------------
# Crop listings for sale
# ----------------------------------------------------------------------

@app.route("/api/farmer/<int:farmer_id>/listings", methods=["GET"])
def get_farmer_listings(farmer_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM crop_listings WHERE farmer_id = ? ORDER BY ready_date, id DESC",
        (farmer_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/farmer/<int:farmer_id>/listings", methods=["POST"])
def create_listing(farmer_id):
    data = request.get_json(silent=True) or {}

    db = get_db()
    farmer = db.execute(
        "SELECT id FROM register WHERE id = ? AND role = 'farmer'", (farmer_id,)
    ).fetchone()
    if farmer is None:
        return jsonify(success=False, error="Farmer not found."), 404

    crop_name = (data.get("cropName") or "").strip()
    quantity = (data.get("quantity") or "").strip()
    ready_date = (data.get("readyDate") or "").strip()
    price_amount = (data.get("priceAmount") or "").strip()

    if not crop_name or not quantity or not ready_date or not price_amount:
        return jsonify(
            success=False,
            error="Crop name, quantity, ready-to-sell date and price are required.",
        ), 400

    now = datetime.now().isoformat(timespec="seconds")
    fields = {
        "farmer_id": farmer_id,
        "crop_name": crop_name,
        "quantity": quantity,
        "unit": (data.get("unit") or "kg").strip(),
        "production_date": (data.get("productionDate") or "").strip(),
        "ready_date": ready_date,
        "price_amount": price_amount,
        "price_unit": (data.get("priceUnit") or "per kg").strip(),
        "farming_method": (data.get("farmingMethod") or "").strip().lower(),
        "notes": (data.get("notes") or "").strip(),
        "status": (data.get("status") or "available").strip().lower(),
        "created_at": now,
        "updated_at": now,
    }

    cur = db.execute(
        """
        INSERT INTO crop_listings
            (farmer_id, crop_name, quantity, unit, production_date, ready_date,
             price_amount, price_unit, farming_method, notes, status,
             created_at, updated_at)
        VALUES
            (:farmer_id, :crop_name, :quantity, :unit, :production_date, :ready_date,
             :price_amount, :price_unit, :farming_method, :notes, :status,
             :created_at, :updated_at)
        """,
        fields,
    )
    db.commit()
    return jsonify(success=True, id=cur.lastrowid)


@app.route("/api/farmer/<int:farmer_id>/listings/<int:listing_id>", methods=["PUT"])
def update_listing(farmer_id, listing_id):
    data = request.get_json(silent=True) or {}

    db = get_db()
    existing = db.execute(
        "SELECT id FROM crop_listings WHERE id = ? AND farmer_id = ?",
        (listing_id, farmer_id),
    ).fetchone()
    if existing is None:
        return jsonify(success=False, error="Listing not found."), 404

    crop_name = (data.get("cropName") or "").strip()
    quantity = (data.get("quantity") or "").strip()
    ready_date = (data.get("readyDate") or "").strip()
    price_amount = (data.get("priceAmount") or "").strip()

    if not crop_name or not quantity or not ready_date or not price_amount:
        return jsonify(
            success=False,
            error="Crop name, quantity, ready-to-sell date and price are required.",
        ), 400

    fields = {
        "id": listing_id,
        "farmer_id": farmer_id,
        "crop_name": crop_name,
        "quantity": quantity,
        "unit": (data.get("unit") or "kg").strip(),
        "production_date": (data.get("productionDate") or "").strip(),
        "ready_date": ready_date,
        "price_amount": price_amount,
        "price_unit": (data.get("priceUnit") or "per kg").strip(),
        "farming_method": (data.get("farmingMethod") or "").strip().lower(),
        "notes": (data.get("notes") or "").strip(),
        "status": (data.get("status") or "available").strip().lower(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    db.execute(
        """
        UPDATE crop_listings SET
            crop_name = :crop_name,
            quantity = :quantity,
            unit = :unit,
            production_date = :production_date,
            ready_date = :ready_date,
            price_amount = :price_amount,
            price_unit = :price_unit,
            farming_method = :farming_method,
            notes = :notes,
            status = :status,
            updated_at = :updated_at
        WHERE id = :id AND farmer_id = :farmer_id
        """,
        fields,
    )
    db.commit()
    return jsonify(success=True)


@app.route("/api/farmer/<int:farmer_id>/listings/<int:listing_id>", methods=["DELETE"])
def delete_listing(farmer_id, listing_id):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM crop_listings WHERE id = ? AND farmer_id = ?",
        (listing_id, farmer_id),
    ).fetchone()
    if existing is None:
        return jsonify(success=False, error="Listing not found."), 404

    db.execute("DELETE FROM crop_listings WHERE id = ? AND farmer_id = ?", (listing_id, farmer_id))
    db.commit()
    return jsonify(success=True)


@app.route("/api/listings", methods=["GET"])
def public_listings():
    """All available crop listings from every farmer."""
    crop_q = (request.args.get("crop") or "").strip().lower()
    location_q = (request.args.get("location") or "").strip().lower()
    method_q = (request.args.get("method") or "").strip().lower()

    db = get_db()
    rows = db.execute(
        """
        SELECT cl.*, r.name AS farmer_name, r.mobile AS farmer_mobile,
               r.location AS farmer_location
        FROM crop_listings cl
        JOIN register r ON r.id = cl.farmer_id
        LEFT JOIN farmer_profile fp ON fp.farmer_id = cl.farmer_id
        WHERE cl.status = 'available'
        ORDER BY cl.ready_date ASC, cl.id DESC
        """
    ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        if crop_q and crop_q not in d["crop_name"].lower():
            continue
        if location_q and location_q not in (d["farmer_location"] or "").lower():
            continue
        if method_q and method_q != (d["farming_method"] or "").lower():
            continue
        results.append(d)

    return jsonify(results)


# ----------------------------------------------------------------------
# NEW: Farmer list for buyers (non-sensitive fields only)
# ----------------------------------------------------------------------

@app.route("/api/farmers", methods=["GET"])
def list_farmers_for_buyers():
    """
    Returns a safe, non-sensitive list of registered farmers.
    Only shows public marketplace-relevant fields:
    name, location, land size, crops, farming type/method, water source,
    and a mobile_verified flag. No email, no mobile, no password, no cert.
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT r.id            AS farmer_id,
               r.name          AS name,
               r.location      AS location,
               fp.land_size_acres   AS land_size_acres,
               fp.available_land    AS available_land,
               fp.crops             AS crops,
               fp.farming_type      AS farming_type,
               fp.farming_method    AS farming_method,
               fp.water_source      AS water_source,
               fp.mobile_verified   AS mobile_verified
        FROM register r
        LEFT JOIN farmer_profile fp ON fp.farmer_id = r.id
        WHERE r.role = 'farmer'
        ORDER BY r.name COLLATE NOCASE ASC
        """
    ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        result.append({
            "id": d.get("farmer_id"),
            "name": d.get("name") or "",
            "location": d.get("location") or "",
            "land_size_acres": d.get("land_size_acres") or "",
            "available_land": d.get("available_land") or "",
            "crops": d.get("crops") or "",
            "farming_type": d.get("farming_type") or "",
            "farming_method": d.get("farming_method") or "",
            "water_source": d.get("water_source") or "",
            "mobile_verified": bool(d.get("mobile_verified")),
        })
    return jsonify(result)


# ----------------------------------------------------------------------
# Buyer profile
# ----------------------------------------------------------------------

@app.route("/api/buyer/<int:buyer_id>")
def get_buyer(buyer_id):
    db = get_db()
    base = db.execute(
        "SELECT id, name, mobile, email, location, business_id, business_type, created_at "
        "FROM register WHERE id = ? AND role = 'buyer'",
        (buyer_id,),
    ).fetchone()
    if base is None:
        return jsonify(error="Buyer not found."), 404

    profile = db.execute(
        "SELECT * FROM buyer_profile WHERE buyer_id = ?", (buyer_id,)
    ).fetchone()

    result = dict(base)
    has_profile = False
    if profile:
        result.update(dict(profile))
        if not result.get("company_name"):
            result["company_name"] = result.get("name") or ""
        if not result.get("company_location"):
            result["company_location"] = result.get("location") or ""
        if not result.get("contact_name"):
            result["contact_name"] = result.get("name") or ""
        has_profile = bool((profile["company_name"] or "").strip())
    else:
        result.update(
            company_name="",
            company_location="",
            contact_name="",
            address_detail="",
            preferred_language="",
            mobile_verified=0,
            updated_at=None,
            registration_number="",
            workers="",
            company_ownership="",
            company_started_year="",
            company_type="",
            manager_name="",
            official_email="",
            mobile_number=result.get("mobile") or "",
        )
    result["has_profile"] = has_profile
    return jsonify(result)


def _save_buyer_profile(buyer_id):
    """Internal helper shared by POST and PUT. Idempotent upsert."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    buyer = db.execute(
        "SELECT id, mobile FROM register WHERE id = ? AND role = 'buyer'", (buyer_id,)
    ).fetchone()
    if buyer is None:
        return jsonify(success=False, error="Buyer not found."), 404

    company_name       = (data.get("companyName") or "").strip()
    company_location   = (data.get("companyLocation") or "").strip()
    contact_name       = (data.get("contactName") or "").strip()
    business_id        = (data.get("businessId") or "").strip()
    business_type      = (data.get("businessType") or "").strip()
    address_detail     = (data.get("addressDetail") or "").strip()
    preferred_language = (data.get("preferredLanguage") or "").strip()

    registration_number  = (data.get("registrationNumber") or "").strip()
    workers              = (data.get("workers") or "").strip()
    company_ownership    = (data.get("companyOwnership") or "").strip().lower()
    company_started_year = (data.get("companyStartedYear") or "").strip()
    company_type         = (data.get("companyType") or "").strip()
    manager_name         = (data.get("managerName") or "").strip()
    official_email       = (data.get("officialEmail") or "").strip()
    mobile_number        = (data.get("mobileNumber") or "").strip()

    if not company_name or not company_location:
        return jsonify(success=False, error="Company name and location are required."), 400

    if workers:
        try:
            int(workers)
        except ValueError:
            return jsonify(success=False, error="Number of workers must be a valid number."), 400

    if company_started_year:
        try:
            y = int(company_started_year)
            if y < 1800 or y > 2200:
                raise ValueError
        except ValueError:
            return jsonify(success=False, error="Company started year must be a valid year."), 400

    if official_email and ("@" not in official_email or "." not in official_email.split("@")[-1]):
        return jsonify(success=False, error="Official email format looks invalid."), 400

    if company_ownership and company_ownership not in ("own", "rental"):
        company_ownership = ""

    fields = {
        "company_name":       company_name,
        "company_location":   company_location,
        "contact_name":       contact_name or manager_name,
        "business_id":        business_id or registration_number,
        "business_type":      business_type or company_type,
        "address_detail":     address_detail,
        "preferred_language": preferred_language,
        "registration_number":   registration_number,
        "workers":               workers,
        "company_ownership":     company_ownership,
        "company_started_year":  company_started_year,
        "company_type":          company_type,
        "manager_name":          manager_name,
        "official_email":        official_email,
        "mobile_number":         mobile_number,
        "updated_at":            datetime.now().isoformat(timespec="seconds"),
    }

    exists = db.execute(
        "SELECT id FROM buyer_profile WHERE buyer_id = ?", (buyer_id,)
    ).fetchone()

    if exists:
        db.execute(
            """
            UPDATE buyer_profile SET
                company_name          = :company_name,
                company_location      = :company_location,
                contact_name          = :contact_name,
                business_id           = :business_id,
                business_type         = :business_type,
                address_detail        = :address_detail,
                preferred_language    = :preferred_language,
                registration_number   = :registration_number,
                workers               = :workers,
                company_ownership     = :company_ownership,
                company_started_year  = :company_started_year,
                company_type          = :company_type,
                manager_name          = :manager_name,
                official_email        = :official_email,
                mobile_number         = :mobile_number,
                mobile_verified       = 1,
                updated_at            = :updated_at
            WHERE buyer_id = :buyer_id
            """,
            {**fields, "buyer_id": buyer_id},
        )
    else:
        db.execute(
            """
            INSERT INTO buyer_profile
                (buyer_id, company_name, company_location, contact_name,
                 business_id, business_type, address_detail, preferred_language,
                 registration_number, workers, company_ownership,
                 company_started_year, company_type, manager_name,
                 official_email, mobile_number, mobile_verified, updated_at)
            VALUES
                (:buyer_id, :company_name, :company_location, :contact_name,
                 :business_id, :business_type, :address_detail, :preferred_language,
                 :registration_number, :workers, :company_ownership,
                 :company_started_year, :company_type, :manager_name,
                 :official_email, :mobile_number, 1, :updated_at)
            """,
            {**fields, "buyer_id": buyer_id},
        )

    db.execute(
        """
        UPDATE register SET
            name = :contact_name,
            location = :company_location,
            business_id = :business_id,
            business_type = :business_type
        WHERE id = :buyer_id
        """,
        {**fields, "buyer_id": buyer_id},
    )
    db.commit()
    return jsonify(success=True)


@app.route("/api/buyer/<int:buyer_id>/profile", methods=["POST"])
def update_buyer_profile(buyer_id):
    # POST = create-or-update (idempotent). Kept for backward compatibility.
    return _save_buyer_profile(buyer_id)


@app.route("/api/buyer/<int:buyer_id>/profile", methods=["PUT"])
def put_buyer_profile(buyer_id):
    # PUT = explicit update. Same behaviour, so no duplicate profile is created.
    return _save_buyer_profile(buyer_id)


# ----------------------------------------------------------------------
# OTP mobile verification (farmer-only in practice; buyer endpoints kept)
# ----------------------------------------------------------------------

@app.route("/api/send-otp", methods=["POST"])
def send_otp():
    data = request.get_json(silent=True) or {}
    mobile = (data.get("mobile") or "").strip()

    if not mobile:
        return jsonify(success=False, error="Mobile number is required."), 400

    otp = f"{random.randint(0, 999999):06d}"
    now = datetime.now()
    expires_at = now + timedelta(minutes=OTP_VALID_MINUTES)

    db = get_db()
    db.execute(
        "INSERT INTO otp_codes (mobile, otp, created_at, expires_at, verified) VALUES (?, ?, ?, ?, 0)",
        (mobile, otp, now.isoformat(timespec="seconds"), expires_at.isoformat(timespec="seconds")),
    )
    db.commit()

    print(f"[DEMO SMS] OTP for {mobile} is {otp} (valid {OTP_VALID_MINUTES} min)")

    return jsonify(
        success=True,
        message=f"OTP sent to {mobile}.",
        demo_otp=otp,
    )


@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json(silent=True) or {}
    mobile = (data.get("mobile") or "").strip()
    otp = (data.get("otp") or "").strip()
    farmer_id = data.get("farmerId")
    buyer_id = data.get("buyerId")

    if not mobile or not otp:
        return jsonify(success=False, error="Mobile number and OTP are required."), 400

    db = get_db()
    row = db.execute(
        """
        SELECT id FROM otp_codes
        WHERE mobile = ? AND otp = ? AND verified = 0 AND expires_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (mobile, otp, datetime.now().isoformat(timespec="seconds")),
    ).fetchone()

    if row is None:
        return jsonify(success=False, error="Invalid or expired OTP."), 400

    db.execute("UPDATE otp_codes SET verified = 1 WHERE id = ?", (row["id"],))
    now = datetime.now().isoformat(timespec="seconds")

    if farmer_id:
        exists = db.execute(
            "SELECT id FROM farmer_profile WHERE farmer_id = ?", (farmer_id,)
        ).fetchone()
        if exists:
            db.execute(
                "UPDATE farmer_profile SET mobile_verified = 1 WHERE farmer_id = ?",
                (farmer_id,),
            )
        else:
            db.execute(
                "INSERT INTO farmer_profile (farmer_id, mobile_verified, updated_at) VALUES (?, 1, ?)",
                (farmer_id, now),
            )

    if buyer_id:
        exists = db.execute(
            "SELECT id FROM buyer_profile WHERE buyer_id = ?", (buyer_id,)
        ).fetchone()
        if exists:
            db.execute(
                "UPDATE buyer_profile SET mobile_verified = 1 WHERE buyer_id = ?",
                (buyer_id,),
            )
        else:
            db.execute(
                "INSERT INTO buyer_profile (buyer_id, mobile_verified, updated_at) VALUES (?, 1, ?)",
                (buyer_id, now),
            )

    db.commit()
    return jsonify(success=True, message="Mobile number verified.")


# ======================================================================
# ============ AI FARMING CHATBOT + PREDICTION ENGINE ============
# ======================================================================

_AI_I18N = {
    "en": {
        "greet": "Namaste! I am your AgriLink farming assistant. Ask me about seeds, crops, water, soil or weather.",
        "ask_soil": "Could you tell me the type of soil on your land (for example: red, black, sandy, loamy, clay)? That will help me give a better suggestion.",
        "no_data": "I do not yet have enough recorded history to make a confident suggestion. Once more production records are added, I will be able to guide you better. In the meantime, tell me your soil type and what you are planning to grow, and I will share what I can.",
        "need_crop": "Which crop are you asking about? For example: paddy, tomato, groundnut, maize, cotton.",
        "recommend_header": "Based on the current conditions and the available farming information, you can consider these crop options:",
        "alt_header": "You can also consider:",
        "why": "Why:",
        "weather_header": "Weather near you:",
        "water_title": "Water advice for your farm",
        "tip": "Tip:",
        "soil_ask_short": "Please share your soil type and I will refine the suggestion."
    },
    "ta": {
        "greet": "வணக்கம்! நான் உங்கள் AgriLink விவசாய உதவியாளர். விதை, பயிர், நீர், மண் அல்லது வானிலை பற்றி கேளுங்கள்.",
        "ask_soil": "உங்கள் நிலத்தின் மண் வகையைச் சொல்ல முடியுமா? (உதாரணம்: சிவப்பு, கருப்பு, மணல், வண்டல், களி). அது சிறந்த பரிந்துரையைத் தர உதவும்.",
        "no_data": "நம்பிக்கையான பரிந்துரைக்கு இன்னும் போதுமான பதிவுகள் இல்லை. மேலும் உற்பத்தி பதிவுகள் சேர்க்கப்பட்ட பிறகு நான் சிறப்பாக வழிகாட்ட முடியும். இப்போதைக்கு உங்கள் மண் வகை மற்றும் நீங்கள் பயிரிட திட்டமிடும் பயிரைச் சொன்னால், என்னால் முடிந்த உதவியைத் தருகிறேன்.",
        "need_crop": "எந்த பயிர் பற்றி கேட்கிறீர்கள்? உதாரணம்: நெல், தக்காளி, நிலக்கடலை, மக்காச்சோளம், பருத்தி.",
        "recommend_header": "தற்போதைய நிலைகள் மற்றும் கிடைக்கும் விவசாய தகவல்களின் அடிப்படையில், இந்த பயிர் வழிகளை பரிசீலிக்கலாம்:",
        "alt_header": "இவற்றையும் பரிசீலிக்கலாம்:",
        "why": "காரணம்:",
        "weather_header": "உங்கள் பகுதியில் வானிலை:",
        "water_title": "உங்கள் பண்ணைக்கான நீர் ஆலோசனை",
        "tip": "குறிப்பு:",
        "soil_ask_short": "உங்கள் மண் வகையைச் சொன்னால் பரிந்துரையை மேலும் துல்லியமாக்குகிறேன்."
    },
    "hi": {
        "greet": "नमस्ते! मैं आपका AgriLink कृषि सहायक हूँ। बीज, फसल, पानी, मिट्टी या मौसम के बारे में पूछें।",
        "ask_soil": "क्या आप अपनी ज़मीन की मिट्टी का प्रकार बता सकते हैं? (जैसे: लाल, काली, रेतीली, दोमट, चिकनी)। इससे बेहतर सुझाव देने में मदद मिलेगी।",
        "no_data": "अभी भरोसेमंद सुझाव देने के लिए पर्याप्त रिकॉर्ड नहीं हैं। जैसे ही और उत्पादन रिकॉर्ड जुड़ेंगे, मैं बेहतर मार्गदर्शन कर पाऊँगा। फ़िलहाल, अपनी मिट्टी का प्रकार और जो फसल आप उगाने की सोच रहे हैं, बताइए — मैं जो मदद कर सकता हूँ, करूँगा।",
        "need_crop": "आप किस फसल के बारे में पूछ रहे हैं? जैसे: धान, टमाटर, मूंगफली, मक्का, कपास।",
        "recommend_header": "वर्तमान परिस्थितियों और उपलब्ध कृषि जानकारी के आधार पर, आप इन फसल विकल्पों पर विचार कर सकते हैं:",
        "alt_header": "आप इन पर भी विचार कर सकते हैं:",
        "why": "कारण:",
        "weather_header": "आपके पास का मौसम:",
        "water_title": "आपके खेत के लिए सिंचाई सलाह",
        "tip": "सुझाव:",
        "soil_ask_short": "अपनी मिट्टी का प्रकार बताएं, मैं सुझाव को और सटीक बना दूँगा।"
    }
}


def _ai_t(lang, key):
    table = _AI_I18N.get(lang) or _AI_I18N["en"]
    return table.get(key, _AI_I18N["en"].get(key, ""))


def _fetch_weather(location_text):
    if not location_text:
        return None
    try:
        q = urllib.parse.quote(location_text.strip())
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json"
        with urllib.request.urlopen(geo_url, timeout=6) as r:
            geo = _json.loads(r.read().decode("utf-8"))
        results = geo.get("results") or []
        if not results:
            return None
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]

        wx_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,precipitation,weather_code"
            "&daily=precipitation_sum,temperature_2m_max,temperature_2m_min"
            "&timezone=auto&forecast_days=3"
        )
        with urllib.request.urlopen(wx_url, timeout=6) as r:
            wx = _json.loads(r.read().decode("utf-8"))
        cur = wx.get("current") or {}
        daily = wx.get("daily") or {}
        return {
            "temperature_c": cur.get("temperature_2m"),
            "precipitation_mm": cur.get("precipitation"),
            "daily_precip": daily.get("precipitation_sum") or [],
            "tmax": daily.get("temperature_2m_max") or [],
            "tmin": daily.get("temperature_2m_min") or [],
        }
    except Exception:
        return None


def _season_from_month(month):
    if month in (6, 7, 8, 9, 10):
        return "kharif"
    if month in (11, 12, 1, 2, 3):
        return "rabi"
    return "zaid"


def _gather_farmer_context(db, farmer_id):
    profile = db.execute(
        "SELECT * FROM farmer_profile WHERE farmer_id = ?", (farmer_id,)
    ).fetchone()
    base = db.execute(
        "SELECT id, name, mobile, email, location FROM register WHERE id = ? AND role = 'farmer'",
        (farmer_id,),
    ).fetchone()
    productions = db.execute(
        "SELECT crop_name, quantity, expected_qty, production_date FROM crop_productions WHERE farmer_id = ?",
        (farmer_id,),
    ).fetchall()
    listings = db.execute(
        "SELECT crop_name, quantity, unit, status FROM crop_listings WHERE farmer_id = ?",
        (farmer_id,),
    ).fetchall()
    return {
        "base": dict(base) if base else {},
        "profile": dict(profile) if profile else {},
        "productions": [dict(p) for p in productions],
        "listings": [dict(l) for l in listings],
    }


def _aggregate_other_farmers(db, exclude_farmer_id, crop_hint=None):
    params = []
    sql = (
        "SELECT cp.crop_name, cp.quantity, cp.production_date "
        "FROM crop_productions cp "
        "WHERE cp.farmer_id != ?"
    )
    params.append(exclude_farmer_id)
    if crop_hint:
        sql += " AND LOWER(cp.crop_name) LIKE ?"
        params.append(f"%{crop_hint.lower()}%")
    rows = db.execute(sql, params).fetchall()

    buckets = {}
    for r in rows:
        crop = (r["crop_name"] or "").strip().lower()
        if not crop:
            continue
        try:
            qty = float(str(r["quantity"]).strip())
        except Exception:
            qty = None
        b = buckets.setdefault(crop, {"n": 0, "total": 0.0, "count_with_qty": 0})
        b["n"] += 1
        if qty is not None:
            b["total"] += qty
            b["count_with_qty"] += 1

    summary = []
    for crop, b in buckets.items():
        avg = (b["total"] / b["count_with_qty"]) if b["count_with_qty"] else None
        summary.append({
            "crop": crop,
            "records": b["n"],
            "avg_quantity": round(avg, 2) if avg is not None else None,
        })
    summary.sort(key=lambda x: (-x["records"], -(x["avg_quantity"] or 0)))
    return summary


def _score_crops(context, agg_others, weather, season, soil_text, crops_interest):
    scores = []
    profile = context.get("profile") or {}
    farmer_crops_text = (profile.get("crops") or "").lower()
    water_source = (profile.get("water_source") or "").lower()
    farming_method = (profile.get("farming_method") or "").lower()
    own_prod = context.get("productions") or []
    own_crop_set = {(p.get("crop_name") or "").lower() for p in own_prod if p.get("crop_name")}

    candidate_crops = set()
    for item in agg_others:
        candidate_crops.add(item["crop"])
    if crops_interest:
        for c in crops_interest:
            candidate_crops.add(c.lower())
    for c in own_crop_set:
        candidate_crops.add(c)

    for crop in candidate_crops:
        if not crop:
            continue
        score = 0.0
        reasons = []

        for item in agg_others:
            if item["crop"] == crop:
                if item["records"] >= 3:
                    score += 3
                    reasons.append("similar farms reported good results")
                elif item["records"] > 0:
                    score += item["records"] * 0.7
                    reasons.append("some similar farms reported results")
                if item["avg_quantity"]:
                    score += min(item["avg_quantity"] / 100.0, 3.0)
                break

        for p in own_prod:
            if (p.get("crop_name") or "").lower() == crop:
                score += 2
                reasons.append("you have grown this before")
                break

        if crop and crop in farmer_crops_text:
            score += 1.5
            reasons.append("already part of your farm plan")

        if soil_text:
            s = soil_text.lower()
            soil_rules = {
                "red":     {"groundnut", "millet", "pulses", "cotton"},
                "black":   {"cotton", "soybean", "wheat", "chickpea", "sugarcane"},
                "sandy":   {"groundnut", "watermelon", "millet", "coconut"},
                "loamy":   {"paddy", "tomato", "maize", "banana", "vegetables"},
                "clay":    {"paddy", "sugarcane", "banana"},
                "alluvial":{"paddy", "wheat", "sugarcane", "vegetables"},
            }
            for k, crops in soil_rules.items():
                if k in s:
                    if any(cc in crop or crop in cc for cc in crops):
                        score += 2
                        reasons.append(f"suits {k} soil")
                    else:
                        score -= 0.5
                    break

        if water_source == "rainfed":
            if crop in {"paddy", "sugarcane", "banana"}:
                score -= 1.5
                reasons.append("needs a lot of water")
            else:
                score += 0.5
        elif water_source in ("well", "borewell"):
            if crop in {"paddy", "sugarcane", "banana", "vegetables"}:
                score += 1
                reasons.append("your water source suits this crop")

        if weather:
            rain3 = sum([float(x or 0) for x in (weather.get("daily_precip") or [])])
            tmax = max([float(x or 0) for x in (weather.get("tmax") or [])] or [0])
            if rain3 > 40:
                if crop in {"paddy", "sugarcane"}:
                    score += 1
                    reasons.append("recent rain pattern suits this crop")
                elif crop in {"groundnut", "millet"}:
                    score -= 1
            if tmax and tmax > 38:
                if crop in {"millet", "cotton"}:
                    score += 0.5
                elif crop in {"tomato", "vegetables"}:
                    score -= 0.5
                    reasons.append("hot weather may stress this crop")

        season_rules = {
            "kharif": {"paddy", "maize", "cotton", "groundnut", "millet", "soybean"},
            "rabi":   {"wheat", "chickpea", "mustard", "barley", "peas"},
            "zaid":   {"watermelon", "cucumber", "millet", "fodder"},
        }
        if any(cc in crop for cc in season_rules.get(season, set())):
            score += 1.5
            reasons.append("good match for the current season")

        if farming_method == "organic" and "organic" in crop:
            score += 1

        scores.append({
            "crop": crop,
            "score": round(score, 2),
            "reasons": reasons[:4],
        })

    scores.sort(key=lambda x: -x["score"])
    return scores


def _detect_intent(message):
    m = (message or "").lower()
    if any(k in m for k in ["seed", "விதை", "बीज"]):
        return "seed"
    if any(k in m for k in ["water", "irrigat", "தண்ணீர்", "நீர்", "पानी"]):
        return "water"
    if any(k in m for k in ["weather", "rain", "வானிலை", "மழை", "मौसम", "बारिश"]):
        return "weather"
    if any(k in m for k in ["soil", "மண்", "मिट्टी"]):
        return "soil"
    if any(k in m for k in ["which crop", "what crop", "which seed", "suggest", "recommend",
                            "எந்த பயிர்", "எந்த விதை", "कौन सी फसल", "कौन सा बीज"]):
        return "recommend"
    if any(k in m for k in ["next", "அடுத்து", "अगला"]):
        return "next"
    return "general"


def _extract_crops_from_text(message, known_crops):
    m = (message or "").lower()
    return [c for c in known_crops if c and c in m]


def _reply_general(lang, context, weather, season, message):
    profile = context.get("profile") or {}
    crops = (profile.get("crops") or "").strip()
    land = (profile.get("land_size_acres") or "").strip()
    water = (profile.get("water_source") or "").strip()

    lines = [_ai_t(lang, "greet")]
    lines.append("")

    if land:
        lines.append("Your land size: " + land + " acres")
    if crops:
        lines.append("Crops on your profile: " + crops)
    if water:
        lines.append("Water source: " + water)
    if weather and weather.get("temperature_c") is not None:
        lines.append("Current temperature near you: " + str(weather["temperature_c"]) + "°C")

    lines.append("")
    if lang == "ta":
        lines.append("இவற்றைக் கேளுங்கள்: \"எந்த விதை பயன்படுத்தலாம்?\", \"இந்த பருவத்திற்கு எந்த பயிர்?\", அல்லது \"அடுத்து என்ன பயிரிடலாம்?\"")
    elif lang == "hi":
        lines.append("आप पूछ सकते हैं: \"कौन सा बीज उपयोग करूँ?\", \"इस मौसम के लिए कौन सी फसल?\", या \"अगला क्या उगाऊँ?\"")
    else:
        lines.append("You can ask me: \"Which seed should I use?\", \"Which crop for this season?\", or \"What should I grow next?\"")
    return "\n".join(lines)


def _reply_soil(lang, soil_text):
    return _ai_t(lang, "ask_soil")


def _reply_weather(lang, weather):
    if not weather:
        if lang == "ta":
            return "இப்போது உங்கள் பகுதிக்கான வானிலையைப் பெற முடியவில்லை. உங்கள் சுயவிவரத்தில் உள்ள இருப்பிடத்தைச் சரிபார்க்கவும்."
        if lang == "hi":
            return "अभी आपके क्षेत्र का मौसम प्राप्त नहीं कर सका। कृपया अपनी प्रोफ़ाइल में स्थान जाँचें।"
        return "I could not fetch the weather for your location right now. Please check your location in your farm profile."

    lines = [_ai_t(lang, "weather_header")]
    lines.append("• Temperature now: " + str(weather.get("temperature_c")) + "°C")
    lines.append("• Current precipitation: " + str(weather.get("precipitation_mm")) + " mm")
    dp = weather.get("daily_precip") or []
    if dp:
        lines.append("• Next 3 days rainfall (mm): " + ", ".join(str(x) for x in dp))
    if lang == "ta":
        lines.append(_ai_t(lang, "tip") + " மழை அதிகமாக இருந்தால் நீர்ப்பாசனத்தைக் குறைக்கவும்; வறட்சி இருந்தால் பயிர் தேர்வில் கவனம் தேவை.")
    elif lang == "hi":
        lines.append(_ai_t(lang, "tip") + " अधिक बारिश हो तो सिंचाई कम करें; सूखा हो तो फसल चुनाव में सावधानी रखें।")
    else:
        lines.append(_ai_t(lang, "tip") + " Reduce irrigation if heavy rain is expected, and prefer drought-tolerant crops if it stays dry.")
    return "\n".join(lines)


def _reply_water(lang, context, weather):
    profile = context.get("profile") or {}
    water = (profile.get("water_source") or "").lower()
    lines = [_ai_t(lang, "water_title")]

    if water == "well":
        lines.append("• You have a well. Irrigate early morning or evening to reduce evaporation.")
    elif water == "borewell":
        lines.append("• You have a borewell. Monitor the water table and avoid over-irrigation.")
    elif water == "rainfed":
        lines.append("• Your land is rainfed. Prefer drought-tolerant crops and conserve soil moisture with mulching.")
    else:
        lines.append("• Add your water source in your farm profile for tailored advice.")

    if weather:
        rain3 = sum([float(x or 0) for x in (weather.get("daily_precip") or [])])
        if rain3 > 40:
            lines.append(_ai_t(lang, "tip") + " Heavy rain expected in the next few days — reduce irrigation this week.")
        elif rain3 < 5:
            lines.append(_ai_t(lang, "tip") + " Very little rain expected — plan irrigation accordingly.")
    return "\n".join(lines)


def _reply_recommend(lang, context, agg_others, weather, season, soil_text, crops_interest,
                     header_key="recommend_header"):
    if not agg_others and not context.get("productions"):
        base = _ai_t(lang, "no_data") + "\n\n" + _ai_t(lang, "ask_soil_short")
        return base

    scored = _score_crops(context, agg_others, weather, season, soil_text, crops_interest)
    if not scored:
        return _ai_t(lang, "no_data")

    lines = [_ai_t(lang, header_key), ""]
    top = scored[:3]

    for i, item in enumerate(top, 1):
        lines.append(f"{i}. **{item['crop'].title()}**")
        friendly = []
        for r in item["reasons"]:
            if any(bad in r.lower() for bad in ["database", "field", "record", "score", "sql", "seed field"]):
                continue
            friendly.append(r)
        if friendly:
            lines.append("   " + _ai_t(lang, "why") + " " + "; ".join(friendly))
        lines.append("")

    others = scored[3:6]
    if others:
        lines.append(_ai_t(lang, "alt_header"))
        for item in others:
            lines.append("• " + item["crop"].title())
        lines.append("")

    if lang == "ta":
        lines.append(_ai_t(lang, "tip") + " நடவு செய்வதற்கு முன் மண் ஈரப்பதத்தையும் வானிலையையும் பாருங்கள்.")
    elif lang == "hi":
        lines.append(_ai_t(lang, "tip") + " बुवाई से पहले मिट्टी की नमी और मौसम देख लें।")
    else:
        lines.append(_ai_t(lang, "tip") + " Check soil moisture and local weather before planting.")

    return "\n".join(lines)


def _reply_next(lang, context, agg_others, weather, season, soil_text):
    return _reply_recommend(lang, context, agg_others, weather, season, soil_text, [],
                            header_key="recommend_header")


def _generate_reply(db, farmer_id, message, language):
    lang = language if language in _AI_I18N else "en"
    context = _gather_farmer_context(db, farmer_id)
    profile = context.get("profile") or {}
    location = context["base"].get("location") or profile.get("address_detail") or ""

    known = db.execute(
        "SELECT DISTINCT LOWER(crop_name) AS c FROM crop_productions"
    ).fetchall()
    known_crops = [r["c"] for r in known if r["c"]]

    crops_interest = _extract_crops_from_text(message, known_crops)

    weather = _fetch_weather(location)
    season = _season_from_month(datetime.now().month)
    soil_text = ""

    intent = _detect_intent(message)

    if intent == "weather":
        return _reply_weather(lang, weather)
    if intent == "soil":
        return _reply_soil(lang, soil_text)
    if intent == "water":
        return _reply_water(lang, context, weather)
    if intent in ("recommend", "seed"):
        agg_others = _aggregate_other_farmers(db, farmer_id,
                                              crop_hint=crops_interest[0] if crops_interest else None)
        return _reply_recommend(lang, context, agg_others, weather, season, soil_text, crops_interest)
    if intent == "next":
        agg_others = _aggregate_other_farmers(db, farmer_id)
        return _reply_next(lang, context, agg_others, weather, season, soil_text)

    return _reply_general(lang, context, weather, season, message)


@app.route("/api/farming/chat", methods=["POST"])
def farming_chat():
    data = request.get_json(silent=True) or {}
    farmer_id = data.get("farmerId")
    message = (data.get("message") or "").strip()
    language = (data.get("language") or "en").strip().lower()
    if language not in _AI_I18N:
        language = "en"

    if not farmer_id or not message:
        return jsonify(success=False, error="farmerId and message are required."), 400

    try:
        farmer_id_int = int(farmer_id)
    except Exception:
        return jsonify(success=False, error="Invalid farmerId."), 400

    db = get_db()
    farmer = db.execute(
        "SELECT id FROM register WHERE id = ? AND role = 'farmer'", (farmer_id_int,)
    ).fetchone()
    if farmer is None:
        return jsonify(success=False, error="Farmer not found."), 404

    reply_text = None
    if AGRI_AI_API_KEY and AGRI_AI_API_URL:
        try:
            system_prompt = (
                "You are an agriculture advisor for Indian farmers. "
                "Reply concisely, in a friendly and clear tone. "
                "Do NOT mention databases, tables, fields, or any internal implementation details. "
                "Do NOT include scoring notes, season notes, or developer explanations. "
                "Respond in the language requested by the user: "
                "English for 'en', Tamil for 'ta', Hindi for 'hi'."
            )
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message + f"\n\n[reply_language={language}]"},
                ],
                "temperature": 0.3,
            }
            req = urllib.request.Request(
                AGRI_AI_API_URL,
                data=_json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {AGRI_AI_API_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = _json.loads(r.read().decode("utf-8"))
            choices = resp.get("choices") or []
            if choices:
                reply_text = (choices[0].get("message") or {}).get("content", "").strip()
        except Exception:
            reply_text = None

    if not reply_text:
        reply_text = _generate_reply(db, farmer_id_int, message, language)

    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO ai_chat_log (farmer_id, role, message, language, created_at) VALUES (?,?,?,?,?)",
        (farmer_id_int, "user", message, language, now),
    )
    db.execute(
        "INSERT INTO ai_chat_log (farmer_id, role, message, language, created_at) VALUES (?,?,?,?,?)",
        (farmer_id_int, "bot", reply_text, language, now),
    )
    db.commit()

    return jsonify(success=True, reply=reply_text)


@app.route("/api/farming/predict", methods=["POST"])
def farming_predict():
    data = request.get_json(silent=True) or {}
    farmer_id = data.get("farmerId")
    language = (data.get("language") or "en").strip().lower()
    crops_interest = data.get("crops") or []

    if not farmer_id:
        return jsonify(success=False, error="farmerId is required."), 400
    try:
        farmer_id_int = int(farmer_id)
    except Exception:
        return jsonify(success=False, error="Invalid farmerId."), 400

    db = get_db()
    farmer = db.execute(
        "SELECT id FROM register WHERE id = ? AND role = 'farmer'", (farmer_id_int,)
    ).fetchone()
    if farmer is None:
        return jsonify(success=False, error="Farmer not found."), 404

    context = _gather_farmer_context(db, farmer_id_int)
    location = context["base"].get("location") or ""
    weather = _fetch_weather(location)
    season = _season_from_month(datetime.now().month)
    agg = _aggregate_other_farmers(db, farmer_id_int)
    scored = _score_crops(context, agg, weather, season, "", crops_interest)

    return jsonify(
        success=True,
        season=season,
        weather=weather,
        recommendations=scored[:6],
        aggregated_other_farmers=agg[:10],
    )
# ======================================================================
# ======================== END OF NEW CODE ============================
# ======================================================================


if __name__ == "__main__":
    init_db()
    app.run(debug=True)