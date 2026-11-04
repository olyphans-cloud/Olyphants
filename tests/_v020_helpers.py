"""Pembantu tes v0.2.0: engine dengan strategi stub dan jalur harga buatan (hasil bisa dihitung manual)."""
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from btc_robot.audit import AuditLog
from btc_robot.config import Config, Mode, config_from_dict, config_hash, validate
from btc_robot.engine import Engine
from btc_robot.execution import SimulatedExecutionAdapter
from btc_robot.ledger import Ledger
from btc_robot.models import Candle, Side, Signal
from btc_robot.risk import RiskEngine

HOUR = 3_600_000
MIN = 60_000
DAY = 86_400_000
T0 = 1_700_000_000_000 // DAY * DAY  # 00:00 UTC


def base_cfg(interval: str = "1h", **risk) -> Config:
    r = {"max_data_age_seconds": 900, "min_stop_distance_pct": 0.05}
    r.update(risk)
    return validate(config_from_dict({"mode": "BACKTEST", "interval": interval, "risk": r}))


def flat_bars(n: int, itv: int, price: float = 100.0, wick: float = 0.05, start: int = T0) -> List[Candle]:
    return [Candle(start + i * itv, price, price + wick, price - wick, price, 100.0) for i in range(n)]


def with_bar(bars: List[Candle], i: int, o: float, h: float, l: float, c: float) -> None:
    bars[i] = Candle(bars[i].ts, o, h, l, c, 100.0)


class StubStrategy:
    """Mengeluarkan LONG pada indeks tertentu: plan = {indeks: (stop_pct, tp_pct)}."""
    VERSION = "stub/1.0"

    def __init__(self, plan: Dict[int, Tuple[float, float]]):
        self.plan, self._c = plan, []

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._c = candles

    def signal_at(self, i: int) -> Signal:
        c = self._c[i]
        if i in self.plan:
            stop, tp = self.plan[i]
            return Signal(c.ts, Side.LONG, "stub", c.close, c.close * (1 - stop / 100),
                          c.close * (1 + tp / 100), None, self.VERSION, "fp")
        return Signal(c.ts, Side.NO_TRADE, "stub_none", c.close, None, None, None, self.VERSION, "fp")


def make_engine(cfg: Config, plan, candles, mode: Mode = Mode.BACKTEST, spread_provider=None,
                entry_gate=None, adapter_cls=None) -> Engine:
    risk = RiskEngine(cfg, mode)
    adapter = (adapter_cls or SimulatedExecutionAdapter)(cfg.costs, mode, risk.verify)
    strat = StubStrategy(plan)
    strat.prepare(candles)
    return Engine(cfg, mode, strat, risk, adapter, Ledger(cfg.initial_equity), AuditLog(None, config_hash(cfg)),
                  spread_provider=spread_provider, entry_gate=entry_gate)


def run_all(engine: Engine, candles: Sequence[Candle]) -> None:
    for i in range(len(candles)):
        engine.process_bar(candles, i)
    engine.finalize(candles)
