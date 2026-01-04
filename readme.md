# Implied Volatility Band Strategy Backtest

## Idea

This strategy is an intraday mean-reversion system triggered by volatility extremes. It calculates price bands based on implied volatility (the market's expected daily move) and only enters trades when the IV Z-score is high, indicating an environment of elevated fear. Positions are initiated when the price dips below the lower band and begins to recover, or peaks above the upper band and starts to fade. Once a trade is active, an ATR-based trailing stop manages the exit, locking in gains as the price moves favourably without utilizing a fixed profit target.

## Backtest

The strategy was backtested on 1-hour candlestick data and daily historical options chain data for the S&P 500 (SPY) from January 2020 to January 2026.

### Performance Summary

![Strategy Performance](fig/SPY_strategy.jpg)

| Metric                    | Buy & Hold | Strategy |
| :------------------------ | ---------: | -------: |
| **Total Return**          |    109.07% |  103.38% |
| **Max Drawdown**          |    -26.93% |  -10.39% |
| **Sharpe Ratio**          |       0.78 |     1.06 |
| **Annualized Volatility** |     15.52% |   10.96% |
| **Annualized Alpha**      |            |   10.03% |
| **Beta (vs Asset)**       |            |     0.13 |

#### Key Insights

- **Risk Mitigation:** The strategy reduced the maximum drawdown by over 60% compared to the benchmark, maintaining a -10.39% floor against the market's -26.93%.
- **Efficiency:** With a Sharpe Ratio of 1.06, the system captured 95% of the total market return while operating with 30% less annualized volatility than the SPY.
- **Low Correlation:** A Beta of 0.13 indicates the strategy decoupled from standard market swings, relying on mean reversion during volatility spikes rather than directional momentum.
- **Macro Resilience:** The system successfully navigated three distinct regimes: the 2020 pandemic crash, the 2022 inflationary bear market, and the 2025 "AI bubble" volatility. By exiting during regime shifts via ATR trailing stops, it avoided the deep drawdowns typical of buy-and-hold during these periods.

### Robustness and Execution

#### 1. Monte Carlo Simulation

![](fig/SPY_strategy.jpg)

The strategy was run through 1,000 randomized simulations to test the stability of the equity curve.

- **Path Consistency:** The realized strategy path closely tracks the **50th percentile** of simulated outcomes, indicating the results are representative of average expectations rather than an outlier.
- **Drawdown Reliability:** Realized drawdowns remained well within the expected distribution, significantly outperforming the 95th percentile of simulated market failures.

#### 2. Lag Sensitivity

![](fig/SPY_lag_sensitivity.jpg)

The strategy was tested with execution delays of up to 35 periods to ensure it does not rely on immediate timing.

- **Sharpe Resilience:** The Sharpe Ratio remains robust even under significant execution lag, confirming that the risk-adjusted edge is driven by persistent regime shifts rather than perfect timing.
- **Alpha Persistence:** Annualized alpha remains positive even with significant delays, showing the signals capture broad regime shifts rather than fleeting price noise.
- **Stable Beta:** The strategy maintains a consistently low beta regardless of execution lag, confirming its defensive nature is a structural feature of the logic.

#### 3. Parameter Stability

![](fig/SPY_parameter_sensitivity.jpg)

Sensitivity heatmaps were used to check if the strategy depends on "perfect" settings.

- **Broad Profitability:** The system maintains a positive Sharpe Ratio across a wide range of ATR periods and Z-score thresholds.
- **Optimal Zones:** Performance is most consistent with **ATR multipliers between 1.5 and 2.0**, suggesting the strategy is robust to minor parameter changes.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Setup Dolt database and indexes
dolt --data-dir ./db sql -f setup.sql
dolt --data-dir ./db sql-server
```
