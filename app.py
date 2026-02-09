import os
import sqlite3
import hashlib
from datetime import datetime
from contextlib import contextmanager

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_session import Session
import stripe

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY non impostata nelle variabili d'ambiente!")

app.config["SESSION_TYPE"] = "filesystem"
Session(app)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

if not stripe.api_key or "sk_" not in stripe.api_key:
    raise RuntimeError("STRIPE_SECRET_KEY non valida o mancante!")
if not STRIPE_PUBLISHABLE_KEY or "pk_" not in STRIPE_PUBLISHABLE_KEY:
    raise RuntimeError("STRIPE_PUBLISHABLE_KEY non valida o mancante!")

DB_FILE = "money_production.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0.0,
            stripe_pm_id TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT,
            timestamp TEXT,
            description TEXT,
            amount REAL,
            balance_before REAL,
            balance_after REAL,
            payout_id TEXT
        )''')

init_db()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ================== ROUTES ==================
@app.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    
    balance = get_balance(session["user"]) or 0.0
    return render_template("index.html", stripe_pk=STRIPE_PUBLISHABLE_KEY, user=session["user"], balance=balance)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")
        if verify_user(name, password):
            session["user"] = name
            return redirect(url_for("index"))
        error = "Credenziali errate"
    return render_template("index.html", error=error, show_login=True)

@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name")
    password = request.form.get("password")
    error = None

    if not name or not password:
        error = "Nome e password obbligatori"
    else:
        success, msg = add_user(name, password)
        if success:
            session["user"] = name
            return redirect(url_for("index"))
        error = msg

    return render_template("index.html", error=error, show_login=False)

@app.route("/api/save-payment-method", methods=["POST"])
def save_payment_method():
    if "user" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    data = request.json
    pm_id = data.get("payment_method_id")
    if not pm_id:
        return jsonify({"error": "Nessun payment_method_id"}), 400

    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET stripe_pm_id = ? WHERE name = ?", (pm_id, session["user"]))
        return jsonify({"success": True, "message": "Metodo salvato correttamente"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/start-production", methods=["POST"])
def start_production():
    if "user" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    data = request.json
    amount_per_cycle = float(data.get("amount_per_cycle", 1000000.0))
    cycles = int(data.get("cycles", 5))
    user = session["user"]

    try:
        for cycle in range(1, cycles + 1):
            balance_before = get_balance(user)
            balance_after = balance_before + amount_per_cycle
            with get_db() as conn:
                conn.execute("UPDATE users SET balance = ? WHERE name = ?", (balance_after, user))
            log_transaction(user, f"Ciclo {cycle}/{cycles}", amount_per_cycle, balance_before, balance_after)

        final_balance = get_balance(user)
        if final_balance <= 0:
            return jsonify({"error": "Saldo insufficiente per il prelievo"}), 400

        pm_id = get_payment_method(user)
        if not pm_id:
            return jsonify({"error": "Nessun metodo di pagamento configurato. Salva prima carta o IBAN."}), 400

        payout = stripe.Payout.create(
            amount=int(final_balance * 100),
            currency="eur",
            destination=pm_id,
            method="instant",
            metadata={"user": user}
        )

        with get_db() as conn:
            conn.execute("UPDATE users SET balance = 0 WHERE name = ?", (user,))

        log_transaction(user, "PRELIEVO FINALE", -final_balance, final_balance, 0.0, payout.id)

        return jsonify({
            "success": True,
            "payout_id": payout.id,
            "amount": final_balance,
            "status": payout.status
        })

    except stripe.error.StripeError as e:
        return jsonify({"error": e.user_message or str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================== HELPER ==================
def get_balance(name):
    with get_db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE name = ?", (name,)).fetchone()
        return row["balance"] if row else None

def get_payment_method(name):
    with get_db() as conn:
        row = conn.execute("SELECT stripe_pm_id FROM users WHERE name = ?", (name,)).fetchone()
        return row["stripe_pm_id"] if row else None

def log_transaction(name, description, amount, before, after, payout_id=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO logs (user_name, timestamp, description, amount, balance_before, balance_after, payout_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, ts, description, amount, before, after, payout_id)
        )

def add_user(name, password, initial_balance=0.0):
    name = name.strip()
    if not name or not password:
        return False, "Nome e password obbligatori"
    try:
        pw_hash = hash_password(password)
        with get_db() as conn:
            conn.execute("INSERT INTO users (name, password_hash, balance) VALUES (?, ?, ?)",
                         (name, pw_hash, initial_balance))
        return True, "Utente creato"
    except sqlite3.IntegrityError:
        return False, "Utente già esistente"

def verify_user(name, password):
    with get_db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE name = ?", (name,)).fetchone()
        return row and hash_password(password) == row["password_hash"]

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
