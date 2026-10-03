"""Ethos Eventos: public signup and a restricted organizer dashboard."""
from __future__ import annotations
import csv
from decimal import Decimal, InvalidOperation
import io
import re
import time
from datetime import date, time as clocktime
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import read_settings
from .database import Base, engine, get_db, SessionLocal
from .models import Admin, Event, Registration, Member, EventAdmin, utc_now
from .security import check_password, password_hash, csrf_token, verify_csrf, hash_token
from .services import cancel_registration, clean_phone, money, occupancy, register, slugify, whatsapp_message
from .original_integration import SITE as ORIGINAL_SITE, make_original_html, upcoming_events

ROOT = Path(__file__).resolve().parent
settings = read_settings()
@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.database_url.startswith("sqlite"):
        path = settings.database_url.split("///", 1)[-1]
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    from .migrations import upgrade
    upgrade()
    yield


app = FastAPI(title="Ethos Eventos", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)
if settings.environment == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
        __import__("urllib.parse", fromlist=["urlparse"]).urlparse(settings.base_url).hostname, "localhost", "127.0.0.1"
    ])
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12,
                   https_only=settings.secure_cookies, same_site="lax", session_cookie="ethos_session")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
# The archived site is served with its original images and stylesheet paths.
app.mount("/_next", StaticFiles(directory=ORIGINAL_SITE / "_next"), name="original_next")
app.mount("/images", StaticFiles(directory=ORIGINAL_SITE / "images"), name="original_images")
app.mount("/integration", StaticFiles(directory=ROOT / "static"), name="integration")
templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.filters["money"] = money
templates.env.filters["from_json"] = __import__("json").loads
templates.env.filters["date_br"] = lambda value: value.strftime("%d/%m/%Y")
templates.env.filters["time_br"] = lambda value: value.strftime("%H:%M")

LOGIN_FAILURES: dict[str, list[float]] = {}
SIGNUPS: dict[str, list[float]] = {}
MEMBER_AUTH_ATTEMPTS: dict[str, list[float]] = {}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path in ("/", "/index.html", "/api/sessao") or request.url.path.startswith(("/admin", "/inscricao/", "/conta", "/evento/")):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if request.url.path in ("/", "/index.html", "/sobre.html", "/grupos.html", "/manifesto.html", "/termos.html", "/privacidade.html"):
        # The original snapshot uses inline style attributes throughout; legacy
        # inline scripts remain blocked to prevent stale Next.js hydration.
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; base-uri 'self'; frame-ancestors 'none'"
    else:
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; base-uri 'self'; frame-ancestors 'none'"
    if settings.environment == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def render(request: Request, template: str, status_code: int = 200, **kwargs):
    return templates.TemplateResponse(request, template, {
        "csrf": csrf_token(request), "admin_logged_in": bool(request.session.get("admin_id")),
        "member_logged_in": bool(request.session.get("member_id")),
        "administrator": getattr(request.state, "admin", None),
        "today": date.today(), "base_url": settings.base_url, **kwargs,
    }, status_code=status_code)


def rate_limit(key: str, bucket: dict[str, list[float]], count: int, seconds: int):
    now = time.monotonic()
    recent = [timestamp for timestamp in bucket.get(key, []) if now - timestamp < seconds]
    if len(recent) >= count:
        raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos.")
    recent.append(now)
    bucket[key] = recent


def current_admin(request: Request, db: Session = Depends(get_db)) -> Admin:
    admin_id = request.session.get("admin_id")
    admin = db.get(Admin, admin_id) if isinstance(admin_id, int) else None
    if not admin:
        raise HTTPException(303, headers={"Location": "/admin/entrar"})
    request.state.admin = admin
    params = request.path_params
    if not admin.is_superadmin:
        if request.url.path == "/admin/eventos/novo":
            raise HTTPException(403, "Somente o administrador geral pode criar atividades e definir responsáveis.")
        event_id = params.get("event_id")
        registration_id = params.get("reg_id")
        if registration_id is not None:
            registration = db.get(Registration, int(registration_id))
            if not registration:
                raise HTTPException(404)
            event_id = registration.event_id
        if event_id is not None and not db.get(EventAdmin, (int(event_id), admin.id)):
            raise HTTPException(403, "Você não está designado para esta atividade.")
    return admin


def find_published(db: Session, slug: str) -> Event:
    event = db.scalar(select(Event).where(Event.slug == slug, Event.is_published.is_(True)))
    if not event:
        raise HTTPException(404, "Evento não encontrado.")
    return event


def counts(db: Session, events: list[Event]) -> dict[int, dict[str, int]]:
    return {event.id: dict(zip(("confirmed", "waitlist"), occupancy(db, event.id))) for event in events}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    """Original Ethos Tribo homepage with a live agenda inserted before About."""
    member = active_member(request, db)
    return HTMLResponse(make_original_html(db, is_member=member is not None))


@app.get("/index.html")
def original_index_redirect():
    return RedirectResponse("/", status_code=302)


@app.get("/eventos")
def public_agenda_redirect():
    return RedirectResponse("/#proximos-encontros", status_code=302)


@app.get("/iconc399.svg")
def official_icon(db: Session = Depends(get_db)):
    from .media import catalog
    from .models import SiteImage
    key = next((key for key, item in catalog().items() if item["canonical"] == "/iconc399.svg"), None)
    image = db.get(SiteImage, key) if key else None
    if image:
        return Response(image.data, media_type=image.mime)
    return FileResponse(ORIGINAL_SITE / "iconc399.svg", media_type="image/svg+xml")


@app.get("/api/sessao")
def member_session_status(request: Request, db: Session = Depends(get_db)):
    # Cookie-based authentication; this JSON contains no private account data.
    return {"autenticado": active_member(request, db) is not None}


@app.get("/entrar.html")
def archive_login_redirect():
    # Archived Next.js login is not a working authentication backend.
    return RedirectResponse("/conta/entrar", status_code=302)


@app.get("/{page_name}.html", response_class=HTMLResponse)
def original_pages(page_name: str, request: Request, db: Session = Depends(get_db)):
    if page_name == "agenda":
        return RedirectResponse("/#proximos-encontros", status_code=302)
    if page_name not in {"sobre", "grupos", "manifesto", "termos", "privacidade"}:
        raise HTTPException(404)
    return HTMLResponse(make_original_html(db, is_member=active_member(request, db) is not None, page=page_name+".html"))


# Internal URLs from archived pages keep working without the old Next.js backend.
@app.get("/entrar")
def legacy_login_redirect():
    return RedirectResponse("/conta/entrar", status_code=302)


@app.get("/agenda")
def legacy_agenda_redirect():
    return RedirectResponse("/#proximos-encontros", status_code=302)


@app.get("/sobre")
def legacy_about_redirect():
    return RedirectResponse("/sobre.html", status_code=302)


@app.get("/grupos")
def legacy_groups_redirect():
    return RedirectResponse("/grupos.html", status_code=302)


@app.get("/manifesto")
def legacy_manifesto_redirect():
    return RedirectResponse("/manifesto.html", status_code=302)


@app.get("/termos")
def legacy_terms_redirect():
    return RedirectResponse("/termos.html", status_code=302)


@app.get("/encontro/volei-hvzxa8")
def legacy_volleyball_redirect():
    return RedirectResponse("/evento/volei-ethos-4-edicao", status_code=302)


def active_member(request: Request, db: Session) -> Member | None:
    member_id = request.session.get("member_id")
    if not isinstance(member_id, int):
        return None
    return db.get(Member, member_id)


@app.get("/conta/entrar", response_class=HTMLResponse)
def member_login_page(request: Request, db: Session = Depends(get_db)):
    if active_member(request, db):
        return RedirectResponse("/conta", status_code=303)
    return render(request, "member_login.html", title="Entrar na Ethos Tribo")


@app.post("/conta/entrar", response_class=HTMLResponse)
def member_login(request: Request, email: str = Form(...), password: str = Form(...),
                 csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    rate_limit(f"member-login:{ip}", MEMBER_AUTH_ATTEMPTS, 10, 900)
    email = email.strip().lower()
    member = db.scalar(select(Member).where(Member.email == email))
    if not member or not check_password(password, member.password_hash):
        return render(request, "member_login.html", title="Entrar na Ethos Tribo",
                      status_code=401, error="E-mail ou senha incorretos.", email=email)
    request.session.clear()
    request.session["member_id"] = member.id
    csrf_token(request)
    MEMBER_AUTH_ATTEMPTS.pop(f"member-login:{ip}", None)
    return RedirectResponse("/conta", status_code=303)


@app.get("/conta/cadastrar", response_class=HTMLResponse)
def member_registration_page(request: Request, db: Session = Depends(get_db)):
    if active_member(request, db):
        return RedirectResponse("/conta", status_code=303)
    return render(request, "member_register.html", title="Criar conta")


@app.post("/conta/cadastrar", response_class=HTMLResponse)
def create_member(request: Request, full_name: str = Form(...), email: str = Form(...),
                  password: str = Form(...), consent: str = Form(""),
                  csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    rate_limit(f"member-create:{ip}", MEMBER_AUTH_ATTEMPTS, 5, 3600)
    full_name = " ".join(full_name.split())
    email = email.strip().lower()
    error = None
    if not (3 <= len(full_name) <= 140) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        error = "Informe um nome e e-mail válidos."
    elif not 12 <= len(password) <= 128:
        error = "A senha precisa ter entre 12 e 128 caracteres."
    elif consent != "yes":
        error = "Leia e aceite a política de privacidade para criar sua conta."
    elif db.scalar(select(Member).where(Member.email == email)):
        error = "Este e-mail já está cadastrado. Entre com sua senha."
    if error:
        return render(request, "member_register.html", title="Criar conta", error=error,
                      full_name=full_name, email=email, status_code=400)
    member = Member(full_name=full_name, email=email, password_hash=password_hash(password))
    db.add(member)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return render(request, "member_register.html", title="Criar conta", status_code=400,
                      error="Não foi possível criar essa conta. Confirme os dados ou tente entrar.")
    request.session.clear()
    request.session["member_id"] = member.id
    csrf_token(request)
    return RedirectResponse("/conta", status_code=303)


@app.get("/conta", response_class=HTMLResponse)
def member_account(request: Request, db: Session = Depends(get_db)):
    member = active_member(request, db)
    if member is None:
        return RedirectResponse("/conta/entrar", status_code=303)
    return render(request, "member_account.html", title="Minha conta", member=member, registrations=db.scalars(select(Registration).where(Registration.member_id == member.id).order_by(Registration.created_at.desc())).all())


@app.post("/conta/sair")
def member_logout(request: Request, csrf: str = Form("")):
    verify_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/evento/{slug}", response_class=HTMLResponse)
def event_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    event = find_published(db, slug)
    confirmed, waiting = occupancy(db, event.id)
    opted_in = db.scalars(select(Registration).where(
        Registration.event_id == event.id, Registration.status == "confirmed", Registration.show_name_publicly.is_(True)
    ).order_by(Registration.created_at, Registration.id)).all()
    return render(request, "event.html", title=event.title, event=event,
                  member_logged_in=active_member(request, db) is not None, confirmed=confirmed,
                  waiting=waiting, public_names=[r.full_name.split()[0] for r in opted_in] if event.mode == "list" else [],
                  shared_link=f"{settings.base_url}/evento/{event.slug}")


@app.post("/evento/{slug}/inscrever", response_class=HTMLResponse)
def signup(slug: str, request: Request, full_name: str = Form(...), phone: str = Form(...),
           email: str = Form(""), consent: str = Form(""), show_name: str = Form(""),
           csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    event = find_published(db, slug)
    if event.mode != "list":
        raise HTTPException(409, "Participe com sua conta usando o botão do evento.")
    ip = request.client.host if request.client else "unknown"
    rate_limit(f"signup:{ip}", SIGNUPS, 10, 600)
    if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        raise HTTPException(422, "Informe um e-mail válido ou deixe o campo em branco.")
    try:
        registration, token = register(db, event.id, full_name, phone, email, consent == "yes", show_name == "yes")
    except ValueError as error:
        db.rollback()
        confirmed, waiting = occupancy(db, event.id)
        return render(request, "event.html", title=event.title, event=event,
                  member_logged_in=active_member(request, db) is not None, confirmed=confirmed,
                      waiting=waiting, public_names=[], error=str(error), fields={"full_name": full_name, "phone": phone, "email": email}, status_code=400)
    return RedirectResponse(f"/inscricao/{token}", status_code=303)


@app.get("/inscricao/{token}", response_class=HTMLResponse)
def signup_status(token: str, request: Request, db: Session = Depends(get_db)):
    registration = db.scalar(select(Registration).where(Registration.cancellation_token_hash == hash_token(token)))
    if not registration:
        raise HTTPException(404, "Inscrição não encontrada.")
    from .features import tracking_page
    return tracking_page(request, db, registration, token)


@app.post("/inscricao/{token}/cancelar")
def cancel_self(token: str, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration = db.scalar(select(Registration).where(Registration.cancellation_token_hash == hash_token(token)))
    if not registration:
        raise HTTPException(404, "Inscrição não encontrada.")
    cancel_registration(db, registration)
    return RedirectResponse(f"/inscricao/{token}", status_code=303)


@app.get("/privacidade", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html", title="Privacidade")


@app.get("/api/eventos")
def public_events(db: Session = Depends(get_db)):
    events = db.scalars(select(Event).where(Event.is_published.is_(True)).order_by(Event.event_date)).all()
    return [{"id": event.id, "slug": event.slug, "titulo": event.title,
             "data": event.event_date.isoformat(), "inicio": event.starts_at.strftime("%H:%M"),
             "fim": event.ends_at.strftime("%H:%M"), "local": event.location_name,
             "endereco": event.address, "preco_centavos": event.price_cents,
             "capacidade": event.capacity, "inscritos": occupancy(db, event.id)[0],
             "lista_espera": occupancy(db, event.id)[1], "inscricoes_abertas": not event.is_closed}
            for event in events]


@app.get("/admin/entrar", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return render(request, "admin/login.html", title="Entrar no painel")


@app.post("/admin/entrar")
def admin_login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form(""),
                db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    rate_limit(f"login:{ip}", LOGIN_FAILURES, 6, 900)
    admin = db.scalar(select(Admin).where(Admin.email == email.strip().lower()))
    account = db.get(Member, admin.member_id) if admin and admin.member_id else None
    encoded = account.password_hash if account else (admin.password_hash if admin else "")
    if not admin or not check_password(password, encoded):
        return render(request, "admin/login.html", title="Entrar no painel", error="E-mail ou senha incorretos.", status_code=401)
    request.session.clear()
    request.session["admin_id"] = admin.id
    csrf_token(request)
    LOGIN_FAILURES.pop(f"login:{ip}", None)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/sair")
def admin_logout(request: Request, csrf: str = Form(""), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    statement = select(Event).order_by(Event.event_date.desc(), Event.starts_at.desc())
    if not _.is_superadmin:
        statement = statement.join(EventAdmin).where(EventAdmin.admin_id == _.id)
    events = db.scalars(statement).all()
    metrics = []
    for event in events:
        confirmed, waiting = occupancy(db, event.id)
        paid = db.scalar(select(func.count(Registration.id)).where(
            Registration.event_id == event.id, Registration.status == "confirmed", Registration.payment_status == "paid")) or 0
        metrics.append({"event": event, "confirmed": confirmed, "waiting": waiting, "paid": paid,
                        "expected": confirmed * event.price_cents, "received": paid * event.price_cents})
    return render(request, "admin/dashboard.html", title="Painel", metrics=metrics)


def event_form_data(title: str, description: str, event_date: str, starts_at: str, ends_at: str,
                    location_name: str, address: str, court_count: int, capacity: int,
                    price: str, pix_key: str, maps_url: str, is_published: str, is_closed: str):
    if not title.strip() or not location_name.strip() or not address.strip():
        raise ValueError("Título, local e endereço são obrigatórios.")
    d = date.fromisoformat(event_date)
    start = clocktime.fromisoformat(starts_at)
    end = clocktime.fromisoformat(ends_at)
    if start >= end:
        raise ValueError("O horário final deve ser posterior ao inicial.")
    if not 1 <= court_count <= 25 or not 1 <= capacity <= 2000:
        raise ValueError("Informe entre 1–25 quadras e 1–2000 vagas.")
    normalized_price = price.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", normalized_price):
        raise ValueError("Informe o preço em reais, por exemplo 20,00.")
    try:
        cents = int(Decimal(normalized_price) * 100)
    except InvalidOperation as exc:
        raise ValueError("Preço inválido.") from exc
    if cents < 0 or cents > 1_000_000:
        raise ValueError("O preço informado está fora do intervalo permitido.")
    if maps_url and not re.fullmatch(r"https://[^\s]+", maps_url):
        raise ValueError("O link do mapa deve começar com https://.")
    return dict(title=title.strip()[:160], description=description.strip()[:3000], event_date=d,
                starts_at=start, ends_at=end, location_name=location_name.strip()[:150],
                address=address.strip()[:300], court_count=court_count, capacity=capacity,
                price_cents=cents, pix_key=pix_key.strip()[:120], maps_url=maps_url.strip()[:400],
                is_published=is_published == "yes", is_closed=is_closed == "yes")


@app.get("/admin/eventos/novo", response_class=HTMLResponse)
def new_event_page(request: Request, _: Admin = Depends(current_admin)):
    return render(request, "admin/event_form.html", title="Novo evento", event=None)


@app.post("/admin/eventos/novo")
async def new_event(request: Request, title: str = Form(...), description: str = Form(""), event_date: str = Form(...),
              starts_at: str = Form(...), ends_at: str = Form(...), location_name: str = Form(...),
              address: str = Form(...), court_count: int = Form(1), capacity: int = Form(...),
              price: str = Form(...), pix_key: str = Form(""), maps_url: str = Form(""),
              is_published: str = Form(""), is_closed: str = Form(""), csrf: str = Form(""),
              db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    try:
        data = event_form_data(title, description, event_date, starts_at, ends_at, location_name,
                               address, court_count, capacity, price, pix_key, maps_url, is_published, is_closed)
    except ValueError as error:
        return render(request, "admin/event_form.html", title="Novo evento", event=None, error=str(error), status_code=400)
    from .features import event_options
    try:
        data.update(event_options(await request.form()))
    except ValueError as error:
        return render(request, "admin/event_form.html", title="Novo evento", event=None, error=str(error), status_code=400)
    base_slug = slugify(title) or "evento"
    slug, n = base_slug, 2
    while db.scalar(select(Event.id).where(Event.slug == slug)):
        slug, n = f"{base_slug[:96]}-{n}", n + 1
    if data["mode"] == "free":
        data["price_cents"] = 0
    if data["mode"] == "paid" and data["price_cents"] <= 0:
        return render(request, "admin/event_form.html", title="Novo evento", event=None, error="Defina um valor para o evento pago.", status_code=400)
    event = Event(slug=slug, **data)
    db.add(event)
    db.commit()
    return RedirectResponse(f"/admin/eventos/{event.id}", status_code=303)


@app.get("/admin/eventos/{event_id}", response_class=HTMLResponse)
def admin_event(event_id: int, request: Request, status: str = "active", q: str = "",
                db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    statement = select(Registration).where(Registration.event_id == event.id)
    if status in ("confirmed", "waitlist", "cancelled"):
        statement = statement.where(Registration.status == status)
    elif status == "active":
        statement = statement.where(Registration.status != "cancelled")
    if q.strip():
        statement = statement.where(Registration.full_name.ilike(f"%{q.strip()[:80]}%"))
    attendees = db.scalars(statement.order_by(Registration.created_at, Registration.id)).all()
    confirmed, waiting = occupancy(db, event.id)
    paid = db.scalar(select(func.count(Registration.id)).where(
        Registration.event_id == event.id, Registration.status == "confirmed", Registration.payment_status == "paid")) or 0
    present = db.scalar(select(func.count(Registration.id)).where(
        Registration.event_id == event.id, Registration.status == "confirmed", Registration.attended.is_(True))) or 0
    return render(request, "admin/event_detail.html", title=event.title, event=event,
                  attendees=attendees, confirmed=confirmed, waiting=waiting, paid=paid, present=present,
                  selected_status=status, q=q, link=f"{settings.base_url}/evento/{event.slug}")


@app.get("/admin/eventos/{event_id}/editar", response_class=HTMLResponse)
def edit_event_page(event_id: int, request: Request, db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    return render(request, "admin/event_form.html", title="Editar evento", event=event)


@app.post("/admin/eventos/{event_id}/editar")
async def edit_event(event_id: int, request: Request, title: str = Form(...), description: str = Form(""),
               event_date: str = Form(...), starts_at: str = Form(...), ends_at: str = Form(...),
               location_name: str = Form(...), address: str = Form(...), court_count: int = Form(1),
               capacity: int = Form(...), price: str = Form(...), pix_key: str = Form(""),
               maps_url: str = Form(""), is_published: str = Form(""), is_closed: str = Form(""),
               csrf: str = Form(""), db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    try:
        changes = event_form_data(title, description, event_date, starts_at, ends_at, location_name,
                                  address, court_count, capacity, price, pix_key, maps_url, is_published, is_closed)
        from .features import event_options
        changes.update(event_options(await request.form(), event))
        if changes["mode"] == "free":
            changes["price_cents"] = 0
        if changes["mode"] == "paid" and changes["price_cents"] <= 0:
            raise ValueError("Eventos pagos precisam de um valor maior que zero.")
        current, _ = occupancy(db, event_id)
        if changes["capacity"] < current:
            raise ValueError(f"Existem {current} inscrições confirmadas. Não é possível diminuir as vagas sem cancelar participantes.")
    except ValueError as error:
        return render(request, "admin/event_form.html", title="Editar evento", event=event, error=str(error), status_code=400)
    for key, value in changes.items():
        setattr(event, key, value)
    if changes["capacity"] > current:
        from .services import promote_waitlist
        promote_waitlist(db, event.id)
    db.commit()
    return RedirectResponse(f"/admin/eventos/{event.id}", status_code=303)


@app.post("/admin/eventos/{event_id}/inscricoes/manual")
def manual_registration(event_id: int, request: Request, full_name: str = Form(...), phone: str = Form(""),
                        csrf: str = Form(""), db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    name = " ".join(full_name.strip().split())
    if not 3 <= len(name) <= 140:
        raise HTTPException(422, "Informe o nome do participante.")
    phone = clean_phone(phone) if phone.strip() else None
    if phone and db.scalar(select(Registration).where(Registration.event_id == event.id, Registration.phone == phone)):
        raise HTTPException(409, "WhatsApp já cadastrado para este evento.")
    confirmed, _ = occupancy(db, event.id)
    db.add(Registration(event_id=event.id, full_name=name, phone=phone,
                        status="confirmed" if confirmed < event.capacity else "waitlist",
                        show_name_publicly=False, payment_status="pending"))
    db.commit()
    return RedirectResponse(f"/admin/eventos/{event.id}#participantes", status_code=303)


@app.post("/admin/inscricoes/{reg_id}/atualizar")
def update_registration(reg_id: int, request: Request, payment_status: str = Form(...),
                        attended: str = Form(""), csrf: str = Form(""),
                        db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    registration = db.get(Registration, reg_id)
    if not registration:
        raise HTTPException(404)
    if registration.status != "confirmed":
        raise HTTPException(409, "Só é possível editar pagamento e presença de confirmados.")
    if payment_status not in ("pending", "paid", "exempt"):
        raise HTTPException(422, "Status de pagamento inválido.")
    if (registration.payment_id or registration.event.mode == "paid") and payment_status != registration.payment_status:
        raise HTTPException(409, "Pagamentos integrados só podem ser confirmados pelo provedor.")
    if payment_status == "paid" and registration.payment_status != "paid":
        registration.paid_at = utc_now()
        registration.payment_source = "manual"
    elif payment_status != "paid":
        registration.paid_at = None
    registration.payment_status = payment_status
    registration.attended = attended == "yes"
    registration.checked_in_at = (registration.checked_in_at or utc_now()) if registration.attended else None
    db.commit()
    return RedirectResponse(f"/admin/eventos/{registration.event_id}#participantes", status_code=303)


@app.post("/admin/inscricoes/{reg_id}/cancelar")
def admin_cancel_registration(reg_id: int, request: Request, csrf: str = Form(""),
                              db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    registration = db.get(Registration, reg_id)
    if not registration:
        raise HTTPException(404)
    event_id = registration.event_id
    cancel_registration(db, registration)
    return RedirectResponse(f"/admin/eventos/{event_id}#participantes", status_code=303)


@app.get("/admin/inscricoes/{reg_id}/editar", response_class=HTMLResponse)
def edit_registration_page(reg_id: int, request: Request, db: Session = Depends(get_db),
                           _: Admin = Depends(current_admin)):
    reg = db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(404)
    return render(request, "admin/registration_form.html", title="Editar participante", registration=reg)


@app.post("/admin/inscricoes/{reg_id}/editar")
def edit_registration(reg_id: int, request: Request, full_name: str = Form(...), phone: str = Form(""),
                      email: str = Form(""), csrf: str = Form(""), db: Session = Depends(get_db),
                      _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    reg = db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(404)
    name = " ".join(full_name.strip().split())
    if not 3 <= len(name) <= 140:
        return render(request, "admin/registration_form.html", title="Editar participante",
                      registration=reg, error="Informe um nome de 3 a 140 caracteres.", status_code=400)
    try:
        cleaned_phone = clean_phone(phone) if phone.strip() else None
    except ValueError as exc:
        return render(request, "admin/registration_form.html", title="Editar participante",
                      registration=reg, error=str(exc), status_code=400)
    if cleaned_phone and db.scalar(select(Registration.id).where(
            Registration.event_id == reg.event_id, Registration.phone == cleaned_phone,
            Registration.id != reg.id)):
        return render(request, "admin/registration_form.html", title="Editar participante",
                      registration=reg, error="Esse WhatsApp já pertence a outra inscrição no evento.", status_code=409)
    if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        return render(request, "admin/registration_form.html", title="Editar participante",
                      registration=reg, error="E-mail inválido.", status_code=400)
    reg.full_name = name
    reg.phone = cleaned_phone
    reg.email = email.strip() or None
    db.commit()
    return RedirectResponse(f"/admin/eventos/{reg.event_id}#participantes", status_code=303)


@app.post("/admin/inscricoes/{reg_id}/anonimizar")
def anonymize_registration(reg_id: int, request: Request, csrf: str = Form(""),
                           db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    verify_csrf(request, csrf)
    reg = db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(404)
    event_id = reg.event_id
    cancel_registration(db, reg)
    reg.full_name = f"Inscrição anonimizada #{reg.id}"
    reg.phone = None
    reg.email = None
    reg.show_name_publicly = False
    reg.member_id = None
    reg.image_consent_at = None
    reg.image_revoked_at = None
    reg.image_consent_text = ""
    reg.consent_at = None
    reg.cancellation_token_hash = None
    reg.admin_notes = ""
    reg.attended = False
    reg.payment_status = "pending"
    db.commit()
    return RedirectResponse(f"/admin/eventos/{event_id}?status=all#participantes", status_code=303)


@app.get("/admin/eventos/{event_id}/mensagem", response_class=HTMLResponse)
def admin_message(event_id: int, request: Request, db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    registrations = db.scalars(select(Registration).where(Registration.event_id == event.id)
                               .order_by(Registration.created_at, Registration.id)).all()
    message = whatsapp_message(event, registrations)
    return render(request, "admin/message.html", title="Mensagem do evento", event=event,
                  message=message, whatsapp_link="https://api.whatsapp.com/send?text=" + quote(message))


@app.get("/admin/eventos/{event_id}/exportar.csv")
def export_csv(event_id: int, db: Session = Depends(get_db), _: Admin = Depends(current_admin)):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    rows = db.scalars(select(Registration).where(Registration.event_id == event.id).order_by(Registration.created_at, Registration.id)).all()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Nome", "WhatsApp", "E-mail", "Inscrição", "Pagamento", "Presença", "Consentimento"])
    def excel_safe(value: str) -> str:
        # Spreadsheet programs may execute CSV cells starting with formula characters.
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for r in rows:
        writer.writerow([excel_safe(r.full_name), excel_safe(r.phone or ""), excel_safe(r.email or ""), r.status, r.payment_status,
                         "Sim" if r.attended else "Não", "Sim" if r.consent_at else "Cadastro manual"])
    response = Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{event.slug}-inscricoes.csv"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(404)
def not_found(request: Request, exc):
    return render(request, "404.html", title="Página não encontrada", status_code=404)


from .features import router as feature_router
app.include_router(feature_router)
