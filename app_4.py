import os
from flask import Flask, render_template, request, redirect, url_for, jsonify
from datetime import datetime
import mysql.connector

app = Flask(__name__)

# ================= CONFIGURATION (FILL THIS!) =================
# If you are running locally, paste your TiDB details here.
# If you are on Render, add these as Environment Variables in the dashboard.
DB_HOST = os.environ.get("DB_HOST", "gateway01.ap-southeast-1.prod.aws.tidbcloud.com") 
DB_USER = os.environ.get("DB_USER", "2smpUV5w6ViQjKx.root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "LHq7rRwOBrVkhQDb")
DB_NAME = os.environ.get("DB_NAME", "test") # Usually 'test' for TiDB free tier
DB_PORT = int(os.environ.get("DB_PORT", 4000)) # TiDB uses 4000, not 3306

# ================= RFID STATE =================
latest_uid = ""

# ================= DATABASE ===================
def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            port=DB_PORT,
            # TiDB requires SSL. This ensures it connects securely.
            ssl_disabled=False 
        )
        return connection
    except mysql.connector.Error as err:
        print(f"❌ DATABASE CONNECTION ERROR: {err}")
        print("Did you paste the correct Host/User/Password in app_4.py?")
        raise err

# ================= HOME =======================
@app.route("/")
def home():
    global latest_uid
    latest_uid = ""   # 🔥 CLEAR RFID ON HOME
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

# ================= MILK =======================
@app.route("/ui/milk")
def milk_page():
    return render_template("milk.html")

@app.route("/milk", methods=["POST"])
def milk_billing():
    global latest_uid

    uid = request.form["uid"]
    volume = float(request.form["volume"])
    snf = float(request.form["snf"])
    water = float(request.form["water"])

    rate = 40
    if snf >= 8.5:
        rate += 2
    if water > 2:
        rate -= 2

    total = rate * volume

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # Check User Balance
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

    # Record Transaction
    cur.execute("""
        INSERT INTO transactions
        (uid, volume, snf, water, rate, total, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (uid, volume, snf, water, rate, total, datetime.now()))

    conn.commit()
    conn.close()

    latest_uid = ""   # 🔥 CLEAR AFTER SUCCESS

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

    # Check if user exists, if so update, else create
    cur.execute("SELECT uid FROM users WHERE uid=%s", (uid,))
    if cur.fetchone():
        cur.execute(
            "UPDATE users SET balance = balance + %s WHERE uid=%s",
            (amount, uid)
        )
    else:
        # Create new user if they don't exist yet (Auto-Registration)
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
    # Fetches UID, Balance, and Name from the users table
    cur.execute("SELECT uid, balance, name FROM users")
    all_users = cur.fetchall()
    conn.close()
    return render_template("users.html", users=all_users)

# ================= RUN ========================
if __name__ == "__main__":
    # Debug=True is fine for local testing, but Gunicorn will override this on Render

    app.run(debug=True)
    port = int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0", port=port)
