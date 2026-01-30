import asyncio
import threading
import logging
from math import ceil
from typing import cast, Optional
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
from uuid import uuid4

# Alpaca SDK
from alpaca.trading import (
    TradingClient, TradingStream,
    MarketOrderRequest, TrailingStopOrderRequest, ReplaceOrderRequest, GetOrdersRequest,
    Position, Order, Asset, AssetClass, Clock, TradeAccount,
    OrderSide, PositionSide, TimeInForce, QueryOrderStatus,
    TradeUpdate, TradeEvent
)

from alpaca.data import (
    StockHistoricalDataClient,
    StockBarsRequest, TimeFrame,
    BarSet, Bar
)
from alpaca.data.live.stock import StockDataStream

logger = logging.getLogger(__name__)

# Constants
YEAR = 252
WEEK = 5
MONTH = 21
HOURS_PER_DAY = 6.5

def days_to_bars(timeframe: timedelta, n=1):
    hours = timeframe.total_seconds() / 3600
    return HOURS_PER_DAY / hours * n

def bars_to_days(timeframe: timedelta, n=1):
    return n / days_to_bars(timeframe)

from enum import Enum
from dataclasses import dataclass

class Strategy(str, Enum):
    MEAN_REVERSION = "MEAN_REVERSION"
    TREND_FOLLOWING = "TREND_FOLLOWING"

@dataclass
class TradeState:
    symbol: str
    strategy: Strategy
    side: OrderSide
    qty: float
    client_uid: str
    trailing_apca_id: Optional[str] = None

class ParsedClientIdType(str, Enum):
    ENTRY = "ENTRY"
    TRAIL = "TRAIL"
    
@dataclass
class ParsedClientId:
    type: ParsedClientIdType
    strategy: Strategy
    uid: str
    with_trailing_stop: Optional[bool] = None
    parent_uid: Optional[str] = None
    
class HybridStrategy:
    VERSION = "HSv1"
    
    def _entry_cid(self, strategy: Strategy, with_trailing_stop: bool) -> str:
        uid = uuid4().hex[:12]
        return "::".join((self.VERSION, ParsedClientIdType.ENTRY, strategy, uid, str(int(with_trailing_stop))))

    def _trail_cid(self, strategy: Strategy, parent_uid: str) -> str:
        uid = uuid4().hex[:12]
        return "::".join((self.VERSION, ParsedClientIdType.TRAIL, strategy, uid, parent_uid))
    
    def _parse_cid(self, cid: str):
        if not cid.startswith(self.VERSION):
            raise ValueError(f"unsupported client order ID version: {cid}")
        parts = cid.split("::")
        if len(parts) < 4:
            raise ValueError(f"invalid client order ID format: {cid}")
        
        parsed = ParsedClientId(
            type=ParsedClientIdType(parts[1]),
            strategy=Strategy(parts[2]),
            uid=parts[3]
        )
        if parsed.type == ParsedClientIdType.ENTRY:
            parsed.with_trailing_stop = parts[4] == "1"
        elif parsed.type == ParsedClientIdType.TRAIL:
            parsed.parent_uid = parts[4]
        return parsed
        
    def __init__(self,
                 api_key: str,
                 secret_key: str,
                 trade_symbol: str,
                 timeframe=timedelta(hours=1), 
                 band_std_dev = 0.8,
                 vol_z_window = 21,
                 vol_z_entry_threshold = 0.0,
                 atr_period = ceil(bars_to_days(timedelta(hours=1), WEEK)),
                 atr_multiplier = 3.0,
                 fast_ma_window = ceil(bars_to_days(timedelta(hours=1), WEEK)),
                 slow_ma_window = ceil(bars_to_days(timedelta(hours=1), MONTH)),
                 mr_exposure: float = 0.1,
                 tf_exposure: float = 0.1,
                 long_only=False, paper=True, delayed_backfill=True,
                 vol_symbol:Optional[str]=None, pos_symbol: Optional[str]=None):

        self.curr_state: Optional[TradeState] = None
        self.lock = threading.RLock()
        
        # Clients
        self.trading_client = TradingClient(api_key, secret_key, paper=paper)
        self.trading_stream = TradingStream(api_key, secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, secret_key)
        self.data_stream = StockDataStream(api_key, secret_key)
        
        self.pos_symbol = pos_symbol or trade_symbol # for example we trade BTC/USD but the position is in BTCUSD
        self.vol_symbol = vol_symbol or trade_symbol
        self.asset = cast(Asset, self.trading_client.get_asset(trade_symbol))
        if not self.asset.tradable:
            raise ValueError(f"Asset {self.asset.symbol} is not tradable.")
        
        self.long_only = long_only
        if not long_only and not self.asset.shortable:
            self.long_only = True
            logger.info(f"Asset {self.asset.symbol} is not shortable; forcing long-only mode.")
            
        # Strategy Params
        self.exposures = {
            Strategy.MEAN_REVERSION: mr_exposure,
            Strategy.TREND_FOLLOWING: tf_exposure
        }
        self.band_std_dev = band_std_dev
        self.vol_z_window = vol_z_window
        self.vol_z_entry_threshold = vol_z_entry_threshold
        self.atr_period, self.atr_multiplier = atr_period, atr_multiplier
        self.fast_ma_window, self.slow_ma_window = fast_ma_window, slow_ma_window
        
        # Timeframe Handling
        self.timeframe = timeframe
        self.tf_minutes = int(timeframe.total_seconds() / 60)
        self.warmup_lookback = ceil(max(7, bars_to_days(timeframe, atr_period), bars_to_days(timeframe, slow_ma_window)))+1
        self.max_buf = int(max(atr_period, slow_ma_window) * self.tf_minutes * 1.2)
        
        self.minute_bar_count = 0
        self.delayed_backfill = delayed_backfill
        
        # Data Buffers
        self.minute_buffer = pd.DataFrame() # Raw 1m bars
        self.history = pd.DataFrame()       # Resampled target bars
        self.vol_history = pd.DataFrame()
        
        # Loop Control
        self.is_ready = False
        self.last_bar_time: Optional[pd.Timestamp] = None
        self.shutdown = asyncio.Event()
        self.shutdown_ts = asyncio.Event()
        
        # Indicators
        self.day_open = 0.0
        self.curr_bar: Optional[pd.Series] = None
        self.last_bar: Optional[pd.Series] = None
        self.latest_vol_z = 0.0
        self.latest_vol_d = 0.0
        self.latest_fast_ma = 0.0
        self.latest_slow_ma = 0.0
        self.latest_atr = 0.0
        
        self.entry_signal: Optional[tuple[OrderSide, Strategy, bool]] = None
        self.exit_signal: bool = False
        
        # Refresh tracking
        self.last_vol_refresh_time: Optional[datetime] = None

    # === STREAM MANAGEMENT ===
    
    async def run(self):
        self.rebuild_state()
        logger.info(f"starting stream for {self.asset.symbol}")
        
        self.data_stream.subscribe_bars(self.handle_minute_bar, self.asset.symbol) # type: ignore
        self.trading_stream.subscribe_trade_updates(self.handle_trade_updates)
        
        self.fetch_vol_from_source()
        clock = cast(Clock, self.trading_client.get_clock())
        if not self.delayed_backfill or not clock.is_open:
            self.fetch_backfill()
        else:
            logger.info("delayed backfill enabled; waiting for 15 mins of data")
        
        try:
            await asyncio.gather(
                self.data_stream._run_forever(),
                self.trading_stream._run_forever()
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self):
        if self.data_stream:
            await self.data_stream.stop_ws()
        if self.trading_stream:
            await self.trading_stream.stop_ws()
        self.shutdown.set()
        
    # === VOLATILITY DATA MANAGEMENT ===
    
    def fetch_vol_from_source(self):
        query = "SELECT DATE_ADD(date, INTERVAL 1 DAY) as timestamp, iv_current as vol FROM volatility_history WHERE act_symbol = '%s' ORDER BY date DESC LIMIT %d"
        try:
            owner, database, branch = "post-no-preference", "options", "master"
            res = requests.get(
                f"https://www.dolthub.com/api/v1alpha1/{owner}/{database}/{branch}",
                params={"q": query % (self.vol_symbol, self.vol_z_window * 3) }
            )
            data: dict = res.json()
            if (data["query_execution_status"] != "Success"):
                raise ValueError(data["query_execution_message"])
            rows = data.get("rows", [])
            if rows:
                with self.lock:
                    self.vol_history = pd.DataFrame(rows, columns=["timestamp", "vol"]).set_index("timestamp")
                    self.vol_history.index = pd.to_datetime(self.vol_history.index).tz_localize("America/New_York")
                    self.calc_vol_stats()
                logger.info(f"volatility history updated. current_vol_z={self.latest_vol_z:.2f}")
        except Exception as e:
            logger.error(f"failed to fetch volatility: {e}")
            
    def refresh_vol_data(self):
        now = datetime.now()
        refresh_threshold = now.replace(hour=6, minute=45, second=0, microsecond=0)
        if now >= refresh_threshold:
            if self.last_vol_refresh_time is None or self.last_vol_refresh_time < refresh_threshold:
                self.fetch_vol_from_source()
                self.last_vol_refresh_time = now

    def calc_vol_stats(self):
        with self.lock:
            if len(self.vol_history) < 2: return
            self.vol_history["vol_d"] = pd.to_numeric(self.vol_history["vol"], errors="coerce").ffill() * np.sqrt(1 / YEAR)
            self.vol_history["vol_ma"] = self.vol_history["vol_d"].ewm(span=self.vol_z_window, adjust=True).mean()
            self.vol_history["vol_sd"] = self.vol_history["vol_d"].ewm(span=self.vol_z_window, adjust=True).std()
            self.vol_history["vol_z"] = (self.vol_history["vol_d"] - self.vol_history["vol_ma"]) / (self.vol_history["vol_sd"] + 0.01)
            self.latest_vol_z = float(self.vol_history["vol_z"].iloc[-1])
            self.latest_vol_d = float(self.vol_history["vol_d"].iloc[-1])

    # === DATA INGESTION & AGGREGATION ===
    
    def fetch_backfill(self):
        logger.info("backfilling historical 1m bars")
        start_dt = datetime.now() - timedelta(days=self.warmup_lookback)
        req = StockBarsRequest(
            symbol_or_symbols=self.asset.symbol, 
            timeframe=TimeFrame.Minute, # type: ignore
            start=start_dt
        )
        bars = cast(BarSet, self.data_client.get_stock_bars(req))
        fetched_df = (
            bars.df.loc[self.asset.symbol]
            .reset_index()
            [["timestamp","open","high","low","close","volume"]]
            .set_index("timestamp")
            .tz_convert("America/New_York")
        )
        
        with self.lock:
            buf = pd.concat([fetched_df, self.minute_buffer]).drop_duplicates().sort_index()
            self.minute_buffer = buf
            daily_resample = buf.resample("1D").agg({"open": "first"})
            if not daily_resample.empty:
                self.day_open = daily_resample.iloc[-1]["open"]
            self.resample_and_sync()
            self.evaluate_signals()
            self.is_ready = True
            logger.info(f"warmup complete. buffer size: {len(self.minute_buffer)} mins.")

    async def handle_minute_bar(self, bar: Bar):
        self.minute_bar_count += 1
        
        if self.minute_bar_count == 15 and self.delayed_backfill:
            self.fetch_backfill()
        
        new_row = pd.DataFrame([bar.model_dump()]).set_index("timestamp").tz_convert("America/New_York")
        with self.lock:
            self.minute_buffer = pd.concat([self.minute_buffer, new_row]).sort_index()
            self.minute_buffer.index = (
                pd.to_datetime(self.minute_buffer.index)
            )
            if len(self.minute_buffer) > self.max_buf:
                self.minute_buffer = self.minute_buffer.iloc[-self.max_buf:]
        if self.is_ready:
            self.resample_and_sync()

    def resample_and_sync(self):
        if self.minute_buffer.empty: return

        resampled = (
            self.minute_buffer
            .resample(self.timeframe, label="left", closed="left")
            .agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum"
            })
            .dropna()
            .sort_index()
        )

        if resampled.empty: return
        latest_idx: pd.Timestamp = resampled.index[-1]
        latest_tick_time: pd.Timestamp = self.minute_buffer.index[-1]
        
        with self.lock:
            self.minute_buffer = self.minute_buffer.tz_convert("America/New_York")
            self.day_open = self.minute_buffer["open"].resample("D").first()
            if (latest_tick_time.minute % self.tf_minutes) == (self.tf_minutes - 1):
                self.on_bar_closed(resampled.iloc[-1])
                
            self.history = resampled
            self.calc_indicators()
            self.last_bar_time = latest_idx

    def calc_indicators(self):
        with self.lock:
            if len(self.history) < self.slow_ma_window + 1: return
            self.history["tr"] = np.maximum(self.history["high"] - self.history["low"], 
                                np.maximum(np.abs(self.history["high"] - self.history["close"].shift(1)), 
                                            np.abs(self.history["low"] - self.history["close"].shift(1))))
            self.history["atr"] = self.history["tr"].rolling(window=self.atr_period).mean()
            self.history["fast_ma"] = self.history["close"].ewm(span=self.fast_ma_window, adjust=True).mean()
            self.history["slow_ma"] = self.history["close"].ewm(span=self.slow_ma_window, adjust=True).mean()
            
            self.latest_atr = float(self.history["atr"].iloc[-1])
            self.latest_fast_ma = float(self.history["fast_ma"].iloc[-1])
            self.latest_slow_ma = float(self.history["slow_ma"].iloc[-1])
            self.latest_lower_band = self.day_open - self.band_std_dev * self.latest_atr
            self.latest_upper_band = self.day_open + self.band_std_dev * self.latest_atr
            
    def on_bar_closed(self, bar: pd.Series):
        self.last_bar = self.curr_bar
        self.curr_bar = bar
        self.refresh_vol_data()
        logger.info(f"bar closed at %s | close=%.2f | vol_z=%.2f vol_regime=%s | fast_ma=%.2f slow_ma=%.2f trend=%s",
                    cast(pd.Timestamp, bar.name).strftime('%Y-%m-%d %H:%M %Z'),
                    bar["close"],
                    self.latest_vol_z, "high" if self.latest_vol_z >= self.vol_z_entry_threshold else "low",
                    self.latest_fast_ma, self.latest_slow_ma, "up" if self.latest_fast_ma > self.latest_slow_ma else "down")
        self.evaluate_signals()
        self.execute_trades()

    # === SIGNAL EXECUTION ===
    
    def evaluate_signals(self):
        with self.lock:
            self.exit_signal = False
            self.entry_signal = None

            if self.last_bar is None or self.curr_bar is None:
                return
            
            high_vol = self.latest_vol_z >= self.vol_z_entry_threshold
            
            if self.curr_state:
                # exit trend following trades if we are in a high vol regime
                if high_vol and self.curr_state.strategy == Strategy.TREND_FOLLOWING:
                    self.exit_signal = True
                # don't enter new trades if we already have a position
                return
            
            # if in high vol regime look for mean reversion entries
            if high_vol:
                if self.last_bar["low"] < self.latest_lower_band and self.curr_bar["low"] > self.latest_lower_band:
                    self.entry_signal = (OrderSide.BUY, Strategy.MEAN_REVERSION, True)
                elif self.last_bar["high"] > self.latest_upper_band and self.curr_bar["high"] < self.latest_upper_band:
                    self.entry_signal = (OrderSide.SELL, Strategy.MEAN_REVERSION, True)
            # otherwise look for trend following entries in low vol regime
            else:
                if self.latest_fast_ma > self.latest_slow_ma:
                    self.entry_signal = (OrderSide.BUY, Strategy.TREND_FOLLOWING, False)
                elif self.latest_fast_ma < self.latest_slow_ma:
                    self.entry_signal = (OrderSide.SELL, Strategy.TREND_FOLLOWING, False)

    def execute_trades(self):
        try:
            self.handle_exit()
            self.update_trailing_stop_order()
            self.handle_entry()
        except Exception as e:
            logger.error(f"execution error: {e}")

    def handle_entry(self):
        if not self.entry_signal or self.curr_bar is None:
            return
        side, strategy, with_trailing_stop = self.entry_signal
        if self.long_only and side == OrderSide.SELL:
            logger.info("long-only mode; skipping short entry")
            return
        self.submit_entry_order(side, strategy, with_trailing_stop, self.curr_bar["close"])
        self.entry_signal = None
        
    def handle_exit(self):
        if not self.exit_signal:
            return
        self.trading_client.close_all_positions(cancel_orders=True)
        self.exit_signal = False
        
    def submit_entry_order(self, side: OrderSide, strategy: Strategy, with_trailing_stop: bool, price: float):
        account = cast(TradeAccount, self.trading_client.get_account())
        equity = float(account.equity or 0)
        exposure = self.exposures[strategy]
        qty = (equity * exposure) / price

        if qty <= 0:
            logger.warning(f"{equity=}, {qty=}; skipping entry")
            return

        cid = self._entry_cid(strategy, with_trailing_stop)
        self.trading_client.close_all_positions(cancel_orders=True)
        self.trading_client.submit_order(MarketOrderRequest(
            symbol=self.asset.symbol,
            qty=int(qty) if self.asset.asset_class == AssetClass.US_EQUITY else qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            client_order_id=cid
        ))

    # === TRAILING STOP LOSS ===
    
    def submit_trailing_stop_order(self, trail_price: Optional[float] = None):
        if not self.curr_state:
            logger.warning("no active trade state; cannot create trailing stop")
            return
        
        if trail_price is None:
            trail_price = self.latest_atr * self.atr_multiplier
        
        if trail_price <= 0:
            logger.warning("trail_price <= 0; cannot create trailing stop")
            return
        
        cid = self._trail_cid(self.curr_state.strategy, self.curr_state.client_uid)
        order = cast(Order, self.trading_client.submit_order(TrailingStopOrderRequest(
            symbol=self.asset.symbol,
            qty=self.curr_state.qty,
            side=OrderSide.SELL if self.curr_state.side == OrderSide.BUY else OrderSide.BUY,
            trail_price=trail_price,
            time_in_force=TimeInForce.GTC,
            client_order_id=cid,
        )))
        self.curr_state.trailing_apca_id = str(order.id)
        logger.info(f"created trailing stop order with trail price: {trail_price:.2f}")
        
    def update_trailing_stop_order(self, trail_price: Optional[float] = None):
        if not self.curr_state or not self.curr_state.trailing_apca_id:
            return
        
        if trail_price is None:
            trail_price = self.latest_atr * self.atr_multiplier
        
        if trail_price <= 0:
            logger.warning("trail_price <= 0; cannot update trailing stop")
            return
        
        trail_amount = self.latest_atr * self.atr_multiplier
        self.trading_client.replace_order_by_id(
            self.curr_state.trailing_apca_id,
            ReplaceOrderRequest(trail=trail_amount)
        )
        logger.info(f"updated trailing stop to new trail price: {trail_amount:.2f}")
    
    # === TRADE DATA STREAM HANDLERS ===
    
    def has_open_position(self) -> bool:
        try:
            pos = cast(Position, self.trading_client.get_open_position(self.pos_symbol))
            return abs(float(pos.qty)) > 0
        except Exception:
            return False
        
    def rebuild_state(self):
        try:
            pos: Position = self.trading_client.get_open_position(self.pos_symbol) # type: ignore
        except Exception:
            logger.info("no open position found")
            return
        
        # find order that opened the position
        orders = cast(list[Order], self.trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            side=OrderSide.BUY if pos.side == PositionSide.LONG else OrderSide.SELL,
            symbols=[self.asset.symbol],
        )))
        for o in orders:
            if not o.side or not o.qty:
                continue
            try:
                parsed = self._parse_cid(o.client_order_id)
            except:
                continue
            if parsed.type == ParsedClientIdType.ENTRY:
                self.curr_state = TradeState(
                    symbol=self.asset.symbol,
                    strategy=parsed.strategy,
                    side=o.side,
                    qty=abs(float(o.qty)),
                    client_uid=parsed.uid,
                    trailing_apca_id=None
                )
                break
            
        if self.curr_state is None:
            logger.warning("no matching entry order for open position")
            return

        # find existing trailing stop order
        orders = cast(list[Order], self.trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            side=OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY,
            symbols=[self.asset.symbol],
        )))
        for o in orders:
            try:
                parsed = self._parse_cid(o.client_order_id)
            except:
                continue
            if parsed.type == ParsedClientIdType.TRAIL and parsed.parent_uid == self.curr_state.client_uid:
                self.curr_state.trailing_apca_id = str(o.id)
                break
        logger.info(f"reconstructed trade state: {self.curr_state}")
        
    async def handle_trade_updates(self, data: TradeUpdate):
        if data.event == TradeEvent.FILL:
            self.on_fill(data.order)
        
    def on_fill(self, order: Order):
        if order.symbol != self.asset.symbol:
            return
        if not order.symbol or not order.side or not order.qty or not order.id:
            logger.error("incomplete order data; cannot process fill")
            return
        
        logger.info(f"{order.client_order_id} order filled: {order.side} {order.qty} {order.symbol} @ {order.filled_avg_price}")
        if self.curr_state and not self.has_open_position():
            logger.info("position closed; clearing state")
            self.curr_state = None
        
        try:
            parsed = self._parse_cid(order.client_order_id)
        except ValueError:
            return
        

        self.curr_state = TradeState(
            symbol=order.symbol,
            strategy=parsed.strategy,
            side=order.side,
            qty=int(order.qty),
            client_uid=parsed.uid,
        )
        if parsed.with_trailing_stop:
            self.submit_trailing_stop_order()
