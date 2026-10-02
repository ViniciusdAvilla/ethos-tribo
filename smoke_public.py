"""Verify the PUBLIC deployment using disposable synthetic accounts and registrations."""
import os, secrets, sys
from urllib.parse import urlsplit, urlunsplit
import httpx
import psycopg
from bs4 import BeautifulSoup

password = os.environ.get("TEST_ADMIN_PASSWORD")
if not password:
    print("Live smoke credential absent; PostgreSQL integration suite remains active.")
    sys.exit(0)

base = os.environ["BASE_URL"]
parts = urlsplit(os.environ["DATABASE_URL"])
direct = urlunsplit((parts.scheme, parts.netloc.replace("-pooler.", "."), parts.path, parts.query, ""))
marker = "Teste tecnico " + secrets.token_hex(6)
email = "ethos-smoke-" + secrets.token_hex(10) + "@example.invalid"
member_password = secrets.token_urlsafe(24)
phone = "419" + str(secrets.randbelow(100000000)).zfill(8)

def csrf(response):
    tag = BeautifulSoup(response.text, "html.parser").select_one('input[name="csrf"]')
    assert tag, "CSRF token not found"
    return tag["value"]

reg_id = member_id = None
with httpx.Client(base_url=base, timeout=90, follow_redirects=False) as admin, httpx.Client(base_url=base, timeout=90, follow_redirects=False) as member:
    try:
        assert admin.get("/health").status_code == 200
        home = admin.get("/")
        assert home.status_code == 200 and "Próximos encontros" in home.text
        unauth = member.get("/admin")
        assert unauth.status_code == 303 and unauth.headers["location"] == "/admin/entrar"

        login = admin.get("/admin/entrar")
        cookie = login.headers.get("set-cookie", "").lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        result = admin.post("/admin/entrar", data={"csrf": csrf(login), "email": os.environ["ADMIN_EMAIL"], "password": password})
        assert result.status_code == 303
        dashboard = admin.get("/admin")
        assert dashboard.status_code == 200 and "Seus encontros" in dashboard.text
        print("PUBLIC PASS: HTTPS, secure cookies, admin login, protected dashboard")

        page = member.get("/conta/cadastrar")
        made = member.post("/conta/cadastrar", data={"csrf": csrf(page), "full_name": marker, "email": email, "password": member_password, "consent": "yes"})
        assert made.status_code == 303
        account = member.get("/conta")
        assert account.status_code == 200
        assert member.get("/api/sessao").json() == {"autenticado": True}
        out = member.post("/conta/sair", data={"csrf": csrf(account)})
        assert out.status_code == 303
        page = member.get("/conta/entrar")
        entered = member.post("/conta/entrar", data={"csrf": csrf(page), "email": email, "password": member_password})
        assert entered.status_code == 303
        assert member.get("/conta").status_code == 200
        print("PUBLIC PASS: member signup, logout and login")

        event = member.get("/evento/volei-ethos-4-edicao")
        result = member.post("/evento/volei-ethos-4-edicao/inscrever", data={"csrf": csrf(event), "full_name": marker, "phone": phone, "email": email, "consent": "yes", "show_name": ""})
        assert result.status_code == 303
        registration_url = result.headers["location"]
        status = member.get(registration_url)
        assert status.status_code == 200
        with psycopg.connect(direct) as db:
            reg = db.execute("SELECT id, event_id FROM registrations WHERE email=%s AND full_name=%s", (email, marker)).fetchone()
            mem = db.execute("SELECT id FROM members WHERE email=%s AND full_name=%s", (email, marker)).fetchone()
            assert reg and mem, "Records missing from persistent PostgreSQL"
            reg_id, event_id = reg
            member_id = mem[0]
        detail = admin.get("/admin/eventos/" + str(event_id))
        changed = admin.post("/admin/inscricoes/" + str(reg_id) + "/atualizar", data={"csrf": csrf(detail), "payment_status": "paid", "attended": "yes"})
        assert changed.status_code == 303
        with psycopg.connect(direct) as db:
            record = db.execute("SELECT payment_status, attended FROM registrations WHERE id=%s", (reg_id,)).fetchone()
            assert record == ("paid", True)
        exported = admin.get("/admin/eventos/" + str(event_id) + "/exportar.csv")
        assert exported.status_code == 200 and marker in exported.text
        cancelled = member.post(registration_url + "/cancelar", data={"csrf": csrf(status)})
        assert cancelled.status_code == 303
        print("PUBLIC PASS: event signup, persistent PostgreSQL records, payment/presence update, CSV export, cancellation")
    finally:
        # Delete only the disposable rows created by THIS test invocation.
        with psycopg.connect(direct) as db:
            db.execute("DELETE FROM registrations WHERE email=%s AND full_name=%s", (email, marker))
            db.execute("DELETE FROM members WHERE email=%s AND full_name=%s", (email, marker))
        print("PUBLIC CLEANUP: synthetic test accounts and registrations removed")
