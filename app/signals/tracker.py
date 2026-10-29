"""Signal outcome tracking on M1 bars, shared by live and backtest.

Prices in bars are mid; bid = mid - spread/2, ask = mid + spread/2.
  BUY:  limit fills when ask <= entry; exits on bid (SL: bid <= stop, TP: bid >= tp).
  SELL: limit fills when bid >= entry; exits on ask (SL: ask >= stop, TP: ask <= tp).
Conservative rules: SL and TP in one bar -> SL; on the fill bar only SL is checked;
SL is a stop order and gets `slippage` against us, limits don't.
After TP1, `tp1_close_fraction` is booked and the stop moves to entry (breakeven).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.signals.models import Direction, SignalStatus

FINAL = {
    SignalStatus.TP2,
    SignalStatus.SL,
    SignalStatus.BREAKEVEN,
    SignalStatus.EXPIRED,
    SignalStatus.INVALIDATED,
}


@dataclass
class TrackEvent:
    status: SignalStatus
    time: pd.Timestamp
    price: float


@dataclass
class TradeTracker:
    direction: Direction
    entry: float
    stop: float
    tp1: float
    tp2: float | None
    expires_at: pd.Timestamp
    tp1_close_fraction: float = 0.5
    slippage: float = 0.0

    status: SignalStatus = SignalStatus.PENDING
    triggered_at: pd.Timestamp | None = None
    closed_at: pd.Timestamp | None = None
    result_r: float | None = None
    mfe_r: float = 0.0  # max favourable excursion after fill, in R
    mae_r: float = 0.0  # max adverse excursion after fill, in R (<= 0)
    events: list[TrackEvent] = field(default_factory=list)
    last_bar: pd.Timestamp | None = None  # open time of the last M1 bar fed
    _booked_r: float = 0.0

    def restore_booked(self) -> None:
        """After a restart in TP1 state, re-book the TP1 part (status alone isn't enough)."""
        if self.status is SignalStatus.TP1 and self.tp2 is not None:
            self._booked_r = self.tp1_close_fraction * self._r(self.tp1)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def done(self) -> bool:
        return self.status in FINAL or (self.status is SignalStatus.TP1 and self.tp2 is None)

    def _r(self, price: float) -> float:
        sign = 1 if self.direction is Direction.BUY else -1
        return sign * (price - self.entry) / self.risk

    def _emit(self, status: SignalStatus, t: pd.Timestamp, price: float) -> TrackEvent:
        self.status = status
        ev = TrackEvent(status, t, price)
        self.events.append(ev)
        return ev

    def _close(self, status: SignalStatus, t: pd.Timestamp, price: float, r: float) -> TrackEvent:
        self.closed_at = t
        self.result_r = r
        return self._emit(status, t, price)

    def update(self, t: pd.Timestamp, high: float, low: float, spread: float) -> list[TrackEvent]:
        """Feed one closed M1 bar (open time `t`). Returns the events it produced."""
        if self.done or (self.last_bar is not None and t <= self.last_bar):
            return []
        self.last_bar = t
        half = spread / 2
        buy = self.direction is Direction.BUY
        bid_hi, bid_lo, ask_hi, ask_lo = high - half, low - half, high + half, low + half
        out: list[TrackEvent] = []

        if self.status is SignalStatus.PENDING:
            if t >= self.expires_at:
                return [self._close(SignalStatus.EXPIRED, t, self.entry, 0.0)]
            stop_hit = bid_lo <= self.stop if buy else ask_hi >= self.stop
            filled = ask_lo <= self.entry if buy else bid_hi >= self.entry
            if not filled:
                if stop_hit:  # gapped through the stop without filling
                    return [self._close(SignalStatus.INVALIDATED, t, self.stop, 0.0)]
                return []
            self.triggered_at = t
            out.append(self._emit(SignalStatus.TRIGGERED, t, self.entry))
            if stop_hit:
                exit_px = self.stop - self.slippage if buy else self.stop + self.slippage
                out.append(self._close(SignalStatus.SL, t, exit_px, self._r(exit_px)))
                return out
            self._excursion(bid_hi, bid_lo, ask_hi, ask_lo)
            return out

        self._excursion(bid_hi, bid_lo, ask_hi, ask_lo)
        if self.status is SignalStatus.TRIGGERED:
            stop_hit = bid_lo <= self.stop if buy else ask_hi >= self.stop
            tp1_hit = bid_hi >= self.tp1 if buy else ask_lo <= self.tp1
            if stop_hit:
                exit_px = self.stop - self.slippage if buy else self.stop + self.slippage
                out.append(self._close(SignalStatus.SL, t, exit_px, self._r(exit_px)))
            elif tp1_hit:
                if self.tp2 is None:
                    out.append(self._close(SignalStatus.TP1, t, self.tp1, self._r(self.tp1)))
                else:
                    self._booked_r = self.tp1_close_fraction * self._r(self.tp1)
                    out.append(self._emit(SignalStatus.TP1, t, self.tp1))
            return out

        # TP1 reached with a runner left: stop at breakeven, target TP2.
        assert self.tp2 is not None
        rest = 1 - self.tp1_close_fraction
        be_hit = bid_lo <= self.entry if buy else ask_hi >= self.entry
        tp2_hit = bid_hi >= self.tp2 if buy else ask_lo <= self.tp2
        if be_hit:
            out.append(self._close(SignalStatus.BREAKEVEN, t, self.entry, self._booked_r))
        elif tp2_hit:
            r = self._booked_r + rest * self._r(self.tp2)
            out.append(self._close(SignalStatus.TP2, t, self.tp2, r))
        return out

    def _excursion(self, bid_hi: float, bid_lo: float, ask_hi: float, ask_lo: float) -> None:
        if self.direction is Direction.BUY:
            self.mfe_r = max(self.mfe_r, self._r(bid_hi))
            self.mae_r = min(self.mae_r, self._r(bid_lo))
        else:
            self.mfe_r = max(self.mfe_r, self._r(ask_lo))
            self.mae_r = min(self.mae_r, self._r(ask_hi))
