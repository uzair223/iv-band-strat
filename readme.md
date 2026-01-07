# Implied Volatility Band Strategy Backtest

## Idea

This strategy is an intraday mean-reversion system triggered by volatility extremes. It calculates price bands based on implied volatility (the market's expected daily move) and only enters trades when the IV Z-score is high, indicating an environment of elevated fear. Positions are initiated when the price dips below the lower band and begins to recover, or peaks above the upper band and starts to fade. Once a trade is active, an ATR-based trailing stop manages the exit, locking in gains as the price moves favorably without utilizing a fixed profit target.

## Backtest

The strategy was backtested on 1-hour candlestick data and daily historical options chain data for the S&P 500 (SPY) from January 2020 to January 2026.

### Performance Summary

| Metric                        | Buy & Hold | Strategy |
| ----------------------------- | ---------- | -------- |
| **Total Return (%)**          | 111.93%    | 118.43%  |
| **Annualized Return (%)**     | 12.35%     | 12.88%   |
| **Annualized Volatility (%)** | 15.51%     | 10.87%   |
| **Sharpe Ratio**              | 0.80       | 1.19     |
| **Sortino Ratio**             | 0.99       | 0.96     |
| **Max Drawdown (%)**          | -26.93%    | -10.21%  |
| **Avg Drawdown (%)**          | -5.94%     | -3.27%   |
| **Annualized Alpha (%)**      |            | 11.76%   |
| **Beta (vs Asset)**           |            | 0.09     |

#### Key Insights

- **Risk Mitigation:** The strategy reduced the maximum drawdown by over 60% compared to the benchmark, maintaining a -10.21% floor against the market's -26.93%.
- **Efficiency:** With a Sharpe Ratio of 1.19, the system outperformed the market's risk-adjusted returns while operating with significantly lower annualized volatility (10.87% vs 15.51%).
- **Low Correlation:** A Beta of 0.09 indicates the strategy is almost entirely decoupled from standard market swings, relying on mean reversion during volatility spikes.
- **Alpha Generation:** The strategy produced an Annualized Alpha of 11.76%, demonstrating a strong independent edge over the benchmark.

---

## Robustness and Execution

### 1. Monte Carlo Simulation

The strategy was run through 1,000 randomized simulations to test the stability of the equity curve.

- **Path Consistency:** The realized strategy path closely tracks the **50th percentile** of simulated outcomes, indicating results are representative of average expectations.
- **Drawdown Reliability:** Realized drawdowns remained well within the expected distribution, significantly outperforming the 95th percentile of simulated market failures.

### 2. Timeframe Sensitivity

The strategy was tested across various timeframes to determine the stability of the edge across different frequencies.

- **Optimal Window:** The **1-hour timeframe** produces the peak risk-adjusted return with a Sharpe Ratio of approximately 1.2.
- **Frequency Decay:** Performance remains stable from 5-minute to 4-hour intervals, though it significantly degrades at the 1-minute level where the Sharpe Ratio turns negative.
- **Beta Stability:** Beta remains near zero across almost all timeframes, confirming the strategy's market-neutral defensive characteristics.

### 3. Lag Sensitivity

The strategy was tested with execution delays of up to 35 periods to ensure it does not rely on immediate timing.

- **Sharpe Resilience:** The Sharpe Ratio remains robust even under significant execution lag, confirming the risk-adjusted edge is driven by persistent regime shifts.
- **Alpha Persistence:** Annualized alpha remains positive even with significant delays, showing the signals capture broad trends rather than fleeting price noise.

### 4. Parameter Stability

Sensitivity heatmaps were used to check if the strategy depends on "perfect" settings.

- **Alpha Consistency:** The system maintains a positive **Annualized Alpha** across a wide range of ATR periods and Z-score thresholds.
- **Optimal Zones:** Performance is most consistent with lower `band_std_dev` values combined with mid-range `vol_z_window` settings, reaching Alphas above 15% in specific clusters.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Setup Dolt database and indexes
dolt --data-dir ./db sql -f setup.sql
dolt --data-dir ./db sql-server

```
