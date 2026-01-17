import asyncio
import threading
import logging
from math import ceil
from typing import cast, Optional
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
import numpy as np
import pandas as pd
import requests

# Alpaca SDK
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models import BarSet, Bar
from alpaca.trading.models import Order, Position, Asset

logger = logging.getLogger(__name__)

# Constants
YEAR = 252
WEEK = 5
MONTH = 21
HOURS_PER_DAY = 6.5

from enum import Enum
class Strategy(str, Enum):
    MEAN_REVERSION = "MEAN_REVERSION"
    TREND_FOLLOWING = "TREND_FOLLOWING"

def days_to_bars(timeframe: pd.Timedelta, n=1):
    hours = timeframe.total_seconds() / 3600
    return HOURS_PER_DAY / hours * n

def bars_to_days(timeframe: pd.Timedelta, n=1):
    return n / days_to_bars(timeframe)

class HybridStrategy:
    def __init__(self, api_key: str, secret_key: str, dolt_db_uri: str|None, symbol: str, timeframe: pd.Timedelta, 
                 band_std_dev: float, vol_z_window: int, vol_z_entry_threshold: float,
                 atr_period: int, atr_multiplier: float, 
                 fast_ma_window: int, slow_ma_window: int,
                 mr_exposure: float = 0.1, tf_exposure: float = 0.1,
                 long_only=False, paper=True, delayed_backfill=True,
                 vol_symbol:Optional[str]=None):
        
        self.lock = threading.RLock()
        self.dolt_engine = create_engine(dolt_db_uri) if dolt_db_uri else None
        
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
        
        # Clients & Assets
        self.trading_client = TradingClient(api_key, secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, secret_key)
        self.stream = StockDataStream(api_key, secret_key)
        
        self.symbol = symbol
        self.vol_symbol = vol_symbol or symbol
        self.asset = cast(Asset, self.trading_client.get_asset(self.symbol))
        
        if not self.asset.tradable:
            raise ValueError(f"Asset {self.symbol} is not tradable.")
        self.long_only = long_only or not self.asset.easy_to_borrow
        
        # Timeframe Handling
        self.timeframe = timeframe
        self.tf_minutes = int(timeframe.total_seconds() / 60)
        self.warmup_lookback = ceil(max(7, bars_to_days(timeframe, atr_period), bars_to_days(timeframe, slow_ma_window)))+1
        
        self.bar_count = 0
        self.delayed_backfill = delayed_backfill
        self.shutdown = asyncio.Event()
        
        # State
        self.pos: Optional[Position] = None
        self.active_strategy: Optional[Strategy] = None
        
        # Data Buffers
        self.day_open: pd.Series = pd.Series()
        self.minute_buffer = pd.DataFrame() # Raw 1m bars
        self.history = pd.DataFrame()       # Resampled target bars
        self.vol_history = pd.DataFrame()
        
        # Loop Control
        self.is_ready = False
        self.last_bar_time: Optional[pd.Timestamp] = None

        # Indicators
        self.latest_vol_z = 0.0
        self.latest_vol_d = 0.0
        self.latest_fast_ma = 0.0
        self.latest_slow_ma = 0.0
        self.latest_atr = 0.0
        self.entry_signal: Optional[tuple[OrderSide, Strategy]] = None
        
        # Refresh tracking
        self.last_vol_refresh_time: Optional[datetime] = None

    def sync_database(self):
        if not self.dolt_engine: return
        logger.info("syncing dolt db")
        try:
            with self.dolt_engine.connect() as conn:
                conn.execute(text("CALL DOLT_PULL('origin', 'master');"))
        except Exception as e:
            logger.warning(f"DB Sync notice: {e}")

    def refresh_vol_data(self):
        now = datetime.now()
        refresh_threshold = now.replace(hour=6, minute=45, second=0, microsecond=0)

        if now >= refresh_threshold:
            if self.last_vol_refresh_time is None or self.last_vol_refresh_time < refresh_threshold:
                logger.info("Pulling fresh volatility data...")
                self.sync_database()
                self.fetch_vol_from_source()
                self.last_vol_refresh_time = now

    def fetch_vol_from_source(self):
        query = (
            text("SELECT DATE_ADD(date, INTERVAL 1 DAY) as timestamp, iv_current as vol FROM volatility_history WHERE act_symbol = :s ORDER BY date DESC LIMIT :l")
            .bindparams(s=self.vol_symbol, l=self.vol_z_window * 3)
        )
        try:
            if self.dolt_engine:
                with self.dolt_engine.connect() as conn:
                    rows = conn.execute(query).fetchall()
            else:
                owner, database, branch = "post-no-preference", "options", "master"
                res = requests.get(
                    f"https://www.dolthub.com/api/v1alpha1/{owner}/{database}/{branch}",
                    params={"q": query.compile(compile_kwargs={"literal_binds": True}).string}
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

    def calc_vol_stats(self):
        with self.lock:
            if len(self.vol_history) < 2: return
            self.vol_history["vol_d"] = pd.to_numeric(self.vol_history["vol"], errors="coerce").ffill() * np.sqrt(1 / YEAR)
            self.vol_history["vol_ma"] = self.vol_history["vol_d"].ewm(span=self.vol_z_window, adjust=True).mean()
            self.vol_history["vol_sd"] = self.vol_history["vol_d"].ewm(span=self.vol_z_window, adjust=True).std()
            self.vol_history["vol_z"] = (self.vol_history["vol_d"] - self.vol_history["vol_ma"]) / (self.vol_history["vol_sd"] + 0.01)
            self.latest_vol_z = float(self.vol_history["vol_z"].iloc[-1])
            self.latest_vol_d = float(self.vol_history["vol_d"].iloc[-1])

    def fetch_backfill(self):
        logger.info("backfilling historical 1m bars")
        start_dt = datetime.now() - timedelta(days=self.warmup_lookback)
        req = StockBarsRequest(
            symbol_or_symbols=self.symbol, 
            timeframe=TimeFrame.Minute, # type: ignore
            start=start_dt
        )
        bars = cast(BarSet, self.data_client.get_stock_bars(req))
        fetched_df = (
            bars.df.loc[self.symbol]
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
             
    def calc_indicators(self):
        with self.lock:
            if len(self.history) < self.slow_ma_window + 1: return
            self.history["tr"] = np.maximum(self.history["high"] - self.history["low"], 
                                np.maximum(np.abs(self.history["high"] - self.history["close"].shift(1)), 
                                            np.abs(self.history["low"] - self.history["close"].shift(1))))
            self.history["atr"] = self.history["tr"].rolling(window=self.atr_period).mean()
            self.latest_atr = float(self.history["atr"].iloc[-1])

            self.history["fast_ma"] = self.history["close"].ewm(span=self.fast_ma_window, adjust=True).mean()
            self.history["slow_ma"] = self.history["close"].ewm(span=self.slow_ma_window, adjust=True).mean()
            
            self.latest_fast_ma = float(self.history["fast_ma"].iloc[-1])
            self.latest_slow_ma = float(self.history["slow_ma"].iloc[-1])

    async def handle_minute_bar(self, bar: Bar):
        self.bar_count += 1
        logger.info(f"received {bar.timestamp.strftime('%H:%M')} bar")
        
        if self.bar_count == 15 and self.delayed_backfill:
            self.fetch_backfill()
        
        new_row = pd.DataFrame([bar.model_dump()]).set_index("timestamp").tz_convert("America/New_York")
        with self.lock:
            self.minute_buffer = pd.concat([self.minute_buffer, new_row]).sort_index()
            self.minute_buffer.index = (
                pd.to_datetime(self.minute_buffer.index)
            )
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

    async def run(self):
        self.sync_database()
        logger.info(f"starting background stream for {self.symbol}")
        self.stream.subscribe_bars(self.handle_minute_bar, self.symbol) # type: ignore
        
        self.fetch_active_position()
        self.fetch_vol_from_source()
        if not self.delayed_backfill:
            self.fetch_backfill()
        else:
            logger.info("delayed backfill enabled; waiting for 15 mins of data")
        
        try:
            await self.stream._run_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self):
        if self.stream:
            await self.stream.stop_ws()
        self.shutdown.set()

    def evaluate_signals(self):
        with self.lock:
            self.entry_signal = None
            if len(self.history) < 2: return
            
            curr_bar = self.history.iloc[-1]
            last_bar = self.history.iloc[-2]
            
            upper = self.day_open.iloc[-1] + (self.day_open.iloc[-1] * self.latest_vol_d * self.band_std_dev)
            lower = self.day_open.iloc[-1] - (self.day_open.iloc[-1] * self.latest_vol_d * self.band_std_dev)
            
            if self.latest_vol_z >= self.vol_z_entry_threshold:
                if last_bar["low"] < lower and curr_bar["low"] > lower:
                    self.entry_signal = (OrderSide.BUY, Strategy.MEAN_REVERSION)
                elif last_bar["high"] > upper and curr_bar["high"] < upper:
                    self.entry_signal = (OrderSide.SELL, Strategy.MEAN_REVERSION)
            else:
                if self.latest_fast_ma > self.latest_slow_ma:
                    self.entry_signal = (OrderSide.BUY, Strategy.TREND_FOLLOWING)
                elif self.latest_fast_ma < self.latest_slow_ma:
                    self.entry_signal = (OrderSide.SELL, Strategy.TREND_FOLLOWING)

    def on_bar_closed(self, bar: pd.Series):
        logger.info(f"bar closed at {cast(pd.Timestamp, bar.name).strftime('%Y-%m-%d %H:%M')}")
        self.refresh_vol_data()
        self.evaluate_signals()
        self.execute_trades(bar)

    def execute_trades(self, bar: pd.Series):
        try:
            self.update_trailing_stop()
            if self.entry_signal is None: return
    
            side, strategy = self.entry_signal
            if self.long_only and side == OrderSide.SELL:
                logger.info("ignored short signal")
                return

            self.submit_entry_order(side, strategy, bar["close"])
        except Exception as e:
            logger.error(f"Execution Error: {e}")

    def fetch_active_position(self):
        try:
            self.pos = cast(Position, self.trading_client.get_open_position(self.symbol))
        except:
            self.pos = None

    def submit_entry_order(self, side: OrderSide, strategy: Strategy, price: float):
        account = self.trading_client.get_account()
        equity = float(account.equity) # type: ignore
        exposure = self.exposures[strategy]
        qty = int((equity * exposure) / price)
        
        if qty <= 0:
            logger.warning("qty <= 0; check exposure settings")
            return

        self.trading_client.close_all_positions(cancel_orders=True)
        order = MarketOrderRequest(
            symbol=self.symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC
        )
        
        order = cast(Order, self.trading_client.submit_order(order))
        self.fetch_active_position()
        logger.info(f"SUBMITTED {strategy} {side}: {order.qty} shares")

    def update_trailing_stop(self):
        if (self.pos is None): return
        orders = cast(list[Order], self.trading_client.get_orders())
        for o in orders:
            if o.symbol == self.symbol and o.type == OrderType.TRAILING_STOP:
                self.trading_client.cancel_order_by_id(o.id)

        trail_by = round(self.latest_atr * self.atr_multiplier, 2)
        if trail_by <= 0: return

        stop_side = OrderSide.SELL if self.pos.side == OrderSide.BUY else OrderSide.BUY
        stop_order = TrailingStopOrderRequest(
            symbol=self.symbol,
            qty=abs(float(self.pos.qty or 0)),
            side=stop_side,
            trail_price=trail_by,
            time_in_force=TimeInForce.GTC
        )
        self.trading_client.submit_order(stop_order)