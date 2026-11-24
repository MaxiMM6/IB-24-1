# HARDEN.md — Укрепление района «Банк»

**Команда:** БИС-24-1 (район «Банк»)
**Дата:** 24.11.2026

## 1. Секреты

**Что было:**
В истории Git были найдены dev-секреты и тестовый пароль:
- `OIDC_CLIENT_SECRET=soc-dev-secret-change-me`
- `NATS_TOKEN=city-dev-nats-token`
- `CITY_TEST_PASSWORD` (по умолчанию `Gorod-2026`)

**Что стало:**
- Все секреты заменены на заглушки (`замените-меня`) в актуальных файлах.
- История Git переписана с помощью `git filter-repo` — старые значения заменены на `REDACTED`.
- Файл `.env` находится в `.gitignore`, в репозитории только `.env.example` с заглушками.

**Проверка:**
```text
$ grep -rn "password\|secret\|token" app/ citykit/ --include="*.py"
app/__init__.py:6:import secrets
app/__init__.py:97:        number = "40817" + "".join(secrets.choice("0123456789") for _ in range(10))
citykit/bus.py:45:    def __init__(self, url: str, token: str | None, name: str) -> None:
citykit/bus.py:46:        self.url, self.token, self.name = url, token, name
citykit/bus.py:67:                    token=self.token or None,
citykit/oidc.py:22:        client_secret=app.config["OIDC_CLIENT_SECRET"],
citykit/oidc.py:103:        token = oauth.city.authorize_access_token()
citykit/oidc.py:107:    info = token.get("userinfo") or {}
citykit/oidc.py:116:    session["id_token"] = token.get("id_token", "")
citykit/oidc.py:126:    id_token = session.get("id_token", "")
citykit/oidc.py:140:    if id_token:
citykit/oidc.py:141:        params["id_token_hint"] = id_token
```
*(В коде только имена переменных и чтение из конфига — реальных секретов нет).*

**Скриншот:** приложен отдельно (чистый прогон `grep`).

## 2. Права

| Что | Было | Стало |
|-----|------|-------|
| Пользователь в контейнере | root | `appuser` (UID 10001, shell `nologin`) |
| Файловая система | read-write | `read_only: true` (в `compose.yaml`) |
| Права на данные | все | минимально необходимые (каталог `/data` принадлежит `appuser`) |

**Проверка:** `docker compose exec web whoami` → `appuser`.

## 3. Порты

| Порт | Где слушает | Наружу? | Обоснование |
|------|-------------|---------|-------------|
| 8080 | gateway (Caddy) | **Да** (только `127.0.0.1`) | Единая точка входа (Шлюз) для всех районов |
| 8000 | web (Банк) | **Нет** | Только внутри сети `city` (доступ через Шлюз) |
| 4222 | transport (NATS) | **Нет** | Только внутри сети `city` (шина событий) |

**Вывод `docker compose ps` (после):**
```text
NAME            IMAGE         COMMAND                  SERVICE   CREATED      STATUS                            PORTS
d4-bank-web-1   d4-bank-web   "gunicorn --workers …"   web       8 days ago   Up Less than a second (healthy)   8000/tcp
```
*(Порт 8000/tcp без стрелки — не опубликован наружу).*

**Вывод `ss -tlnp | grep -E "8080|8000|4222"` (после):**
```text
LISTEN 0      4096       127.0.0.1:8080       0.0.0.0:*
```
*(Наружу слушает только 8080 на localhost — это Шлюз. Порты 8000 и 4222 не слушают на хосте).*

**Скриншоты:** приложены отдельно (`docker compose ps` и `ss -tlnp`).

## 4. Обновления

| Компонент | Было | Стало |
|-----------|------|-------|
| Базовый образ | `python:3.12-slim` | `python:3.12-slim` (актуальный минорный) |
| Flask | `flask>=3.0` | `flask==3.1.3` (зафиксировано) |
| Redis | `redis>=5.0` | `redis==5.0.1` (зафиксировано) |
| Gunicorn | `gunicorn>=22` | `gunicorn==23.0.0` (зафиксировано) |

**Проверка:** `grep "FROM" Dockerfile` → `python:3.12-slim` (не `latest`).
`cat requirements.txt` → версии зафиксированы (`==`).

## 5. Регрессия

После укрепления район работает:
- `docker compose up --build -d` — успешно.
- Функции (открыть счёт, перевод) — работают.
- События (`city.bank.transfer.completed.v1`) — уходят в SOC (проверено `docker compose logs`).
- `pytest` — все тесты зелёные (11 passed).

## 6. Что бы нашёл атакующий до наших правок

До укрепления атакующий (сосед по городу) мог бы:
1. **Найти в истории Git** реальные dev-секреты (`OIDC_CLIENT_SECRET`, `NATS_TOKEN`) и тестовый пароль. С ними он мог бы выдать себя за другой район или подключиться к шине событий.
2. **Запустить процесс от root** и попытаться выйти из контейнера (например, через уязвимость в приложении).
3. **Подключиться к порту 8000 напрямую** (если он был опубликован), минуя Шлюз, и атаковать приложение без ограничений.
4. **Использовать устаревшие зависимости** с известными уязвимостями (если версии не были зафиксированы).

После правок эти векторы закрыты: секреты отозваны, процесс не root, порт 8000 не опубликован, зависимости зафиксированы.

## 7. Ссылка на коммит

https://github.com/MaxiMM6/IB-24-1/commit/dbf9ca14
```
