"""Проверки безопасности Банка. Запуск: pytest"""

import re

from conftest import csrf, login_as

from citykit import get_db


def open_account(client, username):
    login_as(client, username, "житель", sub=f"sub-{username}")
    client.post("/accounts/new", data={"csrf_token": csrf(client)})


def account_of(app, username):
    with app.app_context():
        return dict(get_db().execute("SELECT * FROM accounts WHERE owner_sub = ?", (f"sub-{username}",)).fetchone())


def test_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]


def test_health_is_public(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_other_account_is_404(app, client, bus):
    open_account(client, "ivanov")
    acc = account_of(app, "ivanov")
    login_as(client, "petrova", "житель", sub="sub-petrova")
    assert client.get(f"/accounts/{acc['id']}").status_code == 404
    assert any(p["event"] == "access.denied" for s, p in bus.published if s == "city.logs.bank")


def test_cannot_transfer_from_foreign_account(app, client):
    open_account(client, "ivanov")
    open_account(client, "petrova")
    iv, pe = account_of(app, "ivanov"), account_of(app, "petrova")
    # Петрова подставляет в форму номер счёта Иванова
    token = csrf(client, "/transfer")
    resp = client.post(
        "/transfer", data={"from_id": iv["id"], "to_number": pe["number"], "amount": "100", "csrf_token": token}
    )
    assert resp.status_code == 404
    assert account_of(app, "ivanov")["balance"] == 10_000


def test_cannot_overdraw_or_send_negative(app, client):
    open_account(client, "petrova")
    open_account(client, "ivanov")
    iv, pe = account_of(app, "ivanov"), account_of(app, "petrova")
    for amount in ("20000", "-5", "1e3", "0"):
        token = csrf(client, "/transfer")
        client.post(
            "/transfer", data={"from_id": iv["id"], "to_number": pe["number"], "amount": amount, "csrf_token": token}
        )
    assert account_of(app, "ivanov")["balance"] == 10_000
    assert account_of(app, "petrova")["balance"] == 10_000


def test_transfer_works_and_publishes_event(app, client, bus):
    open_account(client, "petrova")
    open_account(client, "ivanov")
    iv, pe = account_of(app, "ivanov"), account_of(app, "petrova")
    token = csrf(client, "/transfer")
    client.post(
        "/transfer", data={"from_id": iv["id"], "to_number": pe["number"], "amount": "1500", "csrf_token": token}
    )
    assert account_of(app, "ivanov")["balance"] == 8_500
    assert account_of(app, "petrova")["balance"] == 11_500
    events = [p for s, p in bus.published if s == "city.events.bank.transfer.completed.v1"]
    assert events and events[0]["specversion"] == "1.0" and "petrova" not in str(events[0])


def test_post_without_csrf_rejected(app, client):
    open_account(client, "ivanov")
    assert client.post("/accounts/new").status_code == 400


def test_cashier_page_needs_role(client):
    login_as(client, "ivanov", "житель")
    assert client.get("/cashier").status_code == 403


def test_big_transfer_needs_other_controller(app, client):
    open_account(client, "petrova")
    open_account(client, "ivanov")
    iv, pe = account_of(app, "ivanov"), account_of(app, "petrova")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE accounts SET balance = 100000 WHERE id = ?", (iv["id"],))
        db.commit()
    token = csrf(client, "/transfer")
    client.post(
        "/transfer", data={"from_id": iv["id"], "to_number": pe["number"], "amount": "60000", "csrf_token": token}
    )
    assert account_of(app, "ivanov")["balance"] == 100_000  # ещё не списано
    # Иванов, даже с ролью контролёра, не может подтвердить свой перевод
    login_as(client, "ivanov", "житель", "контролёр_банка", sub="sub-ivanov")
    token = csrf(client, "/control")
    assert client.post("/control/1/approve", data={"csrf_token": token}).status_code == 403
    login_as(client, "controller", "контролёр_банка")
    token = csrf(client, "/control")
    client.post("/control/1/approve", data={"csrf_token": token})
    assert account_of(app, "ivanov")["balance"] == 40_000
    assert account_of(app, "petrova")["balance"] == 70_000


def test_html_is_escaped(app, client):
    login_as(client, "x", "житель", sub="sub-x")
    with client.session_transaction() as s:
        s["user"] = {"sub": "sub-x", "username": "x", "name": "<script>alert(1)</script>", "roles": ["житель"]}
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_account_numbers_are_random(app, client):
    open_account(client, "ivanov")
    open_account(client, "petrova")
    a, b = account_of(app, "ivanov")["number"], account_of(app, "petrova")["number"]
    assert re.fullmatch(r"40817\d{10}", a) and abs(int(a) - int(b)) != 1
