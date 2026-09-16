from __future__ import annotations

from datetime import date, time, timezone, tzinfo

import numpy as np
import pandas as pd

from shared.models import Trade
def backtest(
    df: pd.DataFrame,
    strategy_start_at: pd.Timestamp,
    strategy_end_at: pd.Timestamp,
    initial_capital: float,
    fee_pct: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    allow_reentry: bool,
    strategy_mode: str,
    band_sigma: float,
    active_weekdays: set[int] | None = None,
    strategy_timezone: tzinfo = timezone.utc,
    active_session_start: time = time.min,
    active_session_end: time = time.max,
    exit_band_sigma: float = 0.0,
    open_position_side: str = "Both",
    trend_mode: bool = False,
    minimum_order_size: float = 0.0,
    stop_loss_basis: str = "Position",
    hedge_entry_timeout_bars: int = 3,
) -> tuple[pd.DataFrame, list[Trade]]:
    if not np.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("Initial capital must be finite and positive.")
    if not np.isfinite(fee_pct) or not 0 <= fee_pct < 100:
        raise ValueError("Fee percentage must be finite and between 0 and 100.")
    for value in (stop_loss_pct, take_profit_pct, minimum_order_size, band_sigma, exit_band_sigma):
        if not np.isfinite(value) or value < 0:
            raise ValueError("Risk, band, and minimum order parameters must be finite and nonnegative.")
    prices = df[["open", "high", "low", "close"]]
    if (not np.isfinite(prices.to_numpy(dtype=float)).all() or (prices <= 0).any().any()
            or (df["low"] > prices.min(axis=1)).any() or (df["high"] < prices.max(axis=1)).any()):
        raise ValueError("Candles must contain finite positive prices with valid OHLC ranges.")
    if df["time"].isna().any() or not df["time"].is_monotonic_increasing or df["time"].duplicated().any():
        raise ValueError("Candle times must be unique, valid, and increasing.")
    strategy_start_at = pd.Timestamp(strategy_start_at)
    strategy_end_at = pd.Timestamp(strategy_end_at)
    if strategy_start_at >= strategy_end_at:
        raise ValueError("Strategy start must be before strategy end.")
    if open_position_side not in {"Long", "Short", "Both"}:
        raise ValueError("Open position side must be Long, Short, or Both.")
    if minimum_order_size < 0:
        raise ValueError("Minimum order size cannot be negative.")

    # Orders use only indicators known before the candle opened.
    df = df.copy()
    df["order_vwap"] = df["anchored_vwap"].shift(1)
    df["order_std"] = df["anchored_std"].shift(1)
    strategy_df = df[
        (df["time"] >= strategy_start_at) & (df["time"] <= strategy_end_at)
    ].copy()
    scheduled_days = None
    if active_weekdays is not None:
        if not active_weekdays:
            return pd.DataFrame(columns=["time", "equity", "position", "close", "anchored_vwap"]), []
        if not active_weekdays.issubset(set(range(7))):
            raise ValueError("Active weekdays must use values from 0 (Monday) through 6 (Sunday).")
        if active_session_start == active_session_end:
            raise ValueError("Trade start and end times must be different.")
        local_times = pd.to_datetime(strategy_df["time"], utc=True).dt.tz_convert(strategy_timezone)
        if active_session_start < active_session_end:
            session_mask = (
                local_times.dt.weekday.isin(active_weekdays)
                & (local_times.dt.time >= active_session_start)
                & (local_times.dt.time <= active_session_end)
            )
            session_start_days = local_times.dt.date
        else:
            starts_today = local_times.dt.weekday.isin(active_weekdays) & (local_times.dt.time >= active_session_start)
            started_yesterday = (
                ((local_times.dt.weekday - 1) % 7).isin(active_weekdays)
                & (local_times.dt.time <= active_session_end)
            )
            session_mask = starts_today | started_yesterday
            session_start_days = local_times.dt.date.where(
                starts_today,
                (local_times - pd.Timedelta(days=1)).dt.date,
            )
        strategy_df = strategy_df[session_mask].copy()
        scheduled_days = session_start_days.loc[strategy_df.index].reset_index(drop=True)
    strategy_df = strategy_df.reset_index(drop=True)
    if strategy_df.empty:
        return pd.DataFrame(columns=["time", "equity", "position", "close", "anchored_vwap"]), []

    from shared.hedge_strategy import HEDGE_MODE, hedge_backtest
    if strategy_mode == HEDGE_MODE:
        return hedge_backtest(strategy_df, initial_capital, fee_pct, exit_band_sigma,
            stop_loss_pct, stop_loss_basis, hedge_entry_timeout_bars, minimum_order_size, scheduled_days)

    cash = initial_capital
    quantity = 0.0
    entry_price = 0.0
    entry_capital = 0.0
    entry_time: pd.Timestamp | None = None
    position_side: str | None = None
    trades: list[Trade] = []
    equity_curve: list[dict[str, object]] = []
    crossed_down_since_exit = True
    previous_close = np.nan
    previous_vwap = np.nan
    previous_upper_band = np.nan
    previous_lower_band = np.nan
    def close_position(exit_price: float, exit_time: pd.Timestamp, exit_reason: str) -> None:
        nonlocal cash, quantity, entry_price, entry_capital, entry_time, position_side
        if entry_time is None or position_side is None:
            return

        if position_side == "Long":
            gross_cash = quantity * exit_price
            exit_fee = gross_cash * fee_pct / 100
            cash = gross_cash - exit_fee
            trade_pnl = cash - entry_capital
        else:
            gross_exposure = quantity * exit_price
            exit_fee = gross_exposure * fee_pct / 100
            trade_pnl = quantity * (entry_price - exit_price) - entry_capital * fee_pct / 100 - exit_fee
            cash = entry_capital + trade_pnl
        return_pct = trade_pnl / entry_capital * 100
        trades.append(
            Trade(
                deposit_size=entry_capital,
                side=position_side,
                entry_time=entry_time,
                exit_time=exit_time,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=trade_pnl,
                return_pct=return_pct,
                exit_reason=exit_reason,
            )
        )
        quantity = 0.0
        entry_price = 0.0
        entry_capital = 0.0
        entry_time = None
        position_side = None
    def open_position(side: str, price: float, timestamp: pd.Timestamp) -> None:
        nonlocal cash, quantity, entry_price, entry_capital, entry_time, position_side
        entry_price = price
        entry_time = timestamp
        entry_capital = cash
        entry_fee = cash * fee_pct / 100
        quantity = (cash - entry_fee) / entry_price
        cash = 0.0
        position_side = side

    def band_fill(row, level: float, upward: bool) -> float | None:
        if pd.isna(level):
            return None
        opening = float(row.open)
        # A pre-existing exit crossed by a gap executes at the opening price.
        if (upward and opening >= level) or (not upward and opening <= level):
            return opening
        if float(row.low) <= level <= float(row.high):
            return level
        return None

    previous_scheduled_day: date | None = None
    for row_number, row in enumerate(strategy_df.itertuples(index=False)):
        closed_this_candle = False
        scheduled_day = scheduled_days.iloc[row_number] if scheduled_days is not None else None
        if scheduled_day is not None and scheduled_day != previous_scheduled_day:
            previous_close = np.nan
            previous_vwap = np.nan
            previous_upper_band = np.nan
            previous_lower_band = np.nan
            crossed_down_since_exit = True

        price = float(row.close)
        vwap = float(row.anchored_vwap) if pd.notna(row.anchored_vwap) else np.nan
        if strategy_mode == "VWAP band mean reversion":
            vwap = float(row.order_vwap)
        anchored_std = float(row.order_std) if pd.notna(row.order_std) else np.nan
        upper_band = vwap + anchored_std * band_sigma if pd.notna(vwap) and pd.notna(anchored_std) else np.nan
        lower_band = vwap - anchored_std * band_sigma if pd.notna(vwap) and pd.notna(anchored_std) else np.nan
        if trend_mode:
            long_exit_band = vwap + anchored_std * exit_band_sigma if pd.notna(vwap) and pd.notna(anchored_std) else np.nan
            short_exit_band = vwap - anchored_std * exit_band_sigma if pd.notna(vwap) and pd.notna(anchored_std) else np.nan
        else:
            long_exit_band = vwap - anchored_std * exit_band_sigma if pd.notna(vwap) and pd.notna(anchored_std) else np.nan
            short_exit_band = vwap + anchored_std * exit_band_sigma if pd.notna(vwap) and pd.notna(anchored_std) else np.nan
        timestamp = row.time

        if position_side is not None:
            exit_reason = None

            if position_side == "Long":
                stop_price = entry_price * (1 - stop_loss_pct / 100)
                target_price = entry_price * (1 + take_profit_pct / 100)
                if stop_loss_pct > 0 and float(row.low) <= stop_price:
                    price = min(stop_price, float(row.open))
                    exit_reason = "Stop loss"
                elif take_profit_pct > 0 and float(row.high) >= target_price:
                    price = max(target_price, float(row.open))
                    exit_reason = "Take profit"
                elif strategy_mode == "AVWAP crossover" and pd.notna(vwap) and price < vwap and previous_close >= previous_vwap:
                    exit_reason = "Close below AVWAP"
                elif strategy_mode == "VWAP band mean reversion":
                    exit_price = band_fill(row, long_exit_band, exit_band_sigma >= band_sigma if trend_mode else exit_band_sigma <= band_sigma)
                    if exit_price is not None:
                        price = exit_price
                        exit_sign = "+" if trend_mode else "-"
                        exit_reason = f"Reached {exit_sign}{exit_band_sigma:g}σ exit band"
            else:
                stop_price = entry_price * (1 + stop_loss_pct / 100)
                target_price = entry_price * (1 - take_profit_pct / 100)
                if stop_loss_pct > 0 and float(row.high) >= stop_price:
                    price = max(stop_price, float(row.open))
                    exit_reason = "Stop loss"
                elif take_profit_pct > 0 and float(row.low) <= target_price:
                    price = min(target_price, float(row.open))
                    exit_reason = "Take profit"
                elif strategy_mode == "VWAP band mean reversion":
                    exit_price = band_fill(row, short_exit_band, exit_band_sigma < band_sigma if trend_mode else exit_band_sigma > band_sigma)
                    if exit_price is not None:
                        price = exit_price
                        exit_sign = "-" if trend_mode else "+"
                        exit_reason = f"Reached {exit_sign}{exit_band_sigma:g}σ exit band"

            if exit_reason:
                close_position(price, timestamp, exit_reason)
                closed_this_candle = True
                crossed_down_since_exit = True

        if position_side is None and not closed_this_candle and pd.notna(vwap):
            if strategy_mode == "AVWAP crossover":
                crossed_up = price > vwap and (pd.isna(previous_vwap) or previous_close <= previous_vwap)
                if price < vwap:
                    crossed_down_since_exit = True

                can_enter = allow_reentry or crossed_down_since_exit
                if crossed_up and can_enter and cash >= minimum_order_size and cash > 0:
                    open_position("Long", price, timestamp)
                    crossed_down_since_exit = False
            else:
                touched_upper = pd.notna(upper_band) and float(row.low) <= upper_band <= float(row.high)
                touched_lower = pd.notna(lower_band) and float(row.low) <= lower_band <= float(row.high)
                if trend_mode:
                    if cash >= minimum_order_size and cash > 0 and touched_lower and open_position_side in {"Short", "Both"}:
                        open_position("Short", lower_band, timestamp)
                    elif cash >= minimum_order_size and cash > 0 and touched_upper and open_position_side in {"Long", "Both"}:
                        open_position("Long", upper_band, timestamp)
                else:
                    if cash >= minimum_order_size and cash > 0 and touched_upper and open_position_side in {"Short", "Both"}:
                        open_position("Short", upper_band, timestamp)
                    elif cash >= minimum_order_size and cash > 0 and touched_lower and open_position_side in {"Long", "Both"}:
                        open_position("Long", lower_band, timestamp)

        # Intrabar ordering is unknown: charge a reachable adverse entry-bar
        # stop, but never award an ambiguous same-bar profit.
        if strategy_mode == "VWAP band mean reversion" and position_side is not None and entry_time == timestamp and stop_loss_pct > 0:
            stop_price = entry_price * (1 - stop_loss_pct / 100 if position_side == "Long" else 1 + stop_loss_pct / 100)
            if (position_side == "Long" and float(row.low) <= stop_price) or (position_side == "Short" and float(row.high) >= stop_price):
                close_position(stop_price, timestamp, "Stop loss")

        is_end_of_scheduled_day = scheduled_days is not None and (
            row_number == len(strategy_df) - 1 or scheduled_days.iloc[row_number + 1] != scheduled_day
        )
        if position_side is not None and is_end_of_scheduled_day:
            close_position(float(row.close), timestamp, "End of scheduled session")

        if position_side == "Long":
            equity = quantity * float(row.close)
            signed_position = quantity
        elif position_side == "Short":
            equity = entry_capital * (1 - fee_pct / 100) + quantity * (entry_price - float(row.close))
            signed_position = -quantity
        else:
            equity = cash
            signed_position = 0.0

        equity_curve.append(
            {
                "time": timestamp,
                "equity": equity,
                "position": signed_position,
                "close": float(row.close),
                "anchored_vwap": row.anchored_vwap,
            }
        )

        previous_close = float(row.close)
        previous_vwap = vwap
        previous_upper_band = upper_band
        previous_lower_band = lower_band
        previous_scheduled_day = scheduled_day

    if position_side is not None:
        final_row = strategy_df.iloc[-1]
        close_position(float(final_row["close"]), final_row["time"], "End of strategy")
        equity_curve[-1]["equity"] = cash
        equity_curve[-1]["position"] = 0.0

    if any(
        trade.entry_time < strategy_start_at
        or trade.entry_time > strategy_end_at
        or trade.exit_time < strategy_start_at
        or trade.exit_time > strategy_end_at
        for trade in trades
    ):
        raise RuntimeError("Backtest generated a trade outside the strategy timeline.")

    return pd.DataFrame(equity_curve), trades
