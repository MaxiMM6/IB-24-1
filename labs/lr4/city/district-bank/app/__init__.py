"""Район «Банк»: счета жителей, переводы, касса и контроль крупных переводов.

Места, важные для безопасности, помечены комментарием «ИБ:».
"""

import secrets

from flask import abort, flash, redirect, render_template, request, url_for

from citykit import create_city_app, current_user, get_db, log, login_required, publish_event, role_required, has_role

DEMO_BALANCE = 10_000
BIG_TRANSFER = 50_000
MAX_DEPOSIT = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT UNIQUE NOT NULL,
    owner_sub  TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id       INTEGER REFERENCES accounts (id),
    to_id         INTEGER NOT NULL REFERENCES accounts (id),
    amount        INTEGER NOT NULL CHECK (amount > 0),
    status        TEXT NOT NULL CHECK (status IN ('done', 'pending', 'rejected')),
    initiator_sub TEXT NOT NULL,
    approver_sub  TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

NAV = [("/", "Мои счета"), ("/transfer", "Перевод"), ("/cashier", "Касса"), ("/control", "Контроль"), ("/history", "История")]


def create_app(test_config: dict | None = None):
    app = create_city_app(__name__, "bank", "Банк", NAV, SCHEMA, test_config)

    def own_account(account_id: int):
        """ИБ: счёт ищем по номеру И владельцу. Чужой и несуществующий выглядят одинаково — 404."""
        acc = (
            get_db()
            .execute("SELECT * FROM accounts WHERE id = ? AND owner_sub = ?", (account_id, current_user()["sub"]))
            .fetchone()
        )
        if acc is None:
            log(
                "WARN",
                "access.denied",
                "failure",
                "попытка открыть чужой или несуществующий счёт",
                details={"account_id": account_id},
            )
            abort(404)
        return acc

    def parse_amount(raw: str, limit: int) -> int | None:
        if not raw.isdigit():
            return None
        value = int(raw)
        return value if 0 < value <= limit else None

    def move_money(db, from_id: int | None, to_id: int, amount: int) -> bool:
        if from_id is not None:
            cur = db.execute(
                "UPDATE accounts SET balance = balance - ? WHERE id = ? AND balance >= ?", (amount, from_id, amount)
            )
            if cur.rowcount != 1:
                return False
        db.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, to_id))
        return True

    @app.route("/")
    @login_required
    def index():
        accounts = (
            get_db()
            .execute("SELECT * FROM accounts WHERE owner_sub = ? ORDER BY id", (current_user()["sub"],))
            .fetchall()
        )
        return render_template("index.html", accounts=accounts)

    @app.route("/accounts/new", methods=("POST",))
    @role_required("житель")
    def open_account():
        db = get_db()
        if db.execute("SELECT COUNT(*) FROM accounts WHERE owner_sub = ?", (current_user()["sub"],)).fetchone()[0] >= 3:
            flash("Не больше трёх счетов на жителя.")
            return redirect(url_for("index"))
        number = "40817" + "".join(secrets.choice("0123456789") for _ in range(10))
        cur = db.execute(
            "INSERT INTO accounts (number, owner_sub, owner_name, balance) VALUES (?, ?, ?, ?)",
            (number, current_user()["sub"], current_user()["name"], DEMO_BALANCE),
        )
        db.commit()
        log("INFO", "data.change", "success", "открыт счёт", details={"account_id": cur.lastrowid})
        
        publish_event(
            "city.bank.account.opened.v1",
            subject=f"account/{cur.lastrowid}",
            data={"owner": current_user()["sub"], "balance": DEMO_BALANCE}
        )
        
        return redirect(url_for("index"))

    @app.route("/accounts/<int:account_id>")
    @login_required
    def account(account_id: int):
        acc = own_account(account_id)
        history = (
            get_db()
            .execute(
                "SELECT t.*, fa.number AS from_number, ta.number AS to_number FROM transfers t "
                "LEFT JOIN accounts fa ON fa.id = t.from_id JOIN accounts ta ON ta.id = t.to_id "
                "WHERE t.from_id = ? OR t.to_id = ? ORDER BY t.id DESC LIMIT 50",
                (acc["id"], acc["id"]),
            )
            .fetchall()
        )
        return render_template("account.html", acc=acc, history=history)

    @app.route("/transfer", methods=("GET", "POST"))
    @role_required("житель")
    def transfer():
        db = get_db()
        mine = db.execute("SELECT * FROM accounts WHERE owner_sub = ? ORDER BY id", (current_user()["sub"],)).fetchall()
        if request.method == "POST":
            from_id = request.form.get("from_id", "")
            src = own_account(int(from_id)) if from_id.isdigit() else abort(400)
            dst = db.execute(
                "SELECT * FROM accounts WHERE number = ?", (request.form.get("to_number", "").strip(),)
            ).fetchone()
            amount = parse_amount(request.form.get("amount", ""), MAX_DEPOSIT)
            if dst is None or dst["id"] == src["id"]:
                flash("Счёт получателя не найден.")
            elif amount is None:
                flash(f"Сумма — целое число рублей от 1 до {MAX_DEPOSIT}.")
            elif amount > src["balance"]:
                flash("Недостаточно денег на счёте.")
            elif amount > BIG_TRANSFER:
                db.execute(
                    "INSERT INTO transfers (from_id, to_id, amount, status, initiator_sub) VALUES (?, ?, ?, 'pending', ?)",
                    (src["id"], dst["id"], amount, current_user()["sub"]),
                )
                db.commit()
                log(
                    "INFO",
                    "data.change",
                    "success",
                    "крупный перевод ждёт подтверждения",
                    details={"from": src["id"], "to": dst["id"], "amount": amount},
                )
                flash("Перевод больше 50 000 ₽ — он выполнится после подтверждения контролёром.")
                return redirect(url_for("account", account_id=src["id"]))
            else:
                db.execute("BEGIN IMMEDIATE")
                if move_money(db, src["id"], dst["id"], amount):
                    cur = db.execute(
                        "INSERT INTO transfers (from_id, to_id, amount, status, initiator_sub) VALUES (?, ?, ?, 'done', ?)",
                        (src["id"], dst["id"], amount, current_user()["sub"]),
                    )
                    db.commit()
                    log(
                        "INFO",
                        "data.change",
                        "success",
                        "перевод выполнен",
                        details={"transfer_id": cur.lastrowid, "amount": amount},
                    )
                    publish_event(
                        "city.bank.transfer.completed.v1",
                        f"transfer/{cur.lastrowid}",
                        {"transfer_id": cur.lastrowid, "amount": amount, "currency": "RUB"},
                    )
                    flash("Перевод выполнен.")
                    return redirect(url_for("account", account_id=src["id"]))
                db.rollback()
                flash("Недостаточно денег на счёте.")
        return render_template("transfer.html", accounts=mine)

    @app.route("/cashier", methods=("GET", "POST"))
    @role_required("кассир")
    def cashier():
        db = get_db()
        if request.method == "POST":
            dst = db.execute(
                "SELECT * FROM accounts WHERE number = ?", (request.form.get("number", "").strip(),)
            ).fetchone()
            amount = parse_amount(request.form.get("amount", ""), MAX_DEPOSIT)
            if dst is None or amount is None:
                flash("Проверьте номер счёта и сумму.")
            else:
                move_money(db, None, dst["id"], amount)
                db.execute(
                    "INSERT INTO transfers (from_id, to_id, amount, status, initiator_sub) VALUES (NULL, ?, ?, 'done', ?)",
                    (dst["id"], amount, current_user()["sub"]),
                )
                db.commit()
                log(
                    "INFO",
                    "data.change",
                    "success",
                    "пополнение в кассе",
                    details={"account_id": dst["id"], "amount": amount},
                )
                flash("Счёт пополнен.")
        return render_template("cashier.html")

    @app.route("/control")
    @role_required("контролёр_банка")
    def control():
        pending = (
            get_db()
            .execute(
                "SELECT t.*, fa.number AS from_number, ta.number AS to_number FROM transfers t "
                "JOIN accounts fa ON fa.id = t.from_id JOIN accounts ta ON ta.id = t.to_id "
                "WHERE t.status = 'pending' ORDER BY t.id"
            )
            .fetchall()
        )
        return render_template("control.html", pending=pending)

    @app.route("/control/<int:transfer_id>/<action>", methods=("POST",))
    @role_required("контролёр_банка")
    def decide(transfer_id: int, action: str):
        if action not in ("approve", "reject"):
            abort(404)
        db = get_db()
        t = db.execute("SELECT * FROM transfers WHERE id = ? AND status = 'pending'", (transfer_id,)).fetchone()
        if t is None:
            abort(404)
        if t["initiator_sub"] == current_user()["sub"]:
            log(
                "WARN",
                "access.denied",
                "failure",
                "попытка подтвердить свой перевод",
                details={"transfer_id": transfer_id},
            )
            abort(403)
        db.execute("BEGIN IMMEDIATE")
        if action == "approve" and move_money(db, t["from_id"], t["to_id"], t["amount"]):
            db.execute(
                "UPDATE transfers SET status = 'done', approver_sub = ? WHERE id = ?",
                (current_user()["sub"], transfer_id),
            )
            db.commit()
            log("INFO", "data.change", "success", "крупный перевод подтверждён", details={"transfer_id": transfer_id})
            publish_event(
                "city.bank.transfer.completed.v1",
                f"transfer/{transfer_id}",
                {"transfer_id": transfer_id, "amount": t["amount"], "currency": "RUB"},
            )
        else:
            db.execute(
                "UPDATE transfers SET status = 'rejected', approver_sub = ? WHERE id = ?",
                (current_user()["sub"], transfer_id),
            )
            db.commit()
            log("INFO", "data.change", "success", "крупный перевод отклонён", details={"transfer_id": transfer_id})
        return redirect(url_for("control"))

    @app.route("/history")
    @login_required
    def history():
        db = get_db()
        transfers = db.execute(
            "SELECT t.*, fa.number AS from_number, ta.number AS to_number FROM transfers t "
            "LEFT JOIN accounts fa ON fa.id = t.from_id JOIN accounts ta ON ta.id = t.to_id "
            "ORDER BY t.id DESC LIMIT 100"
        ).fetchall()
        return render_template("history.html", transfers=transfers)

    @app.route("/cashier/accounts")
    @role_required("кассир")
    def cashier_accounts():
        accounts = get_db().execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return render_template("cashier_accounts.html", accounts=accounts)

    def on_foreign_event(event):
        try:
            log("INFO", "foreign.event.handled", "success", event.get("type", "unknown"),
                details={"from": event.get("district", "unknown")})
        except Exception as e:
            log("ERROR", "foreign.event.failed", "error", str(e),
                details={"event_id": event.get("id", "?")})

    app.extensions["city"]["bus"].subscribe("city.cityhall.request.created.v1", on_foreign_event)

    return app
