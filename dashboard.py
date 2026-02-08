from typing import cast
from dash import Dash, dcc, html, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objs as go
import numpy as np
import pandas as pd
from bot import HybridStrategy
from alpaca.trading.enums import OrderSide

import logging
logger = logging.getLogger(__name__)

# Constants for UI
REFRESH_INTERVAL = 2_000 # 2 seconds
THEME = dbc.themes.LUMEN

dashboard = None
app = Dash(__name__, external_stylesheets=[THEME])

def create_card(label, value, id):
    return dbc.Card(
        dbc.CardBody([
            html.H6(label, className="card-subtitle text-muted"),
            html.H4(value, id=id, className="card-title mb-0"),
            html.P(None, id=f"{id}-desc", className="card-text")
        ]),
        className="mb-2"
    )

app.layout = dbc.Container([
    dbc.Row([
        dbc.Col(html.H2("Hybrid Strategy Monitor", className="text-center py-3"), width=12)
    ]),
    
    dbc.Row([
        dbc.Col(create_card("Trend", "-", "card-trend"), width=2),
        dbc.Col(create_card("Regime", "-", "card-regime"), width=2),
        dbc.Col(create_card("Vol Rank", "-", "card-vol-z"), width=2),
        dbc.Col(create_card("Deannualized IV", "-", "card-vol-d"), width=2),
        dbc.Col(create_card("Active Position", "-", "card-pos"), width=2),
    ], className="justify-content-center mb-4"),

    dbc.Row([
        dbc.Col([
            dcc.Interval(id="interval-component", interval=REFRESH_INTERVAL),
            dcc.Graph(id="live-graph", style={"height": "60vh"}),
        ], width=12)
    ]),
], fluid=True)

@callback(
    [Output("live-graph", "figure"),
     Output("card-trend", "children"),
     Output("card-regime", "children"),
     Output("card-vol-z", "children"),
     Output("card-vol-d", "children"),
     Output("card-pos", "children"),
     Output("card-pos-desc", "children")],
    [Input("interval-component", "n_intervals")]
)
def update(_):
    try:
        if dashboard is None or not dashboard.strat.is_ready():
            return go.Figure(), "-", "-", "-", "-", "-", None
        
        strat = dashboard.strat
        
        with strat.lock:
            hist_df = strat.history.copy()
            vol_df = strat.vol_history.copy()
            daily_open = strat.daily_open.copy()
            curr_state = strat.curr_state
            
            vol_rank = strat.latest_vol_rank
            vol_d = strat.latest_vol_d
            fast_ma = strat.latest_fast_ma
            slow_ma = strat.latest_slow_ma

        # Data Processing
        hist = hist_df.tz_convert("America/New_York").dropna().sort_index()
        vol = vol_df.tz_convert("America/New_York").dropna().sort_index()

        df = pd.merge_asof(hist, vol, left_index=True, right_index=True, direction="forward").ffill()
        df["daily_open"] = daily_open.reindex(df.index, method="ffill").ffill()
        
        vol_move_dist = df["vol_d"] * strat.band_std_dev
        df["upper_band"] = df["daily_open"] * (1 + vol_move_dist)
        df["lower_band"] = df["daily_open"] * (1 - vol_move_dist)

        x_axis = np.arange(len(df))
        index = cast(pd.DatetimeIndex, df.index)
        
        # Determine Card Values
        regime_text = "High Vol" if vol_rank >= strat.vol_rank_entry_threshold else "Low Vol"
        trend_text = "Bullish" if fast_ma > slow_ma else "Bearish"
        
        if curr_state:
            pos_text = curr_state.strategy.value
            pos_desc = f"{curr_state.side.value.upper()} {curr_state.qty} @ {curr_state.filled_price}"
        else:
            pos_text = "Flat"
            pos_desc = None
            

        # Build Graph
        fig = go.Figure()
            
        # 1. Price Subplot
        fig.add_trace(go.Candlestick(
            x=x_axis, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="Price"
        ))
        
        fig.add_trace(go.Scatter(x=x_axis, y=df["daily_open"], line=dict(color='red', width=1, dash='dot'), legendgroup="Bands", name="Daily Open"))
        fig.add_trace(go.Scatter(x=x_axis, y=df["upper_band"], line=dict(color='cyan', width=1, dash='dot'), legendgroup="Bands", name="Vol Band +"))
        fig.add_trace(go.Scatter(x=x_axis, y=df["lower_band"], line=dict(color='cyan', width=1, dash='dot'), fill='tonexty', legendgroup="Bands", name="Vol Band -"))
        
        fig.add_trace(go.Scatter(x=x_axis, y=df["fast_ma"], line=dict(color='orange', width=1.5), legendgroup="MA", name="Fast MA"))
        fig.add_trace(go.Scatter(x=x_axis, y=df["slow_ma"], line=dict(color='magenta', width=1.5), legendgroup="MA", name="Slow MA"))

        if curr_state:
            sell = curr_state.side == OrderSide.SELL
            x = index.get_indexer([pd.Timestamp(curr_state.filled_at).tz_convert("America/New_York")], method="nearest")[0] # type: ignore
            y = 1.005*df["high"].iloc[x] if sell else 0.995*df["low"].iloc[x]
            marker = dict(color="red", size=10, symbol="triangle-down") if sell else dict(color="green", size=10, symbol="triangle-up")
            
            fig.add_trace(go.Scatter(
                x=[x], y=[y], text=[f"{curr_state.qty} @ {curr_state.filled_price}"], textposition=["top center" if sell else "bottom center"],
                mode="markers+text", marker=marker, textfont=dict(color="black", size=10),
                name="Entry Point"
            ))
            
        # 2. Volatility Subplot
        fig.add_trace(go.Scatter(x=x_axis, y=df["vol_rank"], name="Vol Rank", yaxis="y2", line=dict(color='gold')))
        fig.add_hline(y=strat.vol_rank_entry_threshold, line_dash="dash", line_color="red", yref="y2")

        # Layout styling
        fig.update_layout(
            xaxis_rangeslider_visible=False,
            hovermode="x unified",
            uirevision="constant",
            margin=dict(l=0, r=0, t=0, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            yaxis=dict(title="Price", domain=[0.35, 1]),
            yaxis2=dict(title="Vol Rank", domain=[0, 0.25]),
            xaxis=dict(
                tickvals=x_axis,
                ticktext=index.strftime("%a %b %d %H:%M"),
                showgrid=False,
                showticklabels=False,
            )
        )

        return fig, trend_text, regime_text, f"{vol_rank:.1f}", f"{vol_d:.2%}", pos_text, pos_desc

    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return go.Figure(), "Error", "Error", "Error", "Error", "Error", None

class Dashboard:
    def __init__(self, strat: HybridStrategy):
        self.strat = strat

    def run(self, **kwargs):
        global dashboard
        dashboard = self
        app.run(**kwargs)