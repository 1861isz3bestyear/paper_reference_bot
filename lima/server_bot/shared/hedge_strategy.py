"""Causal VWAP-zero hedge strategy, shared by historical and streaming runners."""
from dataclasses import dataclass, field
import math
import pandas as pd
from shared.models import Trade

HEDGE_MODE = "VWAP zero hedge"


def stop_price(entry, quantity, allocation, deposit, percent, basis, fee_pct=0.):
    """Long/short stops limiting net loss, including estimated exit fees."""
    if basis not in {"Position", "Deposit"}:
        raise ValueError("Stop loss basis must be Position or Deposit.")
    if percent == 0:
        return {"Long": None, "Short": None}
    risk = (allocation if basis == "Position" else deposit) * percent / 100
    fee = fee_pct / 100
    if risk <= 2 * quantity * entry * fee:
        raise ValueError("Stop loss budget must exceed estimated round-trip fees.")
    long_stop = (quantity * entry * (1 + fee) - risk) / (quantity * (1 - fee))
    short_stop = (quantity * entry * (1 - fee) + risk) / (quantity * (1 + fee))
    if long_stop <= 0:
        raise ValueError("Stop loss budget must be smaller than the long position value.")
    return {"Long": long_stop, "Short": short_stop}


@dataclass
class HedgeEngine:
    cash: float
    fee_pct: float
    target_sigma: float
    stop_loss_pct: float
    stop_loss_basis: str = "Position"
    entry_timeout_bars: int = 3
    minimum_order_size: float = 0.
    positions: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    plan: dict | None = None
    touching: bool = False
    pending_bars: int = 0
    last_time: str | None = None

    def __post_init__(self):
        for value in (self.cash, self.fee_pct, self.target_sigma, self.stop_loss_pct, self.minimum_order_size):
            if not math.isfinite(value):
                raise ValueError("Hedge parameters must be finite.")
        if self.cash < 0 or not 0 <= self.fee_pct < 100 or self.target_sigma <= 0 or self.stop_loss_pct < 0:
            raise ValueError("Invalid hedge capital, fee, target sigma or stop loss.")
        if self.stop_loss_basis not in {"Position", "Deposit"}:
            raise ValueError("Stop loss basis must be Position or Deposit.")
        if not isinstance(self.entry_timeout_bars, int) or self.entry_timeout_bars < 1:
            raise ValueError("Entry timeout must be at least one candle.")

    def snapshot(self):
        from dataclasses import asdict
        return asdict(self)

    def _close(self, side, price, timestamp, reason):
        position = self.positions.pop(side)
        quantity = position['quantity']
        gross = quantity * (price - position['entry_price']) * (1 if side == 'Long' else -1)
        exit_fee = quantity * price * self.fee_pct / 100
        pnl = gross - position['entry_fee'] - exit_fee
        self.cash += position['allocation'] + pnl
        self.history.append(dict(deposit_size=position['allocation'],side=side,
            entry_time=position['entry_time'],exit_time=str(timestamp),entry_price=position['entry_price'],
            exit_price=price,quantity=quantity,pnl=pnl,entry_fee=position['entry_fee'],exit_fee=exit_fee,return_pct=100*pnl/position['allocation'],exit_reason=reason))

    def _stop(self, side, row, entry_bar=False):
        position = self.positions[side]
        stop = position['stop_price']
        if stop is None:
            return False
        touched = row.low <= stop if side == 'Long' else row.high >= stop
        if touched:
            price = stop if entry_bar else (min(float(row.open), stop) if side == 'Long' else max(float(row.open), stop))
            self._close(side, price, row.time, 'Stop loss')
            return True
        return False

    def process(self, row, vwap, std, *, allow_new=True):
        timestamp = pd.Timestamp(row.time).isoformat()
        if self.last_time is not None and pd.Timestamp(timestamp) <= pd.Timestamp(self.last_time):
            return False
        self.last_time = timestamp
        active_at_start = bool(self.positions or self.pending)
        exited = False
        for side in list(self.positions):
            if self._stop(side,row):
                exited = True
                continue
            target = self.positions[side]['take_profit']
            if (side == 'Long' and row.high >= target) or (side == 'Short' and row.low <= target):
                price = max(float(row.open),target) if side == 'Long' else min(float(row.open),target)
                self._close(side,price,row.time,'Reached hedge TP band')
                exited = True
        if exited:
            self.pending.clear()
        for side, order in list(self.pending.items()):
            entry = order['entry_price']
            if not (row.low <= entry if side == 'Long' else row.high >= entry):
                continue
            price = min(float(row.open),entry) if side == 'Long' else max(float(row.open),entry)
            quantity = min(order['quantity'], order['allocation'] / (price * (1 + self.fee_pct / 100)))
            allocation = quantity * price * (1 + self.fee_pct / 100)
            stops = stop_price(price,quantity,allocation,self.plan['deposit'],self.stop_loss_pct,self.stop_loss_basis,self.fee_pct)
            self.cash -= allocation
            self.positions[side] = dict(order,entry_price=price,allocation=allocation,entry_fee=quantity*price*self.fee_pct/100,
                                        entry_time=timestamp,stop_price=stops[side])
            del self.pending[side]
            self._stop(side,row,entry_bar=True)
        if active_at_start:
            self.pending_bars += 1
            if self.pending_bars >= self.entry_timeout_bars:
                self.pending.clear()
        if self.plan and not self.positions and not self.pending:
            self.plan = None
        valid = pd.notna(vwap) and pd.notna(std) and vwap > 0 and std > 0
        touched = bool(valid and row.low <= vwap <= row.high)
        if allow_new and touched and not self.touching and not active_at_start and not self.positions and not self.pending:
            allocation = self.cash / 2
            if allocation >= self.minimum_order_size and allocation > 0:
                if vwap - self.target_sigma * std <= 0:
                    raise ValueError("Short TP band must be positive.")
                quantity = allocation / (vwap * (1 + self.fee_pct / 100))
                stops = stop_price(vwap,quantity,allocation,self.cash,self.stop_loss_pct,self.stop_loss_basis,self.fee_pct)
                legs = {side:dict(entry_price=float(vwap),quantity=quantity,allocation=allocation,
                    take_profit=float(vwap + (1 if side=='Long' else -1)*self.target_sigma*std),stop_price=stops[side])
                    for side in ('Long','Short')}
                self.plan = dict(id=timestamp,created_at=timestamp,deposit=self.cash,legs=legs,
                                 entry_timeout_bars=self.entry_timeout_bars,fee_pct=self.fee_pct)
                self.pending = {side:dict(order) for side,order in legs.items()}
                self.pending_bars = 0
        self.touching = touched
        return True

    def equity(self, price):
        return self.cash + sum(p['allocation'] - p['entry_fee'] + p['quantity']*(price-p['entry_price'])*(1 if side=='Long' else -1)
                               for side,p in self.positions.items())

    def close_all(self, price, timestamp, reason):
        for side in list(self.positions):
            self._close(side,float(price),timestamp,reason)
        self.pending.clear()
        self.plan = None

    def trades(self):
        return [Trade(**dict({k:v for k,v in t.items() if k in Trade.__dataclass_fields__},entry_time=pd.Timestamp(t['entry_time']),exit_time=pd.Timestamp(t['exit_time']))) for t in self.history]


def hedge_backtest(data, initial_capital, fee_pct, target_sigma, stop_loss_pct, stop_loss_basis,
                   entry_timeout_bars, minimum_order_size, scheduled_days=None):
    engine = HedgeEngine(initial_capital,fee_pct,target_sigma,stop_loss_pct,stop_loss_basis,entry_timeout_bars,minimum_order_size)
    rows=[]
    for i,row in enumerate(data.itertuples(index=False)):
        session_end = scheduled_days is not None and (i==len(data)-1 or scheduled_days.iloc[i+1]!=scheduled_days.iloc[i])
        engine.process(row,float(row.order_vwap),float(row.order_std),allow_new=not session_end)
        if session_end:
            engine.close_all(row.close,row.time,'End of scheduled session')
            engine.touching=False
        if i==len(data)-1:
            engine.close_all(row.close,row.time,'End of strategy')
        rows.append(dict(time=row.time,equity=engine.equity(row.close),
            position=sum(p['quantity']*(1 if s=='Long' else -1) for s,p in engine.positions.items()),
            close=row.close,anchored_vwap=row.anchored_vwap))
    return pd.DataFrame(rows),engine.trades()


def process_hedge_history(state,candles,config,launched_at,fee_pct):
    """Advance a persisted paper hedge ledger without end-of-backtest liquidation."""
    from shared.indicators import add_launch_weekly_anchored_vwap
    anchor = pd.Timestamp(launched_at) - pd.Timedelta(days=config.anchor_before_days if config.anchor_before_strategy_start else 0)
    data = add_launch_weekly_anchored_vwap(candles,anchor,config.vwap_anchor_reset_weeks)
    data['order_vwap']=data.anchored_vwap.shift(1)
    data['order_std']=data.anchored_std.shift(1)
    engine=HedgeEngine(**state.hedge_state) if state.hedge_state else HedgeEngine(config.initial_capital,fee_pct,
        config.close_order_vwap_sigma,config.stop_loss_pct,config.stop_loss_basis,config.hedge_entry_timeout_bars,config.minimum_order_size)
    desired = dict(target_sigma=config.close_order_vwap_sigma, stop_loss_pct=config.stop_loss_pct,
                   stop_loss_basis=config.stop_loss_basis, entry_timeout_bars=config.hedge_entry_timeout_bars,
                   minimum_order_size=config.minimum_order_size)
    if any(getattr(engine,k)!=v for k,v in desired.items()):
        if engine.positions or engine.pending:
            raise RuntimeError("Cannot change hedge risk/TP settings during an active cycle.")
        for key,value in desired.items():
            setattr(engine,key,value)
    processed=0
    for row in data.itertuples(index=False):
        if row.time < launched_at:
            continue
        if engine.process(row,float(row.order_vwap),float(row.order_std)):
            processed+=1
            state.last_processed_candle=pd.Timestamp(row.time).isoformat()
    state.hedge_state=engine.snapshot()
    return processed
