"""Activity permissions, persistent photos, account signup and Pix tracking."""
import io
import json
import math
import re
import secrets
from datetime import timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from PIL import Image, ImageDraw, ImageFont
import qrcode
from .database import get_db
from .models import Admin, Event, EventAdmin, Member, Registration, SiteImage, utc_now
from .security import verify_csrf, hash_token
from .services import occupancy, money, lock_event
from . import payments
from .media import catalog, decode_image, encode, transform
from .main import current_admin, active_member, find_published, render, rate_limit, SIGNUPS, settings

router = APIRouter()
IMAGE_CONSENT = "Autorizo gratuitamente a Ethos Tribo a utilizar fotos e vídeos da minha imagem captados nesta atividade em seu site, redes sociais e materiais de divulgação das atividades da comunidade, exclusivamente para fins institucionais e não lucrativos, sem publicidade comercial ou venda da minha imagem. A autorização não tem prazo final predeterminado e permanece válida até eu revogá-la gratuitamente pelo acompanhamento da inscrição. A recusa não impede minha participação. Versão 1 — 03/10/2026."


def event_options(form, event=None):
    mode = str(form.get("mode", event.mode if event else "list"))
    if mode not in ("paid", "free", "list"):
        raise ValueError("Escolha evento pago, gratuito ou por lista.")
    followup = safe_url(str(form.get("followup_url", "")))
    links = []
    lines = str(form.get("extra_links", "")).strip().splitlines()
    if len(lines) > 8:
        raise ValueError("Adicione até 8 opções ao evento.")
    for line in lines:
        if "|" not in line:
            raise ValueError("Use uma opção por linha: Título | https://link")
        label, url = line.split("|", 1)
        if not 1 <= len(label.strip()) <= 80:
            raise ValueError("Cada opção precisa de um título de até 80 caracteres.")
        links.append({"label": label.strip(), "url": safe_url(url.strip(), required=True)})
    return {"mode": mode, "followup_url": followup, "extra_links": json.dumps(links, ensure_ascii=False), "checkin_enabled": form.get("checkin_enabled", "yes" if "mode" not in form else "") == "yes"}


def safe_url(value, required=False):
    value = value.strip()
    parsed = urlsplit(value)
    if value and (len(value) > 400 or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or re.search(r'[\s<>"\\]', value)):
        raise ValueError("Os links devem ser endereços HTTPS válidos, sem credenciais.")
    if required and not value:
        raise ValueError("Informe o link da opção.")
    return value


def require_master(admin):
    if not admin.is_superadmin:
        raise HTTPException(403, "Esta área é exclusiva do administrador geral.")


def owned_registration(request, db, registration_id):
    member = active_member(request, db)
    if not member:
        raise HTTPException(303, headers={"Location": "/conta/entrar"})
    registration = db.get(Registration, registration_id)
    if not registration or registration.member_id != member.id:
        raise HTTPException(404)
    return registration, member


def tracking_page(request, db, registration, token=None):
    return render(request, "tracking.html", title="Acompanhar inscrição", registration=registration, event=registration.event,
                  token=token, tracking_path=f"/inscricao/{token}" if token else f"/conta/inscricoes/{registration.id}",
                  image_consent=IMAGE_CONSENT, links=json.loads(registration.event.extra_links),
                  pix_payload=payments.pix_payload(registration.event, f"ETHOS{registration.id}") if registration.event.mode == "list" else registration.payment_payload,
                  payment_ready=payments.payment_ready())


@router.post("/evento/{slug}/participar")
def join_account(slug: str, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    member = active_member(request, db)
    if not member:
        return RedirectResponse("/conta/entrar", status_code=303)
    event = find_published(db, slug)
    if event.mode == "list":
        raise HTTPException(409, "Este evento utiliza o formulário de lista.")
    rate_limit(f"account-join:{member.id}", SIGNUPS, 15, 600)
    event = lock_event(db, event.id)
    if event.is_closed:
        raise HTTPException(409, "Inscrições encerradas.")
    previous = db.scalar(select(Registration).where(Registration.event_id == event.id, Registration.member_id == member.id))
    if previous:
        if previous.status == "cancelled":
            raise HTTPException(409, "Sua inscrição foi cancelada. Fale com a organização para reativar ou tratar o reembolso.")
        return RedirectResponse(f"/conta/inscricoes/{previous.id}", status_code=303)
    confirmed, _ = occupancy(db, event.id)
    if confirmed >= event.capacity:
        raise HTTPException(409, "As vagas desta atividade estão esgotadas.")
    registration = Registration(event_id=event.id, member_id=member.id, full_name=member.full_name,
                                email=member.email, consent_at=member.privacy_consent_at, status="confirmed",
                                payment_status="exempt" if event.mode == "free" else "pending",
                                payment_reference=secrets.token_hex(20), payment_amount_cents=event.price_cents)
    db.add(registration)
    db.commit()
    if event.mode == "paid" and payments.payment_ready():
        return payment_create(registration.id, request, csrf, db)
    return RedirectResponse(f"/conta/inscricoes/{registration.id}", status_code=303)


@router.get("/conta/inscricoes/{reg_id}")
def account_tracking(reg_id: int, request: Request, db: Session = Depends(get_db)):
    registration, _ = owned_registration(request, db, reg_id)
    return tracking_page(request, db, registration)


@router.post("/conta/inscricoes/{reg_id}/pagar")
def payment_create(reg_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration, member = owned_registration(request, db, reg_id)
    if registration.event.mode != "paid" or registration.status != "confirmed":
        raise HTTPException(409, "Esta inscrição não aceita uma nova cobrança.")
    if not payments.payment_ready():
        raise HTTPException(503, "O Pix automático aguarda a conexão da conta de pagamentos da organização. Nenhuma cobrança foi gerada.")
    db.execute(select(Registration).where(Registration.id == reg_id).with_for_update()).scalar_one()
    db.refresh(registration)
    if registration.payment_status == "paid":
        return RedirectResponse(f"/conta/inscricoes/{reg_id}", status_code=303)
    if registration.payment_id:
        order = payments.provider_request("GET", f"/v1/orders/{registration.payment_id}")
    else:
        order = payments.create_order(registration, member, registration.event)
    payments.apply_order(registration, order)
    db.commit()
    return RedirectResponse(f"/conta/inscricoes/{reg_id}", status_code=303)


@router.post("/conta/inscricoes/{reg_id}/verificar")
def payment_refresh(reg_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration, _ = owned_registration(request, db, reg_id)
    rate_limit(f"payment-check:{reg_id}", SIGNUPS, 90, 600)
    if registration.payment_id:
        order = payments.provider_request("GET", f"/v1/orders/{registration.payment_id}")
        db.execute(select(Registration).where(Registration.id == reg_id).with_for_update()).scalar_one()
        db.refresh(registration)
        payments.apply_order(registration, order)
        db.commit()
    return RedirectResponse(f"/conta/inscricoes/{reg_id}", status_code=303)


@router.get("/conta/inscricoes/{reg_id}/status")
def payment_poll(reg_id: int, request: Request, db: Session = Depends(get_db)):
    registration, _ = owned_registration(request, db, reg_id)
    rate_limit(f"payment-poll:{reg_id}", SIGNUPS, 90, 600)
    if registration.payment_id and registration.payment_status == "pending":
        try:
            order = payments.provider_request("GET", f"/v1/orders/{registration.payment_id}")
            db.execute(select(Registration).where(Registration.id == reg_id).with_for_update()).scalar_one()
            db.refresh(registration)
            payments.apply_order(registration, order)
            db.commit()
        except HTTPException:
            db.rollback()
    return {"paid": registration.payment_status == "paid", "cancelled": registration.status == "cancelled"}


@router.post("/webhooks/pix")
async def pix_webhook(request: Request, db: Session = Depends(get_db)):
    if int(request.headers.get("content-length", "0")) > 32000:
        raise HTTPException(413)
    raw = await request.body()
    if len(raw) > 32000:
        raise HTTPException(413)
    try:
        body = json.loads(raw)
        body_id = str(body.get("data", {}).get("id", ""))
    except (ValueError, AttributeError):
        raise HTTPException(400)
    order_id = request.query_params.get("data.id", body_id)
    if not re.fullmatch(r"ORD[A-Za-z0-9]{8,70}", order_id) or (body_id and body_id.lower() != order_id.lower()):
        raise HTTPException(400)
    payments.verify_signature(request, order_id)
    registration = db.scalar(select(Registration).where(Registration.payment_id == order_id.upper()).with_for_update())
    if not registration:
        return {"received": True}
    order = payments.provider_request("GET", f"/v1/orders/{registration.payment_id}")
    payments.apply_order(registration, order)
    db.commit()
    return {"received": True}


def checkin(registration, consent):
    if registration.status != "confirmed" or not registration.event.checkin_enabled:
        raise HTTPException(409, "O check-in não está disponível para esta inscrição.")
    if registration.event.mode == "paid" and registration.payment_status != "paid":
        raise HTTPException(409, "Confirme o pagamento antes do check-in.")
    if registration.checked_in_at:
        return
    registration.attended = True
    registration.checked_in_at = utc_now()
    registration.image_consent_text = IMAGE_CONSENT
    if consent == "yes":
        registration.image_consent_at = utc_now()
        registration.image_revoked_at = None


@router.post("/conta/inscricoes/{reg_id}/check-in")
def account_checkin(reg_id: int, request: Request, image_consent: str = Form(""), csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration, _ = owned_registration(request, db, reg_id)
    checkin(registration, image_consent)
    db.commit()
    return RedirectResponse(f"/conta/inscricoes/{reg_id}", status_code=303)


@router.post("/inscricao/{token}/check-in")
def token_checkin(token: str, request: Request, image_consent: str = Form(""), csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration = db.scalar(select(Registration).where(Registration.cancellation_token_hash == hash_token(token)))
    if not registration:
        raise HTTPException(404)
    checkin(registration, image_consent)
    db.commit()
    return RedirectResponse(f"/inscricao/{token}", status_code=303)


@router.post("/conta/inscricoes/{reg_id}/revogar-imagem")
def account_revoke(reg_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration, _ = owned_registration(request, db, reg_id)
    registration.image_revoked_at = utc_now()
    db.commit()
    return RedirectResponse(f"/conta/inscricoes/{reg_id}", status_code=303)


@router.post("/inscricao/{token}/revogar-imagem")
def token_revoke(token: str, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration = db.scalar(select(Registration).where(Registration.cancellation_token_hash == hash_token(token)))
    if not registration:
        raise HTTPException(404)
    registration.image_revoked_at = utc_now()
    db.commit()
    return RedirectResponse(f"/inscricao/{token}", status_code=303)


@router.post("/conta/inscricoes/{reg_id}/cancelar")
def account_cancel(reg_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    registration, _ = owned_registration(request, db, reg_id)
    from .services import cancel_registration
    cancel_registration(db, registration)
    return RedirectResponse(f"/conta/inscricoes/{reg_id}", status_code=303)


@router.get("/conta/inscricoes/{reg_id}/qr.png")
def account_qr(reg_id: int, request: Request, db: Session = Depends(get_db)):
    registration, _ = owned_registration(request, db, reg_id)
    if not registration.payment_payload or registration.status != "confirmed":
        raise HTTPException(404)
    return qr_response(registration.payment_payload)


@router.get("/inscricao/{token}/qr.png")
def token_qr(token: str, db: Session = Depends(get_db)):
    registration = db.scalar(select(Registration).where(Registration.cancellation_token_hash == hash_token(token)))
    if not registration or registration.status != "confirmed":
        raise HTTPException(404)
    payload = payments.pix_payload(registration.event, f"ETHOS{registration.id}")
    if not payload:
        raise HTTPException(404)
    return qr_response(payload)


def qr_response(payload):
    output = io.BytesIO()
    qrcode.make(payload, border=4, box_size=8).save(output, format="PNG")
    return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/admin/fotos")
def photo_admin(request: Request, db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    require_master(admin)
    current = {image.key: image for image in db.scalars(select(SiteImage)).all()}
    return render(request, "admin/photos.html", title="Fotos do site", images=list(catalog().values()), current=current)


@router.post("/admin/fotos/{key}")
async def save_photo(key: str, request: Request, file: UploadFile | None = File(None), alt: str = Form(""), rotation: int = Form(0),
                     ratio: str = Form("original"), focal_x: int = Form(50), focal_y: int = Form(50), csrf: str = Form(""),
                     db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    require_master(admin)
    verify_csrf(request, csrf)
    item = catalog().get(key)
    if not item:
        raise HTTPException(404)
    image = db.get(SiteImage, key)
    try:
        if file and file.filename:
            raw = await file.read(5_000_001)
            original = encode(decode_image(raw))
        elif image:
            original = image.original_data
        else:
            from .media import SITE
            original = encode(decode_image((SITE / item["path"].lstrip("/")).read_bytes()))
        data = transform(original, rotation, ratio, focal_x, focal_y)
        if len(alt) > 180:
            raise ValueError("A descrição deve ter até 180 caracteres.")
    except ValueError as error:
        raise HTTPException(422, str(error))
    values = dict(original_path=item["path"], alt=alt.strip(), original_data=original, data=data,
                  mime="image/webp", updated_at=utc_now(), updated_by=admin.id, edit_settings=json.dumps({"rotation":rotation,"ratio":ratio,"focal_x":focal_x,"focal_y":focal_y}))
    if image:
        for name, value in values.items():
            setattr(image, name, value)
    else:
        db.add(SiteImage(key=key, **values))
    db.commit()
    return RedirectResponse("/admin/fotos?saved=1#foto-" + key, status_code=303)


@router.post("/admin/fotos/{key}/restaurar")
def restore_photo(key: str, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    require_master(admin)
    verify_csrf(request, csrf)
    if key not in catalog():
        raise HTTPException(404)
    image = db.get(SiteImage, key)
    if image:
        db.delete(image)
        db.commit()
    return RedirectResponse("/admin/fotos#foto-" + key, status_code=303)


@router.get("/media/site/{key}")
def site_photo(key: str, db: Session = Depends(get_db)):
    image = db.get(SiteImage, key)
    if not image:
        raise HTTPException(404)
    return Response(image.data, media_type=image.mime, headers={"Cache-Control": "public, max-age=60"})


@router.get("/admin/responsaveis")
def administrators(request: Request, db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    require_master(admin)
    admins = db.scalars(select(Admin).where(Admin.is_superadmin.is_(False)).order_by(Admin.email)).all()
    events = db.scalars(select(Event).order_by(Event.event_date.desc())).all()
    grants = {(grant.admin_id, grant.event_id) for grant in db.scalars(select(EventAdmin)).all()}
    return render(request, "admin/administrators.html", title="Responsáveis por atividade", administrators=admins, events=events, grants=grants)


@router.post("/admin/responsaveis")
async def assign_administrator(request: Request, email: str = Form(...), csrf: str = Form(""), db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    require_master(admin)
    verify_csrf(request, csrf)
    member = db.scalar(select(Member).where(Member.email == email.strip().lower()))
    if not member:
        raise HTTPException(422, "A pessoa precisa criar sua própria conta antes de ser designada.")
    form = await request.form()
    try:
        event_ids = {int(value) for value in form.getlist("event_ids")}
    except ValueError:
        raise HTTPException(422)
    if not event_ids or len(event_ids) > 200:
        raise HTTPException(422, "Selecione uma ou mais atividades.")
    if len(db.scalars(select(Event.id).where(Event.id.in_(event_ids))).all()) != len(event_ids):
        raise HTTPException(422, "Atividade inválida.")
    person = db.scalar(select(Admin).where(Admin.email == member.email))
    if person and person.is_superadmin:
        raise HTTPException(409, "O administrador geral já pode gerenciar todas as atividades.")
    if not person:
        person = Admin(email=member.email, password_hash=member.password_hash, member_id=member.id, is_superadmin=False)
        db.add(person)
        db.flush()
    for event_id in event_ids:
        if not db.get(EventAdmin, (event_id, person.id)):
            db.add(EventAdmin(event_id=event_id, admin_id=person.id))
    db.commit()
    return RedirectResponse("/admin/responsaveis", status_code=303)


@router.post("/admin/responsaveis/{person_id}/{event_id}/remover")
def remove_administrator(person_id: int, event_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    require_master(admin)
    verify_csrf(request, csrf)
    grant = db.get(EventAdmin, (event_id, person_id))
    if grant:
        db.delete(grant)
        db.commit()
    return RedirectResponse("/admin/responsaveis", status_code=303)


@router.get("/admin/pix")
def pix_settings(request: Request, admin: Admin = Depends(current_admin)):
    require_master(admin)
    return render(request, "admin/pix.html", title="Pix e confirmação automática", ready=payments.payment_ready(), webhook_url=settings.base_url + "/webhooks/pix")


def paid_rows(db, event_id):
    return db.scalars(select(Registration).where(Registration.event_id == event_id, Registration.payment_status == "paid").order_by(Registration.paid_at, Registration.id)).all()


def local_stamp(value):
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M") if value else "Horário não registrado (legado)"


@router.get("/admin/eventos/{event_id}/pagamentos")
def payment_list(event_id: int, request: Request, db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    rows = paid_rows(db, event_id)
    authorizations = db.scalars(select(Registration).where(Registration.event_id == event_id, Registration.attended.is_(True))).all()
    return render(request, "admin/payments.html", title="Pagamentos da atividade", event=event, rows=rows, stamp=local_stamp,
                  total=sum(row.payment_amount_cents if row.payment_amount_cents is not None else event.price_cents for row in rows),
                  pages=max(1, math.ceil(len(rows)/25)), authorizations=authorizations)


@router.get("/admin/eventos/{event_id}/pagamentos.png")
def export_payment_image(event_id: int, page: int = 1, heading: str = "PRESENÇA CONFIRMADA", theme: str = "verde", db: Session = Depends(get_db), admin: Admin = Depends(current_admin)):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404)
    rows = paid_rows(db, event_id)
    pages = max(1, math.ceil(len(rows)/25))
    if not 1 <= page <= pages or len(heading) > 64 or theme not in ("verde", "claro"):
        raise HTTPException(422, "Personalização inválida.")
    selected = rows[(page-1)*25:page*25]
    dark, lime = "#102a25", "#c6f36a"
    background, foreground = (dark, "#ffffff") if theme == "verde" else ("#f5f6ef", dark)
    image = Image.new("RGB", (1200, 430 + max(1, len(selected))*64), background)
    draw = ImageDraw.Draw(image)
    def font(size):
        for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)
    def clipped(text, limit, face):
        while text and draw.textlength(text, font=face) > limit:
            text = text[:-1]
        return text
    draw.rounded_rectangle((40,40,1160,125), radius=14, fill=lime)
    draw.text((65,55), "ethos tribo", font=font(42), fill=dark)
    draw.text((45,155), clipped(heading.upper(),1100,font(28)), font=font(28), fill=foreground)
    draw.text((45,210), clipped(event.title,1100,font(34)), font=font(34), fill=foreground)
    draw.text((45,267), f"{event.event_date:%d/%m/%Y} · {event.starts_at:%H:%M} · {len(rows)} pagamentos · Página {page}/{pages}", font=font(22), fill=foreground)
    draw.text((45,315), "PARTICIPANTE", font=font(18), fill=foreground)
    draw.text((600,315), "CONFIRMADO EM (BRASÍLIA)", font=font(18), fill=foreground)
    draw.text((1040,315), "VALOR", font=font(18), fill=foreground)
    for index, row in enumerate(selected):
        y = 360+index*64
        draw.line((45,y-8,1155,y-8), fill="#547068", width=1)
        draw.text((45,y), clipped(f"{(page-1)*25+index+1:02d}  {row.full_name}",520,font(23)), font=font(23), fill=foreground)
        stamp = local_stamp(row.paid_at) if row.paid_at else "Legado: horário não registrado"
        draw.text((600,y), stamp, font=font(18), fill=foreground)
        amount = row.payment_amount_cents if row.payment_amount_cents is not None else event.price_cents
        draw.text((1000,y), money(amount), font=font(20), fill=foreground)
    if not selected:
        draw.text((45,365), "Nenhum pagamento confirmado nesta atividade.",font=font(25),fill=foreground)
    draw.text((45,image.height-45), "MAIS PRESENÇA, MENOS TELA.  ·  Relatório privado da organização",font=font(18),fill=foreground)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(output.getvalue(),media_type="image/png",headers={"Cache-Control":"no-store","Content-Disposition":f'attachment; filename="ethos-{event.slug}-pagamentos-{page}.png"'})
