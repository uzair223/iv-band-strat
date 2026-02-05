from dash import Dash, dcc, html, callback, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objs as go
import numpy as np
import pandas as pd
from bot import HybridStrategy

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
            html.H4(value, id=id, className="card-title"),
        ]),
        className="mb-2"
    )

app.layout = dbc.Container([
    dbc.Row([
        dbc.Col(html.H2("Hybrid Strategy Monitor", className="text-center py-3"), width=12)
    ]),
    
    dbc.Row([
        dbc.Col(create_card("Regime", "-", "card-regime"), width=2),
        dbc.Col(create_card("Vol Z-Score", "-", "card-vol-z"), width=2),
        dbc.Col(create_card("IV (%)", "-", "card-vol-d"), width=2),
        dbc.Col(create_card("Trend", "-", "card-trend"), width=2),
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
     Output("card-regime", "children"),
     Output("card-vol-z", "children"),
     Output("card-vol-d", "children"),
     Output("card-trend", "children"),
     Output("card-pos", "children")],
    [Input("interval-component", "n_intervals")]
)
def update(_):
    try:
        if dashboard is None or not dashboard.strat.is_ready:
            return go.Figure(), "-", "-", "-", "-", "-"
        
        strat = dashboard.strat
        
        with strat.lock:
            hist_df = strat.history.copy()
            vol_df = strat.vol_history.copy()
            curr_state = strat.curr_state
            
            vol_z = strat.latest_vol_z
            vol_d = strat.latest_vol_d
            fast_ma = strat.latest_fast_ma
            slow_ma = strat.latest_slow_ma

        # Data Processing
        hist = hist_df.tz_convert("America/New_York").dropna().sort_index()
        vol = vol_df.tz_convert("America/New_York").dropna().sort_index()

        day_open = hist["open"].resample("1D").first()
        hist["day_open"] = day_open.reindex(hist.index, method="ffill")

        df = pd.merge_asof(hist, vol, left_index=True, right_index=True, direction="forward").ffill()
        
        vol_move_dist = df["vol_d"] * strat.band_std_dev
        df["upper_band"] = df["day_open"] * (1 + vol_move_dist)
        df["lower_band"] = df["day_open"] * (1 - vol_move_dist)

        x_axis = np.arange(len(df))
        
        # Determine Card Values
        regime_text = "High Vol" if vol_z >= strat.vol_z_entry_threshold else "Low Vol"
        trend_text = "Bullish" if fast_ma > slow_ma else "Bearish"
        pos_text = f"{curr_state.side} {curr_state.qty} (@{curr_state.strategy})" if curr_state else "Flat"

        # Build Graph
        fig = go.Figure()

        # 1. Price Subplot
        fig.add_trace(go.Candlestick(
            x=x_axis, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="Price"
        ))
        
        fig.add_trace(go.Scatter(x=x_axis, y=df["upper_band"], line=dict(color='cyan', width=1, dash='dot'), name="Vol Band +"))
        fig.add_trace(go.Scatter(x=x_axis, y=df["lower_band"], line=dict(color='cyan', width=1, dash='dot'), fill='tonexty', name="Vol Band -"))
        
        fig.add_trace(go.Scatter(x=x_axis, y=df["fast_ma"], line=dict(color='orange', width=1.5), name="Fast MA"))
        fig.add_trace(go.Scatter(x=x_axis, y=df["slow_ma"], line=dict(color='magenta', width=1.5), name="Slow MA"))

        # 2. Volatility Subplot
        fig.add_trace(go.Scatter(x=x_axis, y=df["vol_z"], name="Vol Z-Score", yaxis="y2", line=dict(color='gold')))
        fig.add_hline(y=strat.vol_z_entry_threshold, line_dash="dash", line_color="red", yref="y2")

        # Layout styling
        fig.update_layout(
            xaxis_rangeslider_visible=False,
            hovermode="x unified",
            margin=dict(l=0, r=0, t=0, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            yaxis=dict(title="Price", domain=[0.35, 1]),
            yaxis2=dict(title="Vol Z", domain=[0, 0.25]),
            xaxis=dict(
                tickvals=x_axis,
                ticktext=df.index.strftime("%a %b %d %H:%M"), # type: ignore
                showgrid=False,
                showticklabels=False,
            )
        )

        return fig, regime_text, f"{vol_z:.2f}", f"{vol_d:.2%}", trend_text, pos_text

    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return go.Figure(), "Error", "Error", "Error", "Error", "Error"

class Dashboard:
    def __init__(self, strat: HybridStrategy):
        self.strat = strat

    def run(self, **kwargs):
        global dashboard
        dashboard = self
        app.run(**kwargs)