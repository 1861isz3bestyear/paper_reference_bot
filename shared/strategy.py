from __future__ import annotations

from datetime import time, timezone, tzinfo

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
) -> tuple[pd.DataFrame, list[Trade]]:
    strategy_start_at = pd.Timestamp(strategy_start_at)
    strategy_end_at = pd.Timestamp(strategy_end_at)
    if strategy_start_at >= strategy_end_at:
        raise ValueError("Strategy start must be before strategy end.")
    if open_position_side not in {"Long", "Short", "Both"}:
        raise ValueError("Open position side must be Long, Short, or Both.")
    if minimum_order_size < 0:
        raise ValueError("Minimum order size cannot be negative.")

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
            trade_pnl = quantity * (entry_price - exit_price) - exit_fee
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
        anchored_std = float(row.anchored_std) if pd.notna(row.anchored_std) else np.nan
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
                    price = stop_price
                    exit_reason = "Stop loss"
                elif take_profit_pct > 0 and float(row.high) >= target_price:
                    price = target_price
                    exit_reason = "Take profit"
                elif strategy_mode == "AVWAP crossover" and pd.notna(vwap) and price < vwap and previous_close >= previous_vwap:
                    exit_reason = "Close below AVWAP"
                elif (
                    strategy_mode == "VWAP band mean reversion"
                    and pd.notna(long_exit_band)
                    and (
                        (
                            trend_mode
                            and (
                                (exit_band_sigma >= band_sigma and float(row.high) >= long_exit_band)
                                or (exit_band_sigma < band_sigma and float(row.low) <= long_exit_band)
                            )
                        )
                        or (
                            not trend_mode
                            and (
                                (exit_band_sigma <= band_sigma and float(row.high) >= long_exit_band)
                                or (exit_band_sigma > band_sigma and float(row.low) <= long_exit_band)
                            )
                        )
                    )
                ):
                    price = long_exit_band
                    exit_sign = "+" if trend_mode else "-"
                    exit_reason = f"Reached {exit_sign}{exit_band_sigma:g}σ exit band"
            else:
                stop_price = entry_price * (1 + stop_loss_pct / 100)
                target_price = entry_price * (1 - take_profit_pct / 100)
                if stop_loss_pct > 0 and float(row.high) >= stop_price:
                    price = stop_price
                    exit_reason = "Stop loss"
                elif take_profit_pct > 0 and float(row.low) <= target_price:
                    price = target_price
                    exit_reason = "Take profit"
                elif (
                    strategy_mode == "VWAP band mean reversion"
                    and pd.notna(short_exit_band)
                    and (
                        (
                            trend_mode
                            and (
                                (exit_band_sigma >= band_sigma and float(row.low) <= short_exit_band)
                                or (exit_band_sigma < band_sigma and float(row.high) >= short_exit_band)
                            )
                        )
                        or (
                            not trend_mode
                            and (
                                (exit_band_sigma <= band_sigma and float(row.low) <= short_exit_band)
                                or (exit_band_sigma > band_sigma and float(row.high) >= short_exit_band)
                            )
                        )
                    )
                ):
                    price = short_exit_band
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
                touched_upper = pd.notna(upper_band) and float(row.high) >= upper_band
                touched_lower = pd.notna(lower_band) and float(row.low) <= lower_band
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

        is_end_of_scheduled_day = scheduled_days is not None and (
            row_number == len(strategy_df) - 1 or scheduled_days.iloc[row_number + 1] != scheduled_day
        )
        if position_side is not None and is_end_of_scheduled_day:
            close_position(float(row.close), timestamp, "End of scheduled session")

        if position_side == "Long":
            equity = quantity * float(row.close)
            signed_position = quantity
        elif position_side == "Short":
            equity = entry_capital + quantity * (entry_price - float(row.close))
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
