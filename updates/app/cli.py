"""Usage: python -m app.cli seed | create-admin usuario@dominio.com"""
from __future__ import annotations
import argparse
from datetime import date, time
from getpass import getpass
from pathlib import Path
from sqlalchemy import select
from .database import Base, engine, SessionLocal, settings
from .models import Admin, Event, Registration
from .security import password_hash

DEFAULT_NAMES = []


def initialize_db():
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///", 1)[-1]
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    from .migrations import upgrade
    upgrade()


def seed():
    """Create the initial event once without personal data."""
    initialize_db()
    with SessionLocal() as db:
        event = db.scalar(select(Event).where(Event.slug == "volei-ethos-4-edicao"))
        if event:
            print("O evento Vôlei Ethos já existe. Nenhuma alteração feita.")
            return
        event = Event(
            title="Vôlei Ethos — 4ª edição",
            slug="volei-ethos-4-edicao",
            description="Duas quadras e uma tarde para jogar, conhecer pessoas e viver bons encontros.",
            event_date=date(2026, 10, 3), starts_at=time(15, 0), ends_at=time(17, 0),
            location_name="Win Juvevê", address="R. Padre Germano Mayer, 1921 - Hugo Lange, Curitiba - PR, 80040-170",
            court_count=2, capacity=32, price_cents=2000, pix_key="",
            is_published=True, is_closed=False,
        )
        db.add(event)
        db.flush()
        for name in DEFAULT_NAMES:
            db.add(Registration(event_id=event.id, full_name=name, status="confirmed",
                                payment_status="pending", show_name_publicly=False))
        db.commit()
        print(f"Evento '{event.title}' criado com {len(DEFAULT_NAMES)} nomes. Capacidade editável: {event.capacity}.")
        print("Os registros importados não possuem telefone, e-mail ou consentimento para exibição pública.")


def create_admin(email: str):
    initialize_db()
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise SystemExit("Informe um e-mail válido.")
    with SessionLocal() as db:
        if db.scalar(select(Admin).where(Admin.email == email)):
            raise SystemExit("Esse administrador já existe.")
        password = getpass("Defina a senha (mínimo 12 caracteres): ")
        confirmation = getpass("Repita a senha: ")
        if password != confirmation or len(password) < 12:
            raise SystemExit("As senhas devem coincidir e ter pelo menos 12 caracteres.")
        db.add(Admin(email=email, password_hash=password_hash(password)))
        db.commit()
    print(f"Conta {email} criada. Guarde a senha em local seguro.")


def main():
    parser = argparse.ArgumentParser(description="Inicialização do Ethos Eventos")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Cria o evento inicial sem dados pessoais")
    admin_parser = commands.add_parser("create-admin", help="Cria uma conta de administrador")
    admin_parser.add_argument("email", help="E-mail do organizador")
    args = parser.parse_args()
    if args.command == "seed":
        seed()
    elif args.command == "create-admin":
        create_admin(args.email)


if __name__ == "__main__":
    main()
