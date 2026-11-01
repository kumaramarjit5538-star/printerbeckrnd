import os
import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# CORS: सभी फ्रंटएंड डोमेन (GitHub Pages, Netlify, Localhost आदि) को अनुमति दें
CORS(app, resources={r"/*": {"origins": "*"}})

# हाई-क्वालिटी Base64 फोटोज़ और साइन के लिए 50MB तक के JSON पेलोड की अनुमति
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# भारतीय मानक समय (IST = UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# SQLite डेटाबेस फाइल और थ्रेड लॉक (ताकि एक साथ कई फोन जुड़ने पर टोकन आपस में न टकराएं)
DB_PATH = os.environ.get("KIOSK_DB_PATH", "kiosk_store.db")
db_lock = threading.Lock()


def get_ist_now():
    """वर्तमान भारतीय समय (IST) लौटाता है।"""
    return datetime.now(IST)


def get_ist_date_str():
    """भारतीय समय के हिसाब से आज की तारीख (YYYY-MM-DD) लौटाता है।"""
    return get_ist_now().strftime("%Y-%m-%d")


def format_token(num):
    """टोकन संख्या को '01', '02', '03'... फॉर्मेट में बदलता है।"""
    try:
         clean_num = int("".join(filter(str.isdigit, str(num))))
    except ValueError:
        clean_num = 1
    return f"{max(1, clean_num):02d}"


def init_db():
    """सर्वर चालू होते ही डेटाबेस और टेबल बनाता है।"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS print_jobs (
                token_id TEXT PRIMARY KEY,
                date_str TEXT,
                customer_name TEXT,
                total_price REAL,
                payload_json TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()


def check_and_reset_daily_state(conn):
    """
    भारतीय समय (IST) के हिसाब से तारीख चेक करता है।
    जैसे ही अगली सुबह तारीख बदलती है, टोकन काउंटर को 0 पर रीसेट कर देता है
    और पुराने दिन के सभी सेव किए गए प्रिंट जॉब्स साफ कर देता है।
    """
    today_str = get_ist_date_str()
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta_state WHERE key = 'current_date'")
    row = cur.fetchone()
    saved_date = row[0] if row else None

    if saved_date != today_str:
        # नया दिन शुरू हो गया: पुराने दिन का सारा डेटा साफ करें और काउंटर रीसेट करें
        cur.execute("DELETE FROM print_jobs")
        cur.execute(
            "INSERT OR REPLACE INTO meta_state (key, value) VALUES ('current_date', ?)",
            (today_str,)
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta_state (key, value) VALUES ('token_counter', '0')"
        )
        conn.commit()

    return today_str


# सर्वर स्टार्टअप पर DB तैयार करें
init_db()


# ==============================================================================
# 1. UPTIMEROBOT KEEP-ALIVE ROUTE (GET /ping)
# ==============================================================================
@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping_awake():
    now_ist = get_ist_now()
    return jsonify({
        "status": "awake",
        "service": "QuickPrint Kiosk Backend",
        "date_ist": now_ist.strftime("%Y-%m-%d"),
        "time": now_ist.strftime("%Y-%m-%d %I:%M:%S %p IST")
    }), 200


# ==============================================================================
# 2. LIVE DAILY TOKEN GENERATOR (POST /api/new-token & /api/token/next)
# ==============================================================================
@app.route("/api/new-token", methods=["POST", "GET"])
@app.route("/api/token/next", methods=["POST", "GET"])
def generate_new_token():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            today_str = check_and_reset_daily_state(conn)
            cur = conn.cursor()

            cur.execute("SELECT value FROM meta_state WHERE key = 'token_counter'")
            row = cur.fetchone()
            current_count = int(row[0]) if row else 0

            # हर नई स्क्रीन या '➕ नया टोकन' बटन पर अगला नंबर दें (#01, #02, #03...)
            next_count = current_count + 1
            cur.execute(
                "INSERT OR REPLACE INTO meta_state (key, value) VALUES ('token_counter', ?)",
                (str(next_count),)
            )
            conn.commit()

            token_str = format_token(next_count)
            return jsonify({
                "success": True,
                "token": token_str,
                "formatted_token": f"#{token_str}",
                "date_ist": today_str
            }), 200
        finally:
            conn.close()


# ==============================================================================
# 3. SAVE CUSTOMER EDITABLE PAGE (POST /api/job/<token_id>)
# ==============================================================================
@app.route("/api/job/<token_id>", methods=["POST"])
@app.route("/api/print-job/<token_id>", methods=["POST"])
def save_customer_job(token_id):
    clean_token = format_token(token_id)
    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "कोई JSON डेटा प्राप्त नहीं हुआ (Invalid or empty payload)"
        }), 400

    # यदि फ्रंटएंड ने सीधा KioskState भेजा है या { state: KioskState } भेजा है, दोनों को संभालें
    state_obj = data.get("state") if isinstance(data.get("state"), dict) else data
    state_obj["token"] = clean_token

    customer_name = (
        data.get("customerName")
        or state_obj.get("customerName")
        or "ग्राहक"
    )
    total_price = float(data.get("totalPrice") or state_obj.get("totalPrice") or 0)
    now_ist_str = get_ist_now().strftime("%Y-%m-%d %I:%M:%S %p IST")

    full_record = {
        "token": clean_token,
        "date": get_ist_date_str(),
        "customerName": customer_name,
        "totalPrice": total_price,
        "updatedAt": now_ist_str,
        "state": state_obj
    }

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            today_str = check_and_reset_daily_state(conn)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO print_jobs
                (token_id, date_str, customer_name, total_price, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                clean_token,
                today_str,
                customer_name,
                total_price,
                json.dumps(full_record, ensure_ascii=False),
                now_ist_str
            ))
            conn.commit()
        finally:
            conn.close()

    return jsonify({
        "success": True,
        "message": f"टोकन #{clean_token} का पेज सफलतापूर्वक सेव हो गया",
        "token": clean_token,
        "updatedAt": now_ist_str
    }), 200


# ==============================================================================
# 4. LOAD SHOPKEEPER EDITABLE PAGE BY TOKEN (GET /api/job/<token_id>)
# ==============================================================================
@app.route("/api/job/<token_id>", methods=["GET"])
@app.route("/api/print-job/<token_id>", methods=["GET"])
def get_shopkeeper_job(token_id):
    clean_token = format_token(token_id)

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            today_str = check_and_reset_daily_state(conn)
            cur = conn.cursor()
            cur.execute(
                "SELECT payload_json, updated_at FROM print_jobs WHERE token_id = ? AND date_str = ?",
                (clean_token, today_str)
            )
            row = cur.fetchone()
        finally:
            conn.close()

    if not row:
        return jsonify({
            "success": False,
            "error": f"टोकन #{clean_token} का डेटा आज ({today_str}) के रिकॉर्ड में नहीं मिला।"
        }), 404

    saved_payload = json.loads(row[0])
    return jsonify({
        "success": True,
        "token": clean_token,
        "updatedAt": row[1],
        "customerName": saved_payload.get("customerName", "ग्राहक"),
        "totalPrice": saved_payload.get("totalPrice", 0),
        "state": saved_payload.get("state", saved_payload)
    }), 200


# ==============================================================================
# 5. OPTIONAL: LIST ALL TODAY'S JOBS FOR SHOPKEEPER DASHBOARD (GET /api/jobs)
# ==============================================================================
@app.route("/api/jobs", methods=["GET"])
def list_today_jobs():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            today_str = check_and_reset_daily_state(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT token_id, customer_name, total_price, updated_at
                FROM print_jobs
                WHERE date_str = ?
                ORDER BY token_id DESC
            """, (today_str,))
            rows = cur.fetchall()
        finally:
            conn.close()

    jobs = [
        {
            "token": r[0],
            "customerName": r[1],
            "totalPrice": r[2],
            "updatedAt": r[3]
        }
        for r in rows
    ]
    return jsonify({
        "success": True,
        "date_ist": today_str,
        "count": len(jobs),
        "jobs": jobs
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
