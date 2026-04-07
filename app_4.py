import os
from flask import Flask, render_template, request, redirect, url_for, jsonify
from datetime import datetime
import mysql.connector

app = Flask(__name__)

# ================= CONFIGURATION =================
DB_HOST     = os.environ.get("DB_HOST",     "gateway01.ap-southeast-1.prod.aws.tidbcloud.com")
DB_USER     = os.environ.get("DB_USER",     "2smpUV5w6ViQjKx.root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "LHq7rRwOBrVkhQDb")
DB_NAME     = os.environ.get("DB_NAME",     "test")
DB_PORT     = int(os.environ.get("DB_PORT", 4000))

# ================= STATE MANAGEMENT =================
latest_uid   = ""
latest_snf   = 0.0   # 🔥 Received from ESP32 sensor
latest_water = 0.0   # 🔥 Received from ESP32 sensor

pending_dispenses = {}

# ================= DATABASE ===================
def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            port=DB_PORT,
            ssl_disabled=False
        )
        return connection
    except mysql.connector.Error as err:
        print(f"❌ DATABASE CONNECTION ERROR: {err}")
        raise err

# ================= HOME =======================
@app.route("/")
def home():
    global latest_uid
    latest_uid = ""
    return render_template("index.html")

# ================= RFID API (ESP32 → Backend) ==================
@app.route("/api/rfid", methods=["POST"])
def receive_rfid():
    """
    ESP32 posts UID + live sensor readings here.
    SNF and water are stored as read-only state for the milk form.
    """
    global latest_uid, latest_snf, latest_water

    data = request.get_json()

    latest_uid   = data.get("uid",   "")
    latest_snf   = float(data.get("snf",   0.0))   # 🔥 From ESP32 sensor
    latest_water = float(data.get("water", 0.0))   # 🔥 From ESP32 sensor

    print(f"💳 RFID RECEIVED  | UID: {latest_uid}")
    print(f"🧪 SENSOR READING | SNF: {latest_snf}%  |  Water: {latest_water}%")

    return jsonify({"status": "ok"})

@app.route("/api/rfid/latest")
def get_latest_rfid():
    return jsonify({
        "uid":   latest_uid,
        "snf":   latest_snf,
        "water": latest_water
    })

# ================= DISPENSER API (Backend → ESP2) =================
@app.route("/api/dispenser/pull", methods=["GET"])
def dispenser_pull():
    if pending_dispenses:
        uid = list(pending_dispenses.keys())[0]
        vol = pending_dispenses.pop(uid)
        print(f"🥛 ESP 2 Pulled Job: {vol}mL for {uid}")
        return jsonify({"status": "dispense", "uid": uid, "volume": vol})

    return jsonify({"status": "waiting"})

# ================= CHECK DISPENSE (ESP1 polls this) ===============
@app.route("/api/check_dispense", methods=["GET"])
def check_dispense():
    uid = request.args.get("uid", "")
    if uid in pending_dispenses:
        vol = pending_dispenses.pop(uid)
        return jsonify({"status": "dispense", "volume": vol})
    return jsonify({"status": "waiting"})

# ================= MILK UI & LOGIC =======================
@app.route("/ui/milk")
def milk_page():
    """
    SNF and water are passed from live ESP32 sensor readings.
    The template renders them as read-only — operator only sets volume.
    """
    return render_template(
        "milk.html",
        scanned_uid=latest_uid,
        snf=latest_snf,           # 🔥 Read-only, from sensor
        water=latest_water        # 🔥 Read-only, from sensor
    )

@app.route("/milk", methods=["POST"])
def milk_billing():
    global latest_uid

    # Operator submits volume only — SNF & water come from hidden fields
    # (populated server-side, not editable by operator)
    uid      = request.form["uid"]
    volume_l = float(request.form["volume"])
    snf      = float(request.form["snf"])    # Hidden field, pre-filled from sensor
    water    = float(request.form["water"])  # Hidden field, pre-filled from sensor

    # ---- DYNAMIC PRICING ----
    RATE_SNF_COEFF   = 6.0
    RATE_WATER_COEFF = 2.5
    MINIMUM_RATE     = 10.0

    dynamic_rate   = (snf * RATE_SNF_COEFF) - (water * RATE_WATER_COEFF)
    rate_per_liter = max(dynamic_rate, MINIMUM_RATE)
    total          = rate_per_liter * volume_l
    volume_ml      = volume_l * 1000.0

    # ---- DATABASE ----
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)

    cur.execute("SELECT balance FROM users WHERE uid=%s", (uid,))
    user = cur.fetchone()

    if not user:
        conn.close()
        return f"User with RFID {uid} not found!", 404

    if float(user["balance"]) < total:
        conn.close()
        return "Insufficient balance", 400

    new_balance = float(user["balance"]) - total

    cur.execute("UPDATE users SET balance=%s WHERE uid=%s", (new_balance, uid))
    cur.execute("""
        INSERT INTO transactions (uid, volume, snf, water, rate, total, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (uid, volume_ml, snf, water, rate_per_liter, total, datetime.now()))

    conn.commit()
    conn.close()

    # ---- QUEUE DISPENSE FOR ESP2 ----
    pending_dispenses[uid] = int(volume_ml)

    latest_uid = ""
    return redirect(url_for("transactions_page"))

# ================= RECHARGE =======================
@app.route("/ui/recharge")
def recharge_page():
    return render_template("recharge.html", scanned_uid=latest_uid)

@app.route("/recharge", methods=["POST"])
def recharge():
    uid    = request.form["uid"]
    amount = float(request.form["amount"])

    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("SELECT uid FROM users WHERE uid=%s", (uid,))
    if cur.fetchone():
        cur.execute("UPDATE users SET balance = balance + %s WHERE uid=%s", (amount, uid))
    else:
        cur.execute(
            "INSERT INTO users (uid, balance, name) VALUES (%s, %s, %s)",
            (uid, amount, "Unknown User")
        )

    cur.execute("""
        INSERT INTO recharge_transactions (uid, amount, timestamp)
        VALUES (%s, %s, %s)
    """, (uid, amount, datetime.now()))

    conn.commit()
    conn.close()
    return redirect(url_for("transactions_page"))

# ================= TRANSACTIONS ===================
@app.route("/transactions")
def transactions_page():
    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT timestamp, uid, 'RECHARGE' AS type, amount AS credit, NULL AS debit
        FROM recharge_transactions
        UNION ALL
        SELECT timestamp, uid, 'MILK' AS type, NULL, total
        FROM transactions
        ORDER BY timestamp DESC
    """)

    rows = cur.fetchall()
    conn.close()
    return render_template("transactions.html", rows=rows)

# ================= USERS ==========================
@app.route("/ui/users")
def users_page():
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT uid, balance, name FROM users")
    all_users = cur.fetchall()
    conn.close()
    return render_template("users.html", users=all_users)

# ================= RUN ============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
