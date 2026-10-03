"""Idempotent additive upgrades; no deletes or changes to existing event modes."""
from sqlalchemy import inspect
from .database import Base, engine
from . import models

COLUMNS = {
    "admins": {"is_superadmin": "BOOLEAN NOT NULL DEFAULT TRUE", "member_id": "INTEGER REFERENCES members(id)"},
    "events": {"mode": "VARCHAR(12) NOT NULL DEFAULT 'list'", "followup_url": "VARCHAR(400) NOT NULL DEFAULT ''", "extra_links": "TEXT NOT NULL DEFAULT '[]'", "checkin_enabled": "BOOLEAN NOT NULL DEFAULT TRUE"},
    "registrations": {
        "member_id": "INTEGER REFERENCES members(id)", "paid_at": "TIMESTAMP", "checked_in_at": "TIMESTAMP",
        "image_consent_at": "TIMESTAMP", "image_revoked_at": "TIMESTAMP", "image_consent_text": "TEXT NOT NULL DEFAULT ''",
        "payment_id": "VARCHAR(80)", "payment_reference": "VARCHAR(64)", "payment_amount_cents": "INTEGER",
        "payment_payload": "TEXT NOT NULL DEFAULT ''", "payment_source": "VARCHAR(24) NOT NULL DEFAULT ''"
    }
}


def upgrade():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, additions in COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in additions.items():
                if name not in existing:
                    conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
        for column in ("payment_id", "payment_reference"):
            conn.exec_driver_sql(f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_registration_{column}" ON registrations ({column})')
        conn.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_registration_member ON registrations (event_id, member_id)')
