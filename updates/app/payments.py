"""Pix BR Code and authenticated Mercado Pago Orders integration."""
import hashlib
import hmac
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import httpx
from fastapi import HTTPException
from .models import utc_now


def payment_ready():
    return bool(os.getenv("MP_ACCESS_TOKEN") and os.getenv("MP_WEBHOOK_SECRET"))


def pix_key(event):
    return event.pix_key or os.getenv("PIX_KEY", "")


def field(tag, value):
    return f"{tag}{len(value):02d}{value}"


def crc16(payload):
    crc = 0xffff
    for b in payload.encode("utf-8"):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    return f"{crc:04X}"


def pix_payload(event, reference="***"):
    key = pix_key(event).strip()
    if not key:
        return ""
    digits = re.sub(r"\D", "", key)
    if re.fullmatch(r"[+\d ()-]+", key) and len(digits) in (10, 11, 12, 13):
        key = "+" + (digits if digits.startswith("55") and len(digits) in (12, 13) else "55" + digits)
    def plain(value, limit):
        return re.sub(r"[^A-Z0-9 ]", "", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper())[:limit]
    merchant = field("00", "br.gov.bcb.pix") + field("01", key)
    body = field("00", "01") + field("26", merchant) + field("52", "0000") + field("53", "986")
    if event.price_cents:
        body += field("54", f"{event.price_cents/100:.2f}")
    body += field("58", "BR") + field("59", plain(os.getenv("PIX_RECEIVER_NAME", "ETHOS TRIBO"), 25)) + field("60", "CURITIBA") + field("62", field("05", reference)) + "6304"
    return body + crc16(body)


def provider_request(method, path, **kwargs):
    token = os.getenv("MP_ACCESS_TOKEN", "")
    if not token:
        raise HTTPException(503, "A organização ainda precisa conectar o provedor Pix.")
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    try:
        response = httpx.request(method, "https://api.mercadopago.com" + path, headers=headers, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        # Do not render or log provider bodies, credentials or payer information.
        raise HTTPException(502, "O provedor de pagamentos não respondeu. Tente novamente em alguns instantes.")


def create_order(registration, member, event):
    amount = f"{registration.payment_amount_cents/100:.2f}"
    return provider_request("POST", "/v1/orders", headers={"X-Idempotency-Key": registration.payment_reference}, json={
        "type": "online", "total_amount": amount, "external_reference": registration.payment_reference,
        "processing_mode": "automatic", "payer": {"email": member.email},
        "transactions": {"payments": [{"amount": amount, "payment_method": {"id": "pix", "type": "bank_transfer"}, "expiration_time": "PT24H"}]}
    })


def validate_order(registration, order):
    try:
        valid_amount = Decimal(str(order["total_amount"])) * 100 == registration.payment_amount_cents
    except (KeyError, InvalidOperation, TypeError):
        valid_amount = False
    if not valid_amount or order.get("external_reference") != registration.payment_reference:
        raise HTTPException(409, "A cobrança recebida não corresponde a esta inscrição.")
    order_id = str(order.get("id", ""))
    if not re.fullmatch(r"ORD[A-Za-z0-9]{8,70}", order_id):
        raise HTTPException(502, "O provedor retornou uma cobrança inválida.")
    if registration.payment_id and order_id != registration.payment_id:
        raise HTTPException(409, "A cobrança recebida não corresponde a esta inscrição.")
    payments = order.get("transactions", {}).get("payments", [])
    if len(payments) != 1 or payments[0].get("payment_method", {}).get("id") != "pix":
        raise HTTPException(409, "Meio de pagamento inesperado.")
    if order.get("currency_id", "BRL") != "BRL" or order.get("country_code", "BRA") != "BRA":
        raise HTTPException(409, "Moeda inesperada.")
    return payments[0]


def apply_order(registration, order):
    payment = validate_order(registration, order)
    registration.payment_id = str(order["id"])
    registration.payment_source = "mercado_pago"
    qr = payment.get("payment_method", {}).get("qr_code", "")
    if qr and len(qr) <= 2000:
        registration.payment_payload = qr
    # Webhook bodies alone never confirm payment: caller must GET this order with server credentials.
    try:
        amount_paid = Decimal(str(payment.get("paid_amount", "0"))) * 100
    except InvalidOperation:
        amount_paid = 0
    if order.get("status") == "processed" and order.get("status_detail") == "accredited" and payment.get("status") == "processed" and payment.get("status_detail") == "accredited" and amount_paid == registration.payment_amount_cents:
        if registration.payment_status != "paid":
            registration.paid_at = utc_now()
        registration.payment_status = "paid"
    elif order.get("status") in ("refunded", "charged_back") or payment.get("status") in ("refunded", "charged_back"):
        registration.payment_status = "pending"
        registration.attended = False
    return registration


def verify_signature(request, order_id):
    secret = os.getenv("MP_WEBHOOK_SECRET", "")
    parts = dict(part.strip().split("=", 1) for part in request.headers.get("x-signature", "").split(",") if "=" in part)
    stamp, signature = parts.get("ts", ""), parts.get("v1", "")
    request_id = request.headers.get("x-request-id", "")
    if not secret or not stamp.isdigit() or not request_id or not re.fullmatch(r"[a-f0-9]{64}", signature):
        raise HTTPException(401, "Notificação não autenticada.")
    seconds = int(stamp) / 1000 if len(stamp) > 10 else int(stamp)
    if abs(time.time() - seconds) > 300:
        raise HTTPException(401, "Notificação expirada.")
    manifest = f"id:{order_id.lower()};request-id:{request_id};ts:{stamp};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Notificação não autenticada.")
