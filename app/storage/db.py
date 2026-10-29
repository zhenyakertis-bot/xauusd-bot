"""SQLite storage (SQLAlchemy 2.0; switch to PostgreSQL via DATABASE_URL)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import JSON, DateTime, Float, Integer, String, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.risk.position_size import PositionSize
from app.signals.models import Signal, SignalStatus

# Open = not closed yet. TP1 can be final (no TP2) or open (runner to TP2): closed_at decides.
OPEN_STATUSES = (SignalStatus.PENDING.value, SignalStatus.TRIGGERED.value, SignalStatus.TP1.value)


class Base(DeclarativeBase):
    pass


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(4))
    session: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_low: Mapped[float] = mapped_column(Float)
    entry_high: Mapped[float] = mapped_column(Float)
    entry: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
    rr1: Mapped[float] = mapped_column(Float)
    rr2: Mapped[float | None] = mapped_column(Float, nullable=True)
    lots: Mapped[float] = mapped_column(Float, default=0.0)
    risk_amount: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), index=True)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_r: Mapped[float] = mapped_column(Float, default=0.0)
    mae_r: Mapped[float] = mapped_column(Float, default=0.0)
    tracked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class KV(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(1024))


class Store:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)

    # --- signals -----------------------------------------------------------------

    def add_signal(
        self, key: str, s: Signal, size: PositionSize | None, explanation: list[str]
    ) -> int:
        row = SignalRow(
            key=key,
            direction=s.direction.value,
            session=s.session,
            created_at=s.created_at.to_pydatetime(),
            expires_at=s.expires_at.to_pydatetime(),
            entry_low=s.entry_zone.low,
            entry_high=s.entry_zone.high,
            entry=s.entry,
            stop=s.stop,
            tp1=s.tp1,
            tp2=s.tp2,
            rr1=s.rr1,
            rr2=s.rr2,
            lots=float(size.lots) if size else 0.0,
            risk_amount=float(size.actual_risk) if size else 0.0,
            explanation=explanation,
            status=s.status.value,
        )
        with Session(self.engine) as db, db.begin():
            db.add(row)
            db.flush()
            return row.id

    def get(self, signal_id: int) -> SignalRow | None:
        with Session(self.engine, expire_on_commit=False) as db:
            return db.get(SignalRow, signal_id)

    def update(self, signal_id: int, **fields: object) -> None:
        with Session(self.engine) as db, db.begin():
            row = db.get(SignalRow, signal_id)
            if row is None:
                raise KeyError(signal_id)
            for k, v in fields.items():
                setattr(row, k, v)

    def open_signals(self) -> list[SignalRow]:
        with Session(self.engine, expire_on_commit=False) as db:
            q = (
                select(SignalRow)
                .where(SignalRow.status.in_(OPEN_STATUSES), SignalRow.closed_at.is_(None))
                .order_by(SignalRow.id)
            )
            return list(db.scalars(q))

    def last(self, n: int) -> list[SignalRow]:
        with Session(self.engine, expire_on_commit=False) as db:
            return list(db.scalars(select(SignalRow).order_by(SignalRow.id.desc()).limit(n)))

    def since(self, t: datetime) -> list[SignalRow]:
        with Session(self.engine, expire_on_commit=False) as db:
            q = select(SignalRow).where(SignalRow.created_at >= t).order_by(SignalRow.id)
            return list(db.scalars(q))

    def keys(self) -> set[str]:
        with Session(self.engine) as db:
            return set(db.scalars(select(SignalRow.key)))

    # --- flags ---------------------------------------------------------------------

    def get_flag(self, key: str, default: object = None) -> object:
        with Session(self.engine) as db:
            row = db.get(KV, key)
            return default if row is None else json.loads(row.value)

    def set_flag(self, key: str, value: object) -> None:
        with Session(self.engine) as db, db.begin():
            db.merge(KV(key=key, value=json.dumps(value)))


def utc(dt: datetime | None) -> pd.Timestamp | None:
    """SQLite returns naive datetimes; everything is stored in UTC."""
    if dt is None:
        return None
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def rows_to_frame(rows: list[SignalRow]) -> pd.DataFrame:
    """Same columns the backtest metrics use."""
    return pd.DataFrame(
        [
            {
                "created_at": utc(r.created_at),
                "closed_at": utc(r.closed_at),
                "session": r.session,
                "direction": r.direction,
                "status": r.status if r.closed_at is not None else "OPEN",
                "result_r": r.result_r,
                "mfe_r": r.mfe_r,
                "mae_r": r.mae_r,
            }
            for r in rows
        ]
    )
