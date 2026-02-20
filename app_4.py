import os
from flask import Flask, render_template, request, redirect, url_for, jsonify
from datetime import datetime
import mysql.connector

app = Flask(__name__)

# ================= CONFIGURATION =================
DB_HOST = os.environ.get("DB_HOST", "gateway01.ap-southeast-1.prod.aws.tidbcloud.com") 
DB_USER = os.environ.get("DB_USER", "2smpUV5w6ViQjKx.root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "LHq7rRwOBrVkhQDb")
DB_NAME = os.environ.get("DB_NAME", "test") 
DB_PORT = int(os.environ.get("DB_PORT", 4000)) 

# ================= STATE & HARDWARE QUEUE =================
latest_uid = ""

# 🔥 This stores volumes (IN MILLILITERS) waiting for ESP 2 to pull
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

# ================= RFID APIs ==================
@app.route("/api/rfid", methods=["POST"])
def receive_rfid():
    global latest_uid
    data = request.get_json()
    latest_uid = data.get("uid", "")
    print("RFID RECEIVED:", latest_uid)
    return {"status": "ok"}

@app.route("/api/rfid/latest")
def get_latest_rfid():
    return jsonify({"uid": latest_uid})

# ================= DISPENSER API (Hardware Pull) =================
@app.route("/api/dispenser/pull", methods=["GET"])
def dispenser_pull():
    """ESP 2 constantly polls this URL. Jobs here are already in mL."""
    if pending_dispenses:
        uid = list(pending_dispenses.keys())[0]
        vol_ml = pending_dispenses.pop(uid) 
        print(f"🥛 ESP 2 Pulled Job: {vol_ml}mL for {uid}")
        return jsonify({"status": "dispense", "uid": uid, "volume": vol_ml})
    
    return jsonify({"status": "waiting"})

# ================= MILK BILLING & DISPENSE =======================
@app.route("/ui/milk")
def milk_page():
    return render_template("milk.html")

@app.route("/milk", methods=["POST"])
def milk_billing():
    global latest_uid

    uid = request.form["uid"]
    volume_liters = float(request.form["volume"]) # Read as Liters
    snf = float(request.form["snf"])
    water = float(request.form["water"])

    # --- DYNAMIC PRICING FORMULA ---
    base_rate = 40.0 
    
    # SNF factor: +4 rupees for every point above 8.5, -4 for every point below
    snf_adjustment = (snf - 8.5) * 4.0 
    
    # Water factor: -2 rupees for every 1% of water
    water_penalty = water * 2.0 
    
    # Calculate final rate, but never let it drop below 15 rupees/Liter
    calculated_rate = base_rate + snf_adjustment - water_penalty
    rate = max(15.0, calculated_rate)

    total = rate * volume_liters
    # -------------------------------

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT balance FROM users WHERE uid=%s", (uid,))
    user = cur.fetchone()

    if not user:
        conn.close()
        return f"User with RFID {uid} not found!", 404
        
    if float(user["balance"]) < total:
        conn.close()
        return "Insufficient balance", 400

    new_balance = float(user["balance"]) - total

    # Update Balance
    cur.execute(
        "UPDATE users SET balance=%s WHERE uid=%s",
        (new_balance, uid)
    )

    # Record Transaction (Saved in Liters)
    cur.execute("""
        INSERT INTO transactions
        (uid, volume, snf, water, rate, total, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (uid, volume_liters, snf, water, rate, total, datetime.now()))

    conn.commit()
    conn.close()

    # 🔥 ADD TO QUEUE FOR ESP32 (Convert Liters to mL)
    volume_ml = int(volume_liters * 1000)
    pending_dispenses[uid] = volume_ml  

    latest_uid = ""   

    return redirect(url_for("transactions_page"))

# ================= RECHARGE ===================
@app.route("/ui/recharge")
def recharge_page():
    return render_template("recharge.html")

@app.route("/recharge", methods=["POST"])
def recharge():
    uid = request.form["uid"]
    amount = float(request.form["amount"])

    conn = get_db_connection()
    cur = conn.cursor()

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

# ================= TRANSACTIONS ===============
@app.route("/transactions")
def transactions_page():
    conn = get_db_connection()
    cur = conn.cursor()

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

# ================= USERS ======================
@app.route("/ui/users")
def users_page():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT uid, balance, name FROM users")
    all_users = cur.fetchall()
    conn.close()
    return render_template("users.html", users=all_users)

# ================= RUN ========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0", port=port)
