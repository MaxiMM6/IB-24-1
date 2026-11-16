"""Район «Городской SOC»: журналы всех районов, правила обнаружения, тревоги.

Откуда данные:
- журналы районов приходят через Транспорт (темы city.logs.<район>);
- события входа в Паспортном столе (Keycloak) SOC забирает сам, раз в 15 секунд (см. passport.py);
- тревоги Энергосети — событие city.grid.alarm.raised.v1.
"""

import json
import os
import sqlite3
import threading

from flask import abort, redirect, render_template, request, url_for

from citykit import create_city_app, get_db, log, make_event, role_required, topic_for, valid_event

from .passport import KeycloakPoller

RULES = {
    "brute_force": "Перебор паролей: 5 и более неудачных входов за 5 минут",
    "access_denied": "Попытки доступа к чужому: 3 и более отказа за 10 минут",
    "grid_alarm": "Тревога Энергосети: недостоверные показания или чужой счётчик",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    ts          TEXT, district TEXT, service TEXT, level TEXT, event TEXT, outcome TEXT,
    actor       TEXT, src_ip TEXT, request_id TEXT, msg TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS logs_event ON logs (event, received_at);
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule       TEXT NOT NULL,
    key        TEXT NOT NULL,             -- кто: actor или IP
    district   TEXT,
    count      INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
"""

NAV = [("/", "Тревоги"), ("/logs", "Журналы")]
FIELDS = ("ts", "district", "service", "level", "event", "outcome", "actor", "src_ip", "request_id", "msg")
KEEP_LOGS = 20_000


def create_app(test_config: dict | None = None):
    app = create_city_app(__name__, "soc", "Городской SOC", NAV, SCHEMA, test_config)
    city = app.extensions["city"]
    lock = threading.Lock()

    def connect():
        return sqlite3.connect(app.config["DATABASE"], timeout=10)

    def raise_alert(con, rule: str, key: str, district: str | None, count: int) -> None:
        # Одна тревога на правило и нарушителя за 10 минут — не засыпаем аналитика дублями.
        dup = con.execute(
            "SELECT 1 FROM alerts WHERE rule = ? AND key = ? AND created_at >= strftime('%Y-%m-%d %H:%M:%f', 'now', '-10 minutes')",
            (rule, key),
        ).fetchone()
        if dup:
            return
        cur = con.execute(
            "INSERT INTO alerts (rule, key, district, count) VALUES (?, ?, ?, ?)", (rule, key, district, count)
        )
        con.commit()
        event = make_event(
            "soc",
            "web",
            "city.soc.incident.opened.v1",
            f"alert/{cur.lastrowid}",
            {"alert_id": cur.lastrowid, "rule": rule, "district": district},
        )
        city["bus"].publish(topic_for(event["type"]), event)

    def apply_rules(con, rec: dict) -> None:
        if rec.get("event") == "auth.login.failure":
            for col in ("actor", "src_ip"):
                key = rec.get(col)
                if not key or key == "anonymous":
                    continue
                n = con.execute(
                    f"SELECT COUNT(*) FROM logs WHERE event = 'auth.login.failure' AND {col} = ? "
                    "AND received_at >= strftime('%Y-%m-%d %H:%M:%f', 'now', '-5 minutes')",
                    (key,),
                ).fetchone()[0]
                if n >= 5:
                    raise_alert(con, "brute_force", key, rec.get("district"), n)
        elif rec.get("event") == "access.denied" and rec.get("actor") not in (None, "anonymous"):
            n = con.execute(
                "SELECT COUNT(*) FROM logs WHERE event = 'access.denied' AND actor = ? "
                "AND received_at >= strftime('%Y-%m-%d %H:%M:%f', 'now', '-10 minutes')",
                (rec["actor"],),
            ).fetchone()[0]
            if n >= 3:
                raise_alert(con, "access_denied", rec["actor"], rec.get("district"), n)

    def store_log(subject: str, rec: dict) -> None:
        """Запись журнала из Транспорта (другой поток)."""
        if not isinstance(rec.get("event"), str):
            return
        row = [str(rec.get(f))[:500] if rec.get(f) is not None else None for f in FIELDS]
        details = json.dumps(rec.get("details"), ensure_ascii=False)[:2000] if rec.get("details") else None
        with lock:
            con = connect()
            try:
                con.execute(
                    f"INSERT INTO logs ({', '.join(FIELDS)}, details) VALUES ({', '.join('?' * (len(FIELDS) + 1))})",
                    (*row, details),
                )
                con.execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (KEEP_LOGS,))
                con.commit()
                apply_rules(con, rec)
            finally:
                con.close()

    def on_grid_alarm(subject: str, event: dict) -> None:
        if not valid_event(event):
            return
        with lock:
            con = connect()
            try:
                raise_alert(con, "grid_alarm", str(event["data"].get("meter", "?"))[:60], "grid", 1)
            finally:
                con.close()

    city["bus"].subscribe("city.logs.>", store_log)
    city["bus"].subscribe("city.events.grid.alarm.>", on_grid_alarm)

    issuer = app.config["OIDC_ISSUER"]
    if issuer and not app.config.get("TESTING"):
        KeycloakPoller(
            issuer,
            app.config["OIDC_CLIENT_ID"],
            app.config["OIDC_CLIENT_SECRET"],
            on_record=lambda rec: city["bus"].publish("city.logs.passport", rec),
            interval=int(os.environ.get("PASSPORT_POLL", "15")),
        ).start()
    app.extensions["soc_store_log"] = store_log  # для тестов

    @app.route("/")
    @role_required("аналитик_soc")
    def alerts():
        rows = get_db().execute("SELECT * FROM alerts ORDER BY status DESC, id DESC LIMIT 200").fetchall()
        return render_template("alerts.html", alerts=rows, rules=RULES)

    @app.route("/alerts/<int:alert_id>/close", methods=("POST",))
    @role_required("аналитик_soc")
    def close_alert(alert_id: int):
        db = get_db()
        if db.execute("UPDATE alerts SET status = 'closed' WHERE id = ?", (alert_id,)).rowcount != 1:
            abort(404)
        db.commit()
        log("INFO", "data.change", "success", "тревога закрыта", details={"alert_id": alert_id})
        return redirect(url_for("alerts"))

    @app.route("/logs")
    @role_required("аналитик_soc")
    def logs():
        where, args = [], []
        for col in ("district", "event", "level", "actor"):
            val = request.args.get(col, "").strip()
            if val:
                where.append(f"{col} = ?")  # имя колонки — из списка выше, значение — параметром
                args.append(val[:100])
        sql = "SELECT * FROM logs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 300"
        rows = get_db().execute(sql, args).fetchall()
        districts = [r[0] for r in get_db().execute("SELECT DISTINCT district FROM logs ORDER BY district")]
        return render_template("logs.html", rows=rows, districts=districts, args=request.args)

    return app
