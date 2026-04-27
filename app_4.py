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
latest_uid = ""
latest_snf = 0.0
latest_fat = 0.0

pending_dispenses = {}

# ================= DATABASE =================
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

# ================= HOME =================
@app.route("/")
def home():
    global latest_uid
    latest_uid = ""
    return render_template("index.html")

# ================= RFID API (ESP1 → Backend) =================
@app.route("/api/rfid", methods=["POST"])
def receive_rfid():
    global latest_uid
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON received"}), 400
    latest_uid = data.get("uid", "")
    print(f"💳 RFID RECEIVED | UID: {latest_uid}")
    return jsonify({"status": "ok"}), 200

@app.route("/api/rfid/latest")
def get_latest_rfid():
    return jsonify({
        "uid": latest_uid,
        "snf": latest_snf,
        "fat": latest_fat
    })

# ================= MILK ANALYSIS API (ESP3 → Backend) =================
@app.route("/api/milk_analysis", methods=["POST"])
def receive_milk_analysis():
    global latest_snf, latest_fat

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON received"}), 400

    latest_fat = float(data.get("fat_percent", 0.0))
    latest_snf = float(data.get("snf_percent", 0.0))

    print(f"🧪 MILK ANALYSIS RECEIVED")
    print(f"   Fat:         {latest_fat}%")
    print(f"   SNF:         {latest_snf}%")
    print(f"   Protein:     {data.get('protein_percent')}%")
    print(f"   Lactose:     {data.get('lactose_percent')}%")
    print(f"   Salt:        {data.get('salt_percent')}%")
    print(f"   Temperature: {data.get('temperature_c')}°C")

    return jsonify({"status": "ok"}), 200

# ================= DISPENSER API (Backend → ESP2) =================
# ESP2 polls this — only place that pops the job
@app.route("/api/dispenser/pull", methods=["GET"])
def dispenser_pull():
    if pending_dispenses:
        uid = list(pending_dispenses.keys())[0]
        vol = pending_dispenses.pop(uid)   # ← consumed here, nowhere else
        print(f"🥛 ESP2 Pulled Job: {vol}mL for {uid}")
        return jsonify({"status": "dispense", "uid": uid, "volume": vol})
    return jsonify({"status": "waiting"})

# ================= CHECK DISPENSE (ESP1 polls this) =================
# ESP1 only peeks — does NOT pop the job so ESP2 can still consume it
@app.route("/api/check_dispense", methods=["GET"])
def check_dispense():
    uid = request.args.get("uid", "")
    if uid in pending_dispenses:
        vol = pending_dispenses[uid]       # ← peek only, no pop
        return jsonify({"status": "dispense", "volume": vol})
    return jsonify({"status": "waiting"})

# ================= MILK UI & LOGIC =================
@app.route("/ui/milk")
def milk_page():
    return render_template(
        "milk.html",
        scanned_uid=latest_uid,
        snf=latest_snf,
        fat=latest_fat
    )

@app.route("/milk", methods=["POST"])
def milk_billing():
    global latest_uid

    uid      = request.form["uid"]
    volume_l = float(request.form["volume"])
    snf      = float(request.form["snf"])
    fat      = float(request.form["fat"])

    # ── Dynamic Pricing ──
    RATE_SNF_COEFF = 6.0
    RATE_FAT_COEFF = 8.0
    MINIMUM_RATE   = 10.0

    dynamic_rate   = (snf * RATE_SNF_COEFF) + (fat * RATE_FAT_COEFF)
    rate_per_liter = max(dynamic_rate, MINIMUM_RATE)
    total          = rate_per_liter * volume_l
    volume_ml      = volume_l * 1000.0

    # ── Database ──
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

    cur.execute(
        "UPDATE users SET balance=%s WHERE uid=%s",
        (new_balance, uid)
    )
    cur.execute("""
        INSERT INTO transactions (uid, volume, snf, fat, rate, total, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (uid, volume_ml, snf, fat, rate_per_liter, total, datetime.now()))

    conn.commit()
    conn.close()

    # ── Queue Dispense for ESP2 ──
    pending_dispenses[uid] = int(volume_ml)
    print(f"📋 Queued: {int(volume_ml)}mL for {uid}")

    latest_uid = ""
    return redirect(url_for("transactions_page"))

# ================= RECHARGE =================
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
        cur.execute(
            "UPDATE users SET balance = balance + %s WHERE uid=%s",
            (amount, uid)
        )
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

# ================= TRANSACTIONS =================
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

# ================= USERS =================
@app.route("/ui/users")
def users_page():
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT uid, balance, name FROM users")
    all_users = cur.fetchall()
    conn.close()
    return render_template("users.html", users=all_users)

# ================= RUN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
