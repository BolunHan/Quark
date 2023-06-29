import datetime
import enum
from types import SimpleNamespace

from AlgoEngine.Engine import MarketDataMonitor, PositionManagementService
from PyQuantKit import MarketData, TransactionSide, TradeInstruction, OrderState

from . import STRATEGY_ENGINE
from .metric import StrategyMetric
from .data_core import IndexWeight


class DecisionCore(object):
    def __init__(self):
        pass

    def update_factor(self, factor: dict[str, float], timestamp: float) -> int:
        return 0

    def trade_volume(self, position: PositionManagementService, cash: float, margin: float, timestamp: float, signal: int) -> float:
        return 1.


class StrategyStatus(enum.Enum):
    error = -1
    idle = 0
    working = 1
    closing = 2
    closed = 3


class Strategy(object):
    def __init__(
            self,
            index_ticker: str = "000016.SH",
            index_weights: dict[str, float] = None,
            strategy_engine=None,
            metric: StrategyMetric = None,
            **kwargs
    ):
        self.index_ticker = index_ticker
        self.index_weights = IndexWeight(index_name=self.index_ticker, **index_weights)
        self.engine = strategy_engine if strategy_engine is not None else STRATEGY_ENGINE
        self.position_tracker = self.engine.position_tracker
        self.strategy_metric = metric if metric is not None else StrategyMetric(sample_interval=1, index_weights=self.index_weights)
        self.mds = self.engine.mds
        self.monitors: dict[str, MarketDataMonitor] = {}
        self.mode = kwargs.pop('mode', 'production')

        self.status = StrategyStatus.idle
        self.subscription = self.engine.subscription
        self.eod_status = {'last_unwind_timestamp': 0., 'retry_count': -1, 'status': 'idle', 'retry_interval': 30.}
        self.decision_core = DecisionCore()

        self.profile = SimpleNamespace(
            clear_on_eod=True,
        )

        self._last_update_ts = 0.
        self._sampling_interval = 5.

    def get_underlying(self, ticker: str, side: int):
        return ticker

    def register(self):
        from .data_core import register_monitor
        self.engine.multi_threading = False
        self.monitors.update(register_monitor(index_name=self.index_ticker, index_weights=self.index_weights))
        self.engine.add_handler(on_market_data=self._on_market_data)
        self.engine.add_handler(_on_order=self._on_order)
        self.status = StrategyStatus.working
        return self.monitors

    def unwind_all(self):
        self.position_tracker.unwind_all()
        self.status = 'closing'
        self.eod_status['last_unwind_timestamp'] = self.mds.timestamp
        self.eod_status['status'] = 'working'
        self.eod_status['retry_count'] += 1
        self.eod_status['status'] = 'working'

    def _check_unwind(self):
        if not self.status == StrategyStatus.closing:
            return

        exposure = self.position_tracker.exposure_volume
        working = self.position_tracker.working_volume
        timestamp = self.mds.timestamp

        # Scenario 0: no exposure
        if not exposure:
            self.status = StrategyStatus.closed
            self.eod_status['status'] = 'done'
            return

        # Scenario 1: canceling unwinding orders
        eod_status = self.eod_status['status']
        if eod_status == 'canceling':
            # Scenario 1.1: all canceled
            if not working['Long'] and not working['Short']:
                self.unwind_all()
            # Scenario 1.2: still canceling
            else:
                pass
            return

        # Scenario 2: working unwinding orders
        last_unwind_timestamp = self.eod_status['last_unwind_timestamp']
        retry_interval = self.eod_status['retry_interval']
        if last_unwind_timestamp + retry_interval < timestamp:
            self.position_tracker.cancel_all()
            self.eod_status['status'] = 'canceling'
            return

    def _on_market_data(self, market_data: MarketData, **kwargs):
        market_time = market_data.market_time
        timestamp = market_data.timestamp

        if self.mode == 'sampling':
            if self._last_update_ts + self._sampling_interval > timestamp:
                return
            self._last_update_ts = timestamp // self._sampling_interval * self._sampling_interval

        # market_price = market_data.market_price
        # ticker = market_data.ticker

        # working condition 0: in working status
        if self.status == StrategyStatus.idle or self.status == StrategyStatus.closed or self.status == StrategyStatus.error:
            return
        elif self.status == StrategyStatus.closing:
            self._check_unwind()
            return

        # signal condition 1: in trade session
        if not self.mds.in_trade_session(market_time):
            return

        # signal condition 2: avoid market-closing auction
        if market_time.time() >= datetime.time(14, 55):
            self.status = 'closing'

            if self.profile.clear_on_eod:
                return self.unwind_all()
            return

        # Optional signal condition 3: only subscribed ticker
        # if ticker not in self.index_weights:
        #     return

        # all conditions passed, checking signal
        monitor_value = self.strategy_metric.collect_factors(monitors=self.monitors, timestamp=timestamp)
        signal = self.decision_core.update_factor(factor=monitor_value, timestamp=timestamp)
        self.strategy_metric.collect_signal(signal=signal, timestamp=timestamp)

        # long action
        if signal > 0:
            self.engine.open_pos(
                ticker=self.get_underlying(ticker=self.index_ticker, side=1),
                volume=self.decision_core.trade_volume(
                    position=self.position_tracker,
                    cash=0,
                    margin=0,
                    timestamp=timestamp,
                    signal=1
                ),
                side=TransactionSide.Buy_to_Long
            )
        # short action
        elif signal < 0:
            self.engine.open_pos(
                ticker=self.get_underlying(ticker=self.index_ticker, side=1),
                volume=self.decision_core.trade_volume(
                    position=self.position_tracker,
                    cash=0,
                    margin=0,
                    timestamp=timestamp,
                    signal=1
                ),
                side=TransactionSide.Sell_to_Short
            )

    def _on_order(self, order: TradeInstruction, **kwargs):
        if order.order_state == OrderState.Rejected:
            self.status = StrategyStatus.error
