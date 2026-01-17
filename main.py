import os
from dotenv import load_dotenv
load_dotenv()

import asyncio
import threading

from pandas import Timedelta
from bot import HybridStrategy, days_to_bars, WEEK, MONTH
from dashboard import Dashboard

from math import ceil

import logging
logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("werkzeug").setLevel(logging.WARNING)


API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
DOLT_DB_URI = os.getenv("DOLT_DB_URI")

SYMBOL = os.getenv("SYMBOL", "SPY")
VOL_SYMBOL = os.getenv("VOL_SYMBOL", SYMBOL)
TIMEFRAME_HOURS = float(os.getenv("TIMEFRAME_HOURS", 1))
timeframe = Timedelta(hours=TIMEFRAME_HOURS)

MR_EXPOSURE = float(os.getenv("MR_EXPOSURE", 1.0))
TF_EXPOSURE = float(os.getenv("TF_EXPOSURE", 1.0))
VOL_Z_WINDOW = int(os.getenv("VOL_Z_WINDOW", 21))
VOL_Z_ENTRY_THRESHOLD = float(os.getenv("VOL_Z_ENTRY_THRESHOLD", 0.0))
BAND_STD_DEV = float(os.getenv("BAND_STD_DEV", 0.8))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", ceil(days_to_bars(timeframe, WEEK))))
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER", 3.0))
FAST_MA_WINDOW = int(os.getenv("FAST_MA_WINDOW", ceil(days_to_bars(timeframe, WEEK))))
SLOW_MA_WINDOW = int(os.getenv("SLOW_MA_WINDOW", ceil(days_to_bars(timeframe, MONTH))))

LONG_ONLY = os.getenv("LONG_ONLY", "0") == "1"
PAPER = os.getenv("PAPER", "1") == "1"
DELAYED_BACKFILL = os.getenv("DELAYED_BACKFILL", "1") == "1"

if not API_KEY or not SECRET_KEY:
    raise ValueError("API_KEY and SECRET_KEY must be set")

strat = HybridStrategy(
    symbol=SYMBOL,
    vol_symbol=VOL_SYMBOL,
    timeframe=timeframe,
    mr_exposure=MR_EXPOSURE,
    tf_exposure=TF_EXPOSURE,
    vol_z_window=VOL_Z_WINDOW,
    vol_z_entry_threshold=VOL_Z_ENTRY_THRESHOLD,
    band_std_dev=BAND_STD_DEV,
    atr_period=ATR_PERIOD,
    atr_multiplier=ATR_MULTIPLIER,
    fast_ma_window=FAST_MA_WINDOW,
    slow_ma_window=SLOW_MA_WINDOW,
    api_key=API_KEY,
    secret_key=SECRET_KEY,
    dolt_db_uri=DOLT_DB_URI,
    long_only=LONG_ONLY,
    paper=PAPER,
    delayed_backfill=DELAYED_BACKFILL,
)

dashboard = Dashboard(strat)

async def main():
    threading.Thread(target=dashboard.run, kwargs=dict(debug=False, use_reloader=False, host='0.0.0.0', port=8050), daemon=True, name="DashThread").start()
    bot_task = asyncio.create_task(strat.run())
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutting down...")