import asyncio
import threading
import logging
from math import ceil
from typing import Callable, cast, Optional, Any

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import aiohttp
from urllib.parse import urlencode
from uuid import uuid4

# Alpaca SDK
from alpaca.trading import (
    TradingClient, TradingStream,
    MarketOrderRequest, StopOrderRequest, ReplaceOrderRequest, GetOrdersRequest, GetCalendarRequest,
    Position, Order, Asset, AssetClass, Clock, Calendar, TradeAccount,
    OrderSide, PositionSide, TimeInForce, QueryOrderStatus,
    TradeUpdate, TradeEvent
)

from alpaca.data import (
    StockHistoricalDataClient,
    StockBarsRequest, TimeFrame, Sort, DataFeed,
    BarSet, Bar
)
from alpaca.data.live.stock import StockDataStream
class AlpacaConnectionLimitFilter(logging.Filter):
    def __init__(self):
        self.only_log_once = dict()
        
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "starting data websocket connection" in msg:
            return False
        
        if "connecting to" in msg or "data websocket error" in msg:
            if self.only_log_once.get(msg, False):
                return False
            self.only_log_once[msg] = True
            
        if "connection limit exceeded" in msg:
            if self.only_log_once.get(msg, False):
                return False
            record.exc_info = None
            record.stack_info = None
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            self.only_log_once[msg] = True
        return True
logging.getLogger("alpaca.data.live.websocket").addFilter(AlpacaConnectionLimitFilter())

logger = logging.getLogger(__name__)

# Constants
YEAR = 252
WEEK = 5
MONTH = 21
HOURS_PER_DAY = 6.5

def days_to_bars(n: float, timeframe: timedelta):
    hours = timeframe.total_seconds() / 3600
    return ceil(HOURS_PER_DAY / hours * n)

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
    filled_at: datetime
    filled_price: float
    uid: str
    last_stop_price: Optional[float] = None
    with_sl: bool = False
    sl_apca_id: Optional[str] = None

class ParsedClientIdType(str, Enum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    
@dataclass
class ParsedClientId:
    type: ParsedClientIdType
    strategy: Strategy
    uid: str
    with_sl: Optional[bool] = None
    parent_uid: Optional[str] = None
    
class HybridStrategy:
    VERSION = "HSv1"
    
    def _entry_cid(self, strategy: Strategy, with_sl: bool) -> str:
        uid = uuid4().hex[:12]
        return "::".join((self.VERSION, ParsedClientIdType.ENTRY, strategy, uid, str(int(with_sl))))

    def _sl_uid(self, strategy: Strategy, parent_uid: str) -> str:
        uid = uuid4().hex[:12]
        return "::".join((self.VERSION, ParsedClientIdType.STOP, strategy, uid, parent_uid))
    
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
            parsed.with_sl = parts[4] == "1"
        elif parsed.type == ParsedClientIdType.STOP:
            parsed.parent_uid = parts[4]
        return parsed
        
    def __init__(self,
                 api_key: str,
                 secret_key: str,
                 trade_symbol: str,
                 timeframe=timedelta(hours=1), 
                 band_std_dev = 0.8,
                 vol_rank_window = 21,
                 vol_rank_entry_threshold = 50.0,
                 atr_period = days_to_bars(WEEK, timedelta(hours=1)),
                 atr_multiplier = 3.0,
                 fast_ma_window = days_to_bars(WEEK, timedelta(hours=1)),
                 slow_ma_window = days_to_bars(MONTH, timedelta(hours=1)),
                 mr_exposure: float = 0.1,
                 tf_exposure: float = 0.1,
                 feed=DataFeed.IEX, long_only=False, paper=True, delayed_backfill=True,
                 vol_fetch_timeout = 300,
                 vol_symbol:Optional[str]=None, pos_symbol: Optional[str]=None):

        self.curr_state: Optional[TradeState] = None
        self.lock = threading.RLock()
        
        # Clients
        self.trading_client = TradingClient(api_key, secret_key, paper=paper)
        self.trading_stream = TradingStream(api_key, secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, secret_key)
        self.data_stream = StockDataStream(api_key, secret_key)
        self.feed = feed
        
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
        self.vol_rank_window = vol_rank_window
        self.vol_rank_entry_threshold = vol_rank_entry_threshold
        self.atr_period, self.atr_multiplier = atr_period, atr_multiplier
        self.fast_ma_window, self.slow_ma_window = fast_ma_window, slow_ma_window
        
        # Timeframe Handling
        self.timeframe = timeframe
        self.warmup_timedelta = 2 * max(atr_period, slow_ma_window) * timeframe
        self.max_buf = ceil(self.warmup_timedelta.total_seconds() / 60)
        
        self.minute_bar_count = 0
        self.delayed_backfill = delayed_backfill
        self.vol_fetch_timeout = vol_fetch_timeout
        
        # Data Buffers
        self.minute_buffer = pd.DataFrame() # Raw 1m bars
        self.history = pd.DataFrame()       # Resampled target bars
        self.vol_history = pd.DataFrame()
        
        # Loop Control
        self.is_bfill_fetched = False
        self.is_vol_fetched = False
        self.last_bar_time: Optional[pd.Timestamp] = None
        self.shutdown = asyncio.Event()
        self.shutdown_ts = asyncio.Event()
        
        # Indicators
        self.daily_open = pd.Series(dtype=float)
        self.curr_bar: Optional[pd.Series] = None
        self.last_bar: Optional[pd.Series] = None
        
        self.entry_signal: Optional[tuple[OrderSide, Strategy, bool]] = None
        self.exit_signal: bool = False
        
        # Refresh tracking
        self.last_vol_refresh_time = datetime.min.replace(tzinfo=UTC)

    # === STREAM MANAGEMENT ===
    
    async def run(self):
        self.rebuild_state()
        logger.info(f"starting stream for {self.asset.symbol}")
        
        self.data_stream.subscribe_bars(self.handle_minute_bar, self.asset.symbol) # type: ignore
        self.trading_stream.subscribe_trade_updates(self.handle_trade_updates)
        tasks = [
            asyncio.create_task(self.data_stream._run_forever()),
            asyncio.create_task(self.trading_stream._run_forever()),
            asyncio.create_task(self.fetch_vol_from_source())
        ]
        clock = cast(Clock, self.trading_client.get_clock())
        if not self.delayed_backfill or not clock.is_open:
            tasks.append(asyncio.create_task(self.fetch_backfill()))
        else:
            logger.info("delayed backfill enabled; waiting for 15 mins of data")
        
        try:
            for task in tasks:
                await task
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await self.stop()

    async def stop(self):
        if self.data_stream:
            await self.data_stream.stop_ws()
        if self.trading_stream:
            await self.trading_stream.stop_ws()
        self.shutdown.set()
        
    # === VOLATILITY DATA MANAGEMENT ===
    
    async def _with_retries(
        self,
        func: Callable[[], Any],
        name="request",
        max_retries=5,
        base_delay=1.0,
    ):
        for attempt in range(1, max_retries + 1):
            if self.shutdown.is_set():
                logger.info(f"aborting {name}: shutdown requested")
                return

            try:
                return await func()
            except Exception as e:
                if attempt >= max_retries:
                    e.add_note(f"failed {name} (retries exhausted)")
                    logger.exception(e)
                    return

                delay = base_delay * (2 ** (attempt - 1))
                e.add_note("failed %s (attempt %d/%d). retrying in %.1fs" % (name, attempt, max_retries, delay))
                logger.exception(e)

                try:
                    await asyncio.wait_for(self.shutdown.wait(), timeout=delay)
                    logger.info(f"aborting {name}: shutdown during backoff")
                    return
                except asyncio.TimeoutError:
                    pass
    
    async def fetch_vol_from_source(self):
        self.is_vol_fetched = False

        query = (
            "SELECT DATE_ADD(date, INTERVAL 1 DAY) as timestamp, iv_current as vol "
            "FROM volatility_history "
            "WHERE act_symbol = '%s' "
            "ORDER BY date DESC "
            "LIMIT %d"
        )
        

        owner, database, branch = "post-no-preference", "options", "master"
        url = f"https://www.dolthub.com/api/v1alpha1/{owner}/{database}/{branch}"
        params = {"q": query % (self.vol_symbol, 50+self.vol_rank_window * 3)}
        qurl = f"{url}?{urlencode(params)}"

        logger.info("fetching volatility data from source: %s", qurl)
        async def func():
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=self.vol_fetch_timeout)) as res:
                    res.raise_for_status()
                    data: dict = await res.json()

            if data.get("query_execution_status") != "Success":
                raise ValueError(data.get("query_execution_message"))

            rows = data.get("rows", [])
            if not rows:
                logger.warning("volatility query returned no rows")
                return

            with self.lock:
                self.vol_history = (
                    pd.DataFrame(rows, columns=["timestamp", "vol"])
                    .set_index("timestamp")
                    .sort_index()
                )
                self.vol_history.index = (
                    pd.to_datetime(self.vol_history.index)
                    .tz_localize("America/New_York")
                )
                self.is_vol_fetched = True
                self.last_vol_refresh_time = datetime.now(UTC)

            self.calc_vol_stats()
            await self.resample_and_sync(True)

            logger.info(
                "volatility history updated. fetched %d rows. %s, %s",
                len(rows),
                f"{self.latest_vol_rank=:.1f}",
                f"{self.latest_vol_d=:.2f}",
            )

        return await self._with_retries(func, name="fetch volatility data")

    async def refresh_vol_data(self):
        now = datetime.now(UTC)
        refresh_threshold = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if (
            now >= refresh_threshold
            and self.last_vol_refresh_time < refresh_threshold
            and not self.shutdown.is_set()
        ):
                await self.fetch_vol_from_source()

    def calc_vol_stats(self):
        with self.lock:
            vol_history_df = self.vol_history.copy()
        if len(vol_history_df) < 2: return
        
        vol_history_df["vol_d"] = pd.to_numeric(vol_history_df["vol"], errors="coerce").ffill() * np.sqrt(1 / YEAR)
        vol_history_df["vol_rank"] = vol_history_df["vol_d"].rolling(window=self.vol_rank_window).rank(pct=True) * 100
        
        with self.lock:
            self.vol_history = vol_history_df
            self.latest_vol_rank = float(vol_history_df["vol_rank"].iloc[-1])
            self.latest_vol_d = float(vol_history_df["vol_d"].iloc[-1])

    # === DATA INGESTION & AGGREGATION ===
    
    async def fetch_backfill(self):
        if cast(Clock, self.trading_client.get_clock()).is_open:
            logger.info("backfilling historical bars")
            now = datetime.now(UTC)
        else:
            logger.info("market is closed; backfilling up to last closed session")
            today = datetime.now(UTC).date()
            cal = cast(list[Calendar], self.trading_client.get_calendar(GetCalendarRequest(start=today-timedelta(days=2),end=today)))
            last_session = cal[-1]
            now = last_session.close.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
                
        req = StockBarsRequest(
            symbol_or_symbols=self.asset.symbol, 
            timeframe=TimeFrame.Minute, # type: ignore
            end=now,
            start=datetime.fromtimestamp(0, tz=UTC),
            sort=Sort.DESC,
            limit=self.max_buf,
            feed=self.feed,
        )
        bars = cast(BarSet, self.data_client.get_stock_bars(req))
        fetched_df = (
            bars.df.loc[self.asset.symbol]
            .reset_index()
            [["timestamp","open","high","low","close","volume"]]
            .set_index("timestamp")
            .tz_convert("America/New_York")
            .sort_index()
        )
        
        with self.lock:
            buf = pd.concat([fetched_df, self.minute_buffer]).drop_duplicates().sort_index()
            self.minute_buffer = buf
            self.is_bfill_fetched = True
        await self.resample_and_sync(True)
        logger.info(f"warmup complete. buffer size: {len(self.minute_buffer)} mins.")

    async def handle_minute_bar(self, bar: Bar):
        self.minute_bar_count += 1
        if self.minute_bar_count == 15 and self.delayed_backfill:
            await self.fetch_backfill()
        
        new_row = pd.DataFrame([bar.model_dump()]).set_index("timestamp").tz_convert("America/New_York")
        with self.lock:
            self.minute_buffer = pd.concat([self.minute_buffer, new_row]).sort_index()
            self.minute_buffer.index = (
                pd.to_datetime(self.minute_buffer.index).tz_convert("America/New_York")
            )
            if len(self.minute_buffer) > self.max_buf:
                self.minute_buffer = self.minute_buffer.iloc[-self.max_buf:]
            await self.resample_and_sync()

    async def resample_and_sync(self, executed_on_backfill=False):
        with self.lock:
            minute_df = self.minute_buffer.copy()
        if minute_df.empty: return

        resampled = (
            minute_df
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
        latest_mbar_time: pd.Timestamp = minute_df.index[-1]
        
        with self.lock:
            self.last_bar_time = latest_idx
            self.minute_buffer = minute_df
            self.history = resampled
            
            self.curr_bar = resampled.iloc[-1]
            if len(resampled) > 1:
                self.last_bar = resampled.iloc[-2]
                
            self.calc_indicators()
            if not executed_on_backfill and self.is_closing_bar(latest_mbar_time):
                await self.on_bar_closed(resampled.iloc[-1])
                
            
    def is_closing_bar(self, ts: datetime):
        ts_seconds = int(ts.astimezone(UTC).replace(second=0, microsecond=0).timestamp())
        tf_seconds = int(self.timeframe.total_seconds())
        return (ts_seconds+60) % tf_seconds == 0
        
    def calc_indicators(self):
        if not self.is_vol_fetched:
            return
        
        with self.lock:
            history_df = self.history.copy()
        if len(history_df) < max(self.atr_period, self.slow_ma_window) + 1: return
        
        history_df["tr"] = np.maximum(history_df["high"] - history_df["low"], 
                    np.maximum(np.abs(history_df["high"] - history_df["close"].shift(1)), 
                                np.abs(history_df["low"] - history_df["close"].shift(1))))
        history_df["atr"] = history_df["tr"].rolling(window=self.atr_period).mean()
        history_df["fast_ma"] = history_df["close"].ewm(span=self.fast_ma_window, adjust=True).mean()
        history_df["slow_ma"] = history_df["close"].ewm(span=self.slow_ma_window, adjust=True).mean()
        daily_open = history_df["open"].resample("D", label="left", closed="left").first().ffill()
        
        with self.lock:
            self.history = history_df
            self.daily_open = daily_open
            self.latest_atr = float(history_df["atr"].iloc[-1])
            self.latest_fast_ma = float(history_df["fast_ma"].iloc[-1])
            self.latest_slow_ma = float(history_df["slow_ma"].iloc[-1])
            self.latest_lower_band = daily_open.iloc[-1] - self.band_std_dev * self.latest_vol_d
            self.latest_upper_band = daily_open.iloc[-1] + self.band_std_dev * self.latest_vol_d
            
    async def on_bar_closed(self, bar: pd.Series):
        await self.refresh_vol_data()
        logger.info(f"bar closed at %s | close=%.2f | vol_rank=%.1f vol_regime=%s | fast_ma=%.2f slow_ma=%.2f trend=%s",
                    cast(pd.Timestamp, bar.name).strftime('%Y-%m-%d %H:%M %Z'),
                    bar["close"],
                    self.latest_vol_rank, "high" if self.latest_vol_rank >= self.vol_rank_entry_threshold else "low",
                    self.latest_fast_ma, self.latest_slow_ma, "up" if self.latest_fast_ma > self.latest_slow_ma else "down")
        
        if self.is_ready():
            self.evaluate_signals()
            self.execute_trades()

    def is_ready(self):
        return self.is_bfill_fetched and self.is_vol_fetched
    
    # === SIGNAL EXECUTION ===
    
    def evaluate_signals(self):
        with self.lock:
            self.exit_signal = False
            self.entry_signal = None

            if self.last_bar is None or self.curr_bar is None:
                return
            
            high_vol = self.latest_vol_rank >= self.vol_rank_entry_threshold
            
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
        if self.handle_exit():
            logger.info("exit signal executed")
            return
        if self.handle_entry():
            logger.info("entry signal executed")
            return
        self.handle_sl()
        
    # === TRADE HANDLERS ===

    def handle_entry(self):
        if not self.entry_signal or self.curr_bar is None:
            return False
        
        side, strategy, with_sl = self.entry_signal

        if self.long_only and side == OrderSide.SELL:
            logger.info("long-only mode; skipping short entry")
            return False
        
        try:
            self.submit_entry_order(side, strategy, with_sl, self.curr_bar["close"])
        except Exception as e:
            e.add_note("error handling entry")
            logger.exception(e)
            return False
        
        self.entry_signal = None
        return True
    
    def handle_sl(self):
        if not self.curr_state or self.curr_bar is None:
            return False
        try:
            order, is_replaced = self.upsert_sl_order(self.curr_state, self.curr_bar["close"], update_state=True)
            if order is not None:
                logger.info(f"{'modified' if is_replaced else 'submitted'} {order.side} stop order @ {order.stop_price}")
        except Exception as e:
            e.add_note("error handling stop order")
            logger.exception(e)
            return False
        return True

        
    def handle_exit(self):
        if not self.exit_signal:
            return False
        try:
            self.close_position()
        except Exception as e:
            e.add_note("error handling exit")
            logger.exception(e)
            return False
        self.exit_signal = False
        return True
        
    # === ORDER SUBMISSION ===
    
    def submit_entry_order(self, side: OrderSide, strategy: Strategy, with_sl: bool, price: float):
        account = cast(TradeAccount, self.trading_client.get_account())
        equity = float(account.equity or 0)
        exposure = self.exposures[strategy]
        qty = (equity * exposure) / price
        if qty <= 0:
            raise ValueError("calculated order quantity is zero or negative")
        
        cid = self._entry_cid(strategy, with_sl)
        self.close_position()
        order = self.trading_client.submit_order(MarketOrderRequest(
            symbol=self.asset.symbol,
            qty=int(qty) if self.asset.asset_class == AssetClass.US_EQUITY else qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            client_order_id=cid
        ))
        return cast(Order, order)

    def close_position(self):
        open_orders = cast(list[Order], self.trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[self.asset.symbol],
        )))
        for o in open_orders:
            self.trading_client.cancel_order_by_id(o.id)
        try:
            order = self.trading_client.close_position(self.asset.symbol)
        except:
            return None
        return cast(Order, order)
            
    def upsert_sl_order(self, state: TradeState, price: float, update_state=True):
        if not state.with_sl:
            return None, None  # no-op if SL not enabled

        trail = self.latest_atr * self.atr_multiplier
        if trail <= 0:
            raise ValueError("calculated ATR trail <= 0")

        cid = self._sl_uid(state.strategy, state.uid)

        # determine stop side and price
        if state.side == OrderSide.BUY:
            side = OrderSide.SELL
            stop = price - trail
            if state.last_stop_price is not None:
                stop = max(state.last_stop_price, stop)
        else:
            side = OrderSide.BUY
            stop = price + trail
            if state.last_stop_price is not None:
                stop = min(state.last_stop_price, stop)

        stop = round(stop, 2)

        # submit new stop order if none exists
        if state.sl_apca_id is None:
            order = self.trading_client.submit_order(
                StopOrderRequest(
                    symbol=self.asset.symbol,
                    qty=state.qty,
                    side=side,
                    stop_price=stop,
                    time_in_force=TimeInForce.GTC,
                    client_order_id=cid,
                )
            )
        else:
            # modify existing stop if needed
            if stop == state.last_stop_price:
                return None, None # nothing to do
            order = self.trading_client.replace_order_by_id(
                str(state.sl_apca_id),
                ReplaceOrderRequest(
                    stop_price=stop,
                    client_order_id=cid,
                )
            )

        order = cast(Order, order)
        is_replaced = state.sl_apca_id is not None
        if order is not None and update_state:
            state.sl_apca_id = str(order.id)
            state.last_stop_price = float(str(order.stop_price))
        return order, is_replaced

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
                    symbol=o.symbol, # type: ignore
                    strategy=parsed.strategy,
                    side=o.side,
                    qty=int(o.qty),
                    filled_at=o.filled_at.replace(tzinfo=UTC), # type: ignore
                    filled_price=float(o.filled_avg_price), # type: ignore
                    with_sl=bool(parsed.with_sl),
                    uid=parsed.uid,
                )
                break
            
        if self.curr_state is None:
            logger.warning("no matching entry order for open position")
            return

        # find existing stop order
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
            if parsed.type == ParsedClientIdType.STOP and parsed.parent_uid == self.curr_state.uid:
                self.curr_state.sl_apca_id = str(o.id)
                break
        logger.info(f"reconstructed trade state: {self.curr_state}")
        
    async def handle_trade_updates(self, data: TradeUpdate):
        if data.event == TradeEvent.FILL:
            self.on_fill(data.order)
        
    def on_fill(self, order: Order):
        if order.symbol != self.asset.symbol:
            return
        
        if not order.symbol or not order.side or not order.qty or not order.id or not order.filled_avg_price or not order.filled_at:
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
        
        if parsed.type == ParsedClientIdType.ENTRY:
            self.curr_state = TradeState(
                symbol=order.symbol,
                strategy=parsed.strategy,
                side=order.side,
                qty=int(order.qty),
                filled_at=order.filled_at.replace(tzinfo=UTC),
                filled_price=float(order.filled_avg_price),
                with_sl=bool(parsed.with_sl),
                uid=parsed.uid,
            )
            stop_order = self.upsert_sl_order(self.curr_state, float(order.filled_avg_price))[0]
            if stop_order is not None:
                logger.info(f"submitted {stop_order.side} stop order @ {stop_order.stop_price}")