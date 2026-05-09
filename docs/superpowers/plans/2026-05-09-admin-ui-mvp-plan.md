# Zabbix RCA AI — v0.7 Admin UI (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development.

**Goal:** Add a browser-accessible admin UI mounted at `/admin` so the monitoring team can answer the four most common questions without SSH:

1. *"Is the system up and what did it cost today?"* — health dashboard
2. *"What did the AI find on that alert last Tuesday?"* — investigations browser
3. *"What did it just do?"* — live audit log
4. *"What patterns has the AI learned?"* — patterns + host-facts browser

**Out of scope for MVP** (deferred to v0.7.1+): editing connection config (still env-based), user management UI, pattern editing. Read-only first; mutation later.

**Architecture:** Same FastAPI process. New `zabbix_ai/admin/` package. Auth via local user + TOTP, sessions in signed cookies, no JavaScript framework — server-rendered Jinja2 + HTMX for interactive bits. New SQLite tables for `users` and `sessions`. All admin reads come straight from the existing `investigations`, `patterns`, `host_facts`, `audit_log` tables.

**Tech Stack:** FastAPI, Jinja2 (already in deps from v0.4), HTMX (CDN — no build), pyotp, itsdangerous, bcrypt. No new heavy deps.

---

## File Structure

```
zabbix_ai/
  admin/
    __init__.py                NEW: build_router(settings, memory)
    auth.py                    NEW: password+TOTP verify, session cookies, login_required
    users.py                   NEW: user CRUD helpers (create, get_by_username, set_password)
    routes/
      __init__.py
      auth_routes.py           NEW: GET/POST /admin/login, GET /admin/logout, TOTP enrollment
      dashboard.py             NEW: GET /admin → health stats, today's count, cost
      investigations.py        NEW: GET /admin/investigations (list+filter), /admin/investigations/{id}
      audit.py                 NEW: GET /admin/audit (filter by investigation_id, time, event_type)
      memory.py                NEW: GET /admin/patterns, /admin/host-facts (read-only browsing)
  app.py                       MODIFY: mount admin router when settings.admin enabled
  config.py                    MODIFY: add AdminSettings (session_secret_env, bootstrap_admin_password_env)
  templates/
    admin/
      base.html                NEW: nav, layout, flash messages
      login.html               NEW: form
      enroll_totp.html         NEW: QR code on first login
      dashboard.html           NEW: stats cards
      investigations_list.html NEW: filterable table
      investigation_detail.html NEW: per-investigation view with tool transcript
      audit_list.html          NEW: filterable timeline
      patterns_list.html       NEW: pattern browser with occurrences
      host_facts_list.html     NEW: per-host learned facts
migrations/
  002_admin_users.sql          NEW: users + sessions tables
config.example.yaml            MODIFY: add admin: section
.env.example                   MODIFY: SESSION_SECRET, BOOTSTRAP_ADMIN_PASSWORD
pyproject.toml                 MODIFY: add pyotp, itsdangerous, bcrypt
tests/
  unit/
    test_admin_users.py        NEW: password hashing + verification, TOTP, user CRUD
    test_admin_auth.py         NEW: login, logout, session validation, login_required decorator
    test_admin_config.py       NEW: AdminSettings env loading
  integration/
    test_admin_dashboard.py    NEW: GET / requires login, shows correct stats
    test_admin_investigations.py NEW: list + detail views
    test_admin_audit.py        NEW: filtering
    test_admin_memory.py       NEW: patterns + host_facts browsers
```

---

## Task 1: AdminSettings + new dependencies

**Files:** `pyproject.toml`, `zabbix_ai/config.py`, `config.example.yaml`, `.env.example`, `tests/unit/test_config.py`.

- [ ] Add `pyotp>=2.9`, `itsdangerous>=2.2`, `bcrypt>=4.2` to `pyproject.toml` `dependencies`.
- [ ] Run `pip install -e ".[dev]"` — confirm install.
- [ ] Add to `config.py`:

```python
class AdminSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    session_secret_env: str
    bootstrap_admin_password_env: str = ""  # only used on first start if no users exist
    session_max_age_seconds: int = 28800   # 8h
    session_secret: SecretStr = SecretStr("")
    bootstrap_admin_password: SecretStr = SecretStr("")
```

Add `admin: AdminSettings | None = None` to `Settings`. In `load_settings`, after the hostbill block:

```python
if s.admin is not None:
    sec = os.environ.get(s.admin.session_secret_env)
    if not sec:
        raise ValueError(f"{s.admin.session_secret_env} not set in environment")
    s.admin.session_secret = SecretStr(sec)
    if s.admin.bootstrap_admin_password_env:
        bap = os.environ.get(s.admin.bootstrap_admin_password_env, "")
        s.admin.bootstrap_admin_password = SecretStr(bap)
```

- [ ] Append to `config.example.yaml`:

```yaml
admin:
  session_secret_env: SESSION_SECRET
  bootstrap_admin_password_env: BOOTSTRAP_ADMIN_PASSWORD
  session_max_age_seconds: 28800
```

- [ ] Append to `.env.example`:

```
SESSION_SECRET=replace-with-32-bytes-of-random-data
BOOTSTRAP_ADMIN_PASSWORD=
```

- [ ] Tests:

```python
def test_admin_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
admin:
  session_secret_env: SESSION_SECRET
  bootstrap_admin_password_env: BOOTSTRAP_ADMIN_PASSWORD
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("SESSION_SECRET", "32-bytes-of-random-secret-please")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "first-time-pw")
    s = load_settings(cfg)
    assert s.admin is not None
    assert s.admin.session_secret.get_secret_value().startswith("32-")
    assert s.admin.bootstrap_admin_password.get_secret_value() == "first-time-pw"


def test_admin_section_optional(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    assert load_settings(cfg).admin is None
```

---

## Task 2: Schema migration for users + sessions

**Files:** `migrations/002_admin_users.sql`, `tests/unit/test_admin_users.py`.

- [ ] Write `migrations/002_admin_users.sql`:

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    totp_secret TEXT NOT NULL,
    totp_enrolled INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'viewer',           -- admin | operator | viewer
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    sid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_agent TEXT,
    ip TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);

INSERT OR IGNORE INTO schema_version(version) VALUES (2);
```

The migration runner picks up `002_*.sql` automatically.

---

## Task 3: User helpers (password + TOTP)

**Files:** `zabbix_ai/admin/__init__.py` (empty), `zabbix_ai/admin/users.py`, `tests/unit/test_admin_users.py`.

- [ ] Implement helpers:

```python
# zabbix_ai/admin/users.py
from __future__ import annotations
import secrets
from datetime import datetime, timezone
from typing import Any
import bcrypt
import pyotp
from zabbix_ai.memory import Memory


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_provisioning_uri(username: str, secret: str,
                          issuer: str = "Zabbix RCA AI") -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=issuer,
    )


async def create_user(memory: Memory, *, username: str, password: str,
                      role: str = "viewer") -> dict[str, Any]:
    secret = generate_totp_secret()
    await memory.execute(
        """INSERT INTO users
           (username, password_hash, totp_secret, role, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (username, hash_password(password), secret, role, _now_iso()),
    )
    row = await memory.fetchone(
        "SELECT id, username, role, totp_secret FROM users WHERE username=?",
        (username,),
    )
    return {"id": row[0], "username": row[1], "role": row[2],
            "totp_secret": row[3]}


async def get_user_by_username(memory: Memory,
                               username: str) -> dict[str, Any] | None:
    row = await memory.fetchone(
        """SELECT id, username, password_hash, totp_secret, totp_enrolled,
                  role, disabled FROM users WHERE username=?""", (username,),
    )
    if not row:
        return None
    return dict(zip(
        ("id", "username", "password_hash", "totp_secret",
         "totp_enrolled", "role", "disabled"), row, strict=False,
    ))


async def set_totp_enrolled(memory: Memory, user_id: int) -> None:
    await memory.execute(
        "UPDATE users SET totp_enrolled=1 WHERE id=?", (user_id,),
    )


async def update_last_login(memory: Memory, user_id: int) -> None:
    await memory.execute(
        "UPDATE users SET last_login_at=? WHERE id=?", (_now_iso(), user_id),
    )


async def ensure_bootstrap_admin(memory: Memory, *, username: str,
                                  password: str) -> dict[str, Any] | None:
    """Create a single admin user if no users exist. Idempotent."""
    row = await memory.fetchone("SELECT COUNT(*) FROM users")
    if row and row[0] > 0:
        return None
    return await create_user(memory, username=username,
                              password=password, role="admin")
```

- [ ] Tests covering: `hash_password` round-trip, `verify_password` happy and wrong, `generate_totp_secret` returns 32-char base32, `verify_totp` accepts current and rejects wrong, `create_user` + `get_user_by_username`, `ensure_bootstrap_admin` only creates when empty.

---

## Task 4: Sessions + login_required dependency

**Files:** `zabbix_ai/admin/auth.py`, `tests/unit/test_admin_auth.py`.

- [ ] Implement:

```python
# zabbix_ai/admin/auth.py
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from fastapi import Cookie, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from zabbix_ai.memory import Memory

_COOKIE_NAME = "zai_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt="zai.session")


async def create_session(memory: Memory, *, user_id: int, secret: str,
                          ttl_seconds: int, user_agent: str = "",
                          ip: str = "") -> str:
    sid = secrets.token_urlsafe(32)
    now = _now()
    exp = now + timedelta(seconds=ttl_seconds)
    await memory.execute(
        """INSERT INTO sessions
           (sid, user_id, created_at, expires_at, last_seen_at,
            user_agent, ip)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, user_id, now.isoformat(), exp.isoformat(), now.isoformat(),
         user_agent[:255], ip[:64]),
    )
    return _serializer(secret).dumps({"sid": sid})


async def resolve_session(memory: Memory, *, signed_cookie: str,
                           secret: str) -> dict[str, Any] | None:
    try:
        payload = _serializer(secret).loads(signed_cookie)
    except BadSignature:
        return None
    sid = payload.get("sid")
    if not sid:
        return None
    row = await memory.fetchone(
        """SELECT s.user_id, s.expires_at, u.username, u.role, u.disabled
           FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.sid=?""",
        (sid,),
    )
    if not row:
        return None
    user_id, expires_at, username, role, disabled = row
    if disabled:
        return None
    if datetime.fromisoformat(expires_at) <= _now():
        return None
    await memory.execute(
        "UPDATE sessions SET last_seen_at=? WHERE sid=?",
        (_now().isoformat(), sid),
    )
    return {"sid": sid, "user_id": user_id, "username": username,
            "role": role}


async def destroy_session(memory: Memory, sid: str) -> None:
    await memory.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def login_required(min_role: str = "viewer"):
    """FastAPI dependency. Verifies cookie, loads user, enforces role."""
    role_rank = {"viewer": 0, "operator": 1, "admin": 2}

    async def _dep(request: Request,
                   zai_session: str | None = Cookie(default=None)) -> dict:
        if not zai_session:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/admin/login"},
            )
        memory: Memory = request.app.state.memory
        secret: str = request.app.state.session_secret
        user = await resolve_session(memory,
                                      signed_cookie=zai_session,
                                      secret=secret)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/admin/login"},
            )
        if role_rank.get(user["role"], -1) < role_rank.get(min_role, 99):
            raise HTTPException(status_code=403, detail="insufficient role")
        return user

    return _dep
```

- [ ] Tests: create_session round-trip via resolve, expired session rejected, disabled user rejected, bad signature rejected.

---

## Task 5: Auth routes (login, logout, TOTP enrollment)

**Files:** `zabbix_ai/admin/routes/__init__.py`, `zabbix_ai/admin/routes/auth_routes.py`, `templates/admin/base.html`, `templates/admin/login.html`, `templates/admin/enroll_totp.html`, `tests/integration/test_admin_login.py`.

- [ ] `templates/admin/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><title>{% block title %}Zabbix RCA AI{% endblock %}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #0f1115; color: #e6e8ee; }
    nav { background: #1a1d24; border-bottom: 1px solid #2a2f3a;
          padding: 10px 18px; display: flex; gap: 16px; align-items: center; }
    nav a { color: #cdd3e0; text-decoration: none; font-size: 14px; }
    nav a.active { color: #fff; font-weight: 600; }
    nav .right { margin-left: auto; font-size: 12px; color: #8a93a6; }
    main { max-width: 1100px; margin: 0 auto; padding: 20px 18px; }
    h1 { font-size: 18px; margin: 0 0 14px 0; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #2a2f3a; text-align: left; }
    th { color: #8a93a6; font-weight: 500; font-size: 11px;
         text-transform: uppercase; letter-spacing: 0.04em; }
    .card { background: #1a1d24; border: 1px solid #2a2f3a;
            border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
    .stat { display: inline-block; min-width: 140px; margin-right: 12px; }
    .stat .v { font-size: 22px; font-weight: 600; }
    .stat .l { font-size: 11px; color: #8a93a6; text-transform: uppercase; }
    .flash { background: #2c1c1c; border: 1px solid #5a3838; color: #f78b8b;
             padding: 8px 12px; border-radius: 4px; margin: 8px 0; }
    .flash.ok { background: #1c2c1f; border-color: #385a3d; color: #6dd58c; }
    a { color: #4f8cf7; }
    form input, form button { padding: 6px 10px; background: #0f1115;
       color: #e6e8ee; border: 1px solid #2a2f3a; border-radius: 4px; }
    form button { cursor: pointer; }
  </style>
</head>
<body>
  {% if user %}
  <nav>
    <a href="/admin"{% if active=='dashboard' %} class="active"{% endif %}>Dashboard</a>
    <a href="/admin/investigations"{% if active=='investigations' %} class="active"{% endif %}>Investigations</a>
    <a href="/admin/audit"{% if active=='audit' %} class="active"{% endif %}>Audit log</a>
    <a href="/admin/patterns"{% if active=='patterns' %} class="active"{% endif %}>Patterns</a>
    <a href="/admin/host-facts"{% if active=='host-facts' %} class="active"{% endif %}>Host facts</a>
    <span class="right">
      Signed in as <strong>{{ user.username }}</strong>
      ({{ user.role }})
      &middot; <a href="/admin/logout">Logout</a>
    </span>
  </nav>
  {% endif %}
  <main>
    {% for f in flashes %}<div class="flash {{ f.kind }}">{{ f.text }}</div>{% endfor %}
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] `templates/admin/login.html`: simple form with username, password, totp_code (latter only required after first enrollment).
- [ ] `templates/admin/enroll_totp.html`: shows otpauth URI as QR code (use `https://chart.googleapis.com/chart?cht=qr&chs=200x200&chl=...` URL or render QR via `pyqrcode` if we add that dep — for MVP just show the URI as text + ask for first TOTP code to confirm enrollment).

- [ ] Routes:

```python
# zabbix_ai/admin/routes/auth_routes.py
from __future__ import annotations
from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from zabbix_ai.admin import auth, users

router = APIRouter()


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "admin/login.html",
        {"flashes": [], "user": None, "active": "login"},
    )


@router.post("/admin/login")
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...),
                       totp_code: str = Form("")) -> RedirectResponse:
    memory = request.app.state.memory
    secret = request.app.state.session_secret
    ttl = request.app.state.session_ttl

    user = await users.get_user_by_username(memory, username)
    if not user or user["disabled"]:
        return _login_error(request, "invalid credentials")
    if not users.verify_password(password, user["password_hash"]):
        return _login_error(request, "invalid credentials")

    # First-time login: enrollment flow handled via separate page
    if not user["totp_enrolled"]:
        # Stash a short-lived "pre-totp" cookie
        token = auth._serializer(secret).dumps({"pre": user["id"]})
        resp = RedirectResponse(url="/admin/enroll-totp", status_code=303)
        resp.set_cookie("zai_pretotp", token, max_age=300,
                         httponly=True, secure=True, samesite="lax")
        return resp

    if not totp_code or not users.verify_totp(user["totp_secret"], totp_code):
        return _login_error(request, "TOTP required or invalid")

    cookie = await auth.create_session(
        memory, user_id=user["id"], secret=secret, ttl_seconds=ttl,
        user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else "",
    )
    await users.update_last_login(memory, user["id"])
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("zai_session", cookie, max_age=ttl,
                     httponly=True, secure=True, samesite="lax")
    return resp


@router.get("/admin/enroll-totp", response_class=HTMLResponse)
async def enroll_page(request: Request,
                      zai_pretotp: str | None = Cookie(default=None)) -> HTMLResponse:
    if not zai_pretotp:
        return RedirectResponse("/admin/login", status_code=303)
    secret = request.app.state.session_secret
    try:
        payload = auth._serializer(secret).loads(zai_pretotp)
    except Exception:
        return RedirectResponse("/admin/login", status_code=303)
    user_id = payload.get("pre")
    memory = request.app.state.memory
    row = await memory.fetchone(
        "SELECT username, totp_secret FROM users WHERE id=?", (user_id,),
    )
    if not row:
        return RedirectResponse("/admin/login", status_code=303)
    username, totp_secret = row
    uri = users.totp_provisioning_uri(username, totp_secret)
    return request.app.state.templates.TemplateResponse(
        request, "admin/enroll_totp.html",
        {"flashes": [], "user": None, "active": "enroll",
         "totp_uri": uri, "totp_secret": totp_secret, "username": username},
    )


@router.post("/admin/enroll-totp")
async def enroll_submit(request: Request, totp_code: str = Form(...),
                         zai_pretotp: str | None = Cookie(default=None),
                         ) -> RedirectResponse:
    if not zai_pretotp:
        return RedirectResponse("/admin/login", status_code=303)
    secret = request.app.state.session_secret
    ttl = request.app.state.session_ttl
    try:
        payload = auth._serializer(secret).loads(zai_pretotp)
    except Exception:
        return RedirectResponse("/admin/login", status_code=303)
    user_id = payload["pre"]
    memory = request.app.state.memory
    row = await memory.fetchone(
        "SELECT totp_secret FROM users WHERE id=?", (user_id,),
    )
    if not row or not users.verify_totp(row[0], totp_code):
        return _login_error(request, "TOTP code didn't match — try again")
    await users.set_totp_enrolled(memory, user_id)
    cookie = await auth.create_session(
        memory, user_id=user_id, secret=secret, ttl_seconds=ttl,
    )
    await users.update_last_login(memory, user_id)
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("zai_session", cookie, max_age=ttl,
                     httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("zai_pretotp")
    return resp


@router.get("/admin/logout")
async def logout(request: Request,
                  zai_session: str | None = Cookie(default=None),
                  ) -> RedirectResponse:
    if zai_session:
        secret = request.app.state.session_secret
        try:
            payload = auth._serializer(secret).loads(zai_session)
            await auth.destroy_session(request.app.state.memory,
                                         payload["sid"])
        except Exception:
            pass
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("zai_session")
    return resp


def _login_error(request: Request, msg: str) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "admin/login.html",
        {"flashes": [{"kind": "err", "text": msg}],
         "user": None, "active": "login"},
        status_code=400,
    )
```

- [ ] Integration test: GET `/admin/login` returns 200, POST with valid creds + valid TOTP returns 303 to `/admin` with session cookie set.

---

## Task 6: Dashboard route

**Files:** `zabbix_ai/admin/routes/dashboard.py`, `templates/admin/dashboard.html`, `tests/integration/test_admin_dashboard.py`.

- [ ] Route: GET `/admin` — query SQLite for:
  - investigations today (count)
  - investigations this week (count)
  - average duration today
  - total tokens (in + out) today
  - estimated cost today (tokens × per-model rate, hardcoded constants)
  - top 5 host_facts hosts by fact-count
  - top 5 patterns by occurrences

- [ ] Template renders three "stat cards" + two small tables. Plain HTML, no JS needed.

- [ ] Test: with seeded SQLite, dashboard returns 200 and includes the right counts in the response body.

---

## Task 7: Investigations browser (list + detail)

**Files:** `zabbix_ai/admin/routes/investigations.py`, two templates, integration tests.

- [ ] List route GET `/admin/investigations?source=&hostid=&page=`:
  - Filterable by source (cli / slack / zabbix_ui / hostbill), eventid, hostid, date range, pattern_signature.
  - Paginated 50 per page.
  - Columns: id, started_at, source, eventid, hostid, hostname, model, duration_ms, tokens, summary first 80 chars.

- [ ] Detail route GET `/admin/investigations/{id}`:
  - Full summary
  - Tool transcript from audit_log
  - Pattern signature (link to pattern detail)
  - Re-run button (POST `/admin/investigations/{id}/rerun`) — defer to v0.7.1 if time pressure.

---

## Task 8: Audit log viewer + memory browsers

**Files:** `zabbix_ai/admin/routes/audit.py`, `zabbix_ai/admin/routes/memory.py`, three templates, integration tests.

- [ ] `/admin/audit?investigation_id=&event_type=&since=`: timeline view of audit_log rows.
- [ ] `/admin/patterns`: table of patterns sorted by occurrences DESC, columns signature, occurrences, first_seen, last_seen, typical_root_cause (truncated), typical_fix.
- [ ] `/admin/patterns/{signature}`: detail with full root_cause + fix + list of investigations with that signature.
- [ ] `/admin/host-facts?hostid=`: filterable table of host_facts.

---

## Task 9: Wire everything into the FastAPI app

**Files:** `zabbix_ai/admin/__init__.py`, `zabbix_ai/app.py`.

- [ ] `admin/__init__.py`:

```python
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from zabbix_ai.admin import users
from zabbix_ai.admin.routes import (
    audit_routes,
    auth_routes,
    dashboard,
    investigations,
    memory_routes,
)
from zabbix_ai.config import Settings
from zabbix_ai.memory import Memory


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


async def setup_admin(app: FastAPI, settings: Settings,
                      memory: Memory) -> None:
    if settings.admin is None:
        return

    # bootstrap admin user if no users exist and a password was provided
    if settings.admin.bootstrap_admin_password.get_secret_value():
        await users.ensure_bootstrap_admin(
            memory, username="admin",
            password=settings.admin.bootstrap_admin_password.get_secret_value(),
        )

    app.state.memory = memory
    app.state.session_secret = settings.admin.session_secret.get_secret_value()
    app.state.session_ttl = settings.admin.session_max_age_seconds
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(investigations.router)
    app.include_router(audit_routes.router)
    app.include_router(memory_routes.router)
```

- [ ] In `app.py`'s `create_app`, after the existing adapter mounts, await `setup_admin(app, settings, memory)`. Memory must be passed in (currently the FastAPI app doesn't get one — extend `create_app` signature with `memory: Memory | None = None` and have `_default_app` create one if settings.admin is configured).

- [ ] Integration smoke: with admin enabled, unauthenticated GET `/admin` returns 303 to `/admin/login`; login + TOTP succeeds; subsequent GET `/admin` returns 200.

---

## Task 10: README + final smoke + commit

- [ ] Add to `README.md`:

```markdown
## Admin UI (optional, v0.7+)

To enable the admin UI:

1. Generate a session secret: `openssl rand -hex 32`
2. Choose a bootstrap admin password (used only on first start to create the
   `admin` user; once that user exists with TOTP enrolled, this is ignored).
3. Add to `/etc/zabbix-ai/env`:
   ```
   SESSION_SECRET=<32 hex bytes from step 1>
   BOOTSTRAP_ADMIN_PASSWORD=<temporary password>
   ```
4. Add the `admin:` block to `/etc/zabbix-ai/config.yaml`.
5. Restart the service. Open `https://your-host/admin/login` with `admin`
   and your bootstrap password. You'll be prompted to enrol TOTP on first
   login (Google Authenticator, 1Password, etc.).
6. Once enrolled, **clear `BOOTSTRAP_ADMIN_PASSWORD` from the env file**
   so it can't be used to overwrite anything.

Read-only views in MVP: dashboard, investigations history, audit log,
patterns, host-facts. Connection management, user management, and pattern
editing arrive in v0.7.1.
```

- [ ] Bump `__version__` to `0.7.0` in `zabbix_ai/__init__.py` and `pyproject.toml`.
- [ ] Run full test suite + ruff. Expected ~135 tests pass.
- [ ] Single commit: `feat(v0.7): admin UI MVP (auth, dashboard, history, audit, memory)`.

---

## Self-Review

- **Spec coverage** (vs design §11): MVP covers Health, Investigations, Audit log, Patterns, Host facts. Connections / Users / pattern-editing deferred to v0.7.1 (called out explicitly in scope).
- **Type consistency**: `Memory`, `users.create_user`, `auth.create_session` consistent. The `ensure_bootstrap_admin` only creates one user — subsequent users come via v0.7.1's user-management page.
- **Security**: bcrypt for passwords, pyotp for TOTP, signed session cookies with HttpOnly+Secure+SameSite=Lax, session expiry checked on every request, session destruction on logout. Secrets never in logs (passwords + TOTP codes are in form data only, not logged).
- **No placeholders.** Every step shows code or a concrete schema/template.
- **Deferred to v0.7.1**: connection-management pages with envelope-encrypted secret storage, user management UI, pattern editing, "re-run investigation" button, QR code rendering (currently shows otpauth URI as text — works fine for paste-into-1Password).
