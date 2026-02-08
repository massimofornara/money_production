import os
import sys
import sqlite3
import hashlib
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_session import Session
import stripe
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, FloatPrompt, IntPrompt

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

console = Console()

DB_FILE = "money_production.db"

# ================== CONFIGURAZIONE STRIPE ==================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

if not stripe.api_key or "sk_" not in stripe.api_key:
    console.print("[bold red]ERRORE CRITICO: STRIPE_SECRET_KEY non impostata![/bold red]")
    console.print("Imposta la variabile d'ambiente prima di avviare:")
    console.print("export STRIPE_SECRET_KEY='sk_live_xxxxxxxxxxxxxxxx'")
    sys.exit(1)

if not STRIPE_PUBLISHABLE_KEY or "pk_" not in STRIPE_PUBLISHABLE_KEY:
    console.print("[bold red]ERRORE: STRIPE_PUBLISHABLE_KEY non impostata![/bold red]")
    sys.exit(1)

# ================== DATABASE ==================
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
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
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      name TEXT UNIQUE NOT NULL,
                      password_hash TEXT NOT NULL,
                      balance REAL NOT NULL DEFAULT 0.0,
                      stripe_pm_id TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS logs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_name TEXT NOT NULL,
                      timestamp TEXT NOT NULL,
                      description TEXT NOT NULL,
                      amount REAL NOT NULL,
                      balance_before REAL NOT NULL,
                      balance_after REAL NOT NULL,
                      payout_id TEXT)''')

init_db()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# ================== FUNZIONI CONSOLE / API ==================
def add_user(name: str, password: str, initial_balance: float = 0.0):
    name = name.strip()
    if not name or not password:
        return False, "Nome e password obbligatori"

    try:
        pw_hash = hash_password(password)
        with get_db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO users (name, password_hash, balance) VALUES (?, ?, ?)",
                      (name, pw_hash, initial_balance))
        return True, f"Utente '{name}' creato"
    except sqlite3.IntegrityError:
        return False, "Utente già esistente"

def verify_user(name: str, password: str) -> bool:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE name = ?", (name,))
        row = c.fetchone()
        return hash_password(password) == row[0] if row else False

def get_balance(name: str) -> Optional[float]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE name = ?", (name,))
        row = c.fetchone()
        return row[0] if row else None

def get_payment_method(name: str) -> Optional[str]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT stripe_pm_id FROM users WHERE name = ?", (name,))
        row = c.fetchone()
        return row[0] if row else None

def log_transaction(name: str, description: str, amount: float, before: float, after: float, payout_id: str = None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO logs (user_name, timestamp, description, amount, balance_before, balance_after, payout_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, ts, description, amount, before, after, payout_id)
        )

# ================== ROUTES FLASK ==================
@app.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    balance = get_balance(session["user"])
    return render_template("index.html", stripe_pk=STRIPE_PUBLISHABLE_KEY, user=session["user"], balance=balance or 0.0)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")
        if verify_user(name, password):
            session["user"] = name
            return redirect(url_for("index"))
        return render_template("index.html", error="Credenziali errate")
    return render_template("index.html")

@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name")
    password = request.form.get("password")
    success, msg = add_user(name, password)
    if success:
        session["user"] = name
        return redirect(url_for("index"))
    return render_template("index.html", error=msg)

# Salva PaymentMethod dal frontend
@app.route("/api/save-payment-method", methods=["POST"])
def save_payment_method():
    if "user" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    data = request.json
    pm_id = data.get("payment_method_id")
    if not pm_id:
        return jsonify({"error": "payment_method_id mancante"}), 400

    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET stripe_pm_id = ? WHERE name = ?", (pm_id, session["user"]))
        return jsonify({"success": True, "message": "Metodo salvato"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Avvia produzione + prelievo
@app.route("/api/start-production", methods=["POST"])
def start_production():
    if "user" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    data = request.json
    amount_per_cycle = float(data.get("amount_per_cycle", 1000000.0))
    cycles = int(data.get("cycles", 5))
    user = session["user"]

    try:
        # Cicli di produzione
        for cycle in range(1, cycles + 1):
            balance_before = get_balance(user)
            balance_after = balance_before + amount_per_cycle
            with get_db() as conn:
                conn.execute("UPDATE users SET balance = ? WHERE name = ?", (balance_after, user))
            log_transaction(user, f"Ciclo {cycle}/{cycles}", amount_per_cycle, balance_before, balance_after)

        final_balance = get_balance(user)
        if final_balance <= 0:
            return jsonify({"error": "Saldo insufficiente per prelievo"}), 400

        pm_id = get_payment_method(user)
        if not pm_id:
            return jsonify({"error": "Nessun metodo di pagamento configurato"}), 400

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

if __name__ == "__main__":
    # Modalità debug (locale)
    app.run(debug=True, host="0.0.0.0", port=5000)
