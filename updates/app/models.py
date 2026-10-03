"""Persisted events, attendees and administrator accounts."""
from datetime import date, datetime, time, timezone
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(110), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    event_date: Mapped[date] = mapped_column(Date)
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)
    location_name: Mapped[str] = mapped_column(String(150))
    address: Mapped[str] = mapped_column(String(300))
    maps_url: Mapped[str] = mapped_column(String(400), default="")
    court_count: Mapped[int] = mapped_column(Integer, default=1)
    capacity: Mapped[int] = mapped_column(Integer, default=32)
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    pix_key: Mapped[str] = mapped_column(String(120), default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    mode: Mapped[str] = mapped_column(String(12), default="list", server_default="list")
    followup_url: Mapped[str] = mapped_column(String(400), default="", server_default="")
    extra_links: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    checkin_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    registrations: Mapped[list["Registration"]] = relationship(back_populates="event", cascade="all, delete-orphan", order_by="Registration.created_at")


class Registration(Base):
    __tablename__ = "registrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(140))
    phone: Mapped[str | None] = mapped_column(String(22), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")  # confirmed | waitlist | cancelled
    payment_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | paid | exempt
    attended: Mapped[bool] = mapped_column(Boolean, default=False)
    show_name_publicly: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancellation_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    admin_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    image_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    image_revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    image_consent_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    payment_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    payment_reference: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    payment_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_payload: Mapped[str] = mapped_column(Text, default="", server_default="")
    payment_source: Mapped[str] = mapped_column(String(24), default="", server_default="")
    event: Mapped[Event] = relationship(back_populates="registrations")
    __table_args__ = (UniqueConstraint("event_id", "phone", name="uq_event_phone"),)


class Member(Base):
    """Accounts created within this deploy; legacy site accounts need SSO integration."""
    __tablename__ = "members"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(140))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    privacy_consent_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class EventAdmin(Base):
    __tablename__ = "event_admins"
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True)


class SiteImage(Base):
    __tablename__ = "site_images"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    original_path: Mapped[str] = mapped_column(String(400))
    alt: Mapped[str] = mapped_column(String(180), default="")
    edit_settings: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    original_data: Mapped[bytes] = mapped_column(LargeBinary)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str] = mapped_column(String(30), default="image/webp")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_by: Mapped[int] = mapped_column(ForeignKey("admins.id"))
