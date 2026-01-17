from dash import Dash, dcc, callback, Input, Output
import plotly.graph_objs as go
import plotly.express as px
import numpy as np
import pandas as pd
from bot import HybridStrategy, days_to_bars

import logging
logger = logging.getLogger(__name__)

REFRESH_INTERVAL = 1_000

dashboard = None
app = Dash()
app.layout = [
        dcc.Interval(
        id="interval-component",
        interval=REFRESH_INTERVAL,
    ),
    dcc.Graph(id="live-graph", style={"width": "95vw", "height": "95vh", "margin": "auto"}),
]

@callback(
    Output("live-graph", "figure"),
    [Input("interval-component", "n_intervals")]
)
def update(_):
    try:
        if dashboard is None or not dashboard.strat.is_ready:
            return go.Figure()
        strat = dashboard.strat
        
        with strat.lock:
            hist_df = strat.history.copy()
            vol_df = strat.vol_history.copy()
            day_open = strat.day_open.copy()
            vol_z = strat.latest_vol_z
            vol_d = strat.latest_vol_d
        
        subset = int(days_to_bars(strat.timeframe, 21))
        left = hist_df[-subset:].tz_convert("America/New_York").dropna().sort_index().reset_index()
        right = vol_df.tz_convert("America/New_York").dropna().sort_index().reset_index()

        df = pd.merge_asof(
            left.sort_values("timestamp"),
            right.sort_values("timestamp"),
            on="timestamp",
            direction="forward"
        ).set_index("timestamp").ffill()
        
        day_open = day_open.reindex(df.index, method="ffill")
        vol_move_dist = day_open * df["vol_d"] * strat.band_std_dev
        df["upper_band"] = day_open + vol_move_dist
        df["lower_band"] = day_open - vol_move_dist

        x_axis = np.arange(len(df))
        data = [
            # Price and Indicators
            go.Candlestick(
                x=x_axis, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                name="Price", xaxis="x", yaxis="y"
            ),
            go.Scatter(
                x=x_axis, y=df["upper_band"], line_shape="hv",
                line_color="rgba(173, 216, 230, 0.8)", 
                name=f"+{strat.band_std_dev}\u03C3 Vol Band", legendgroup="Indicators",
                xaxis="x", yaxis="y"
            ),
            go.Scatter(
                x=x_axis, y=df["lower_band"], line_shape="hv",
                line_color="rgba(173, 216, 230, 0.8)", fill="tonexty", fillcolor="rgba(173, 216, 230, 0.3)",
                name=f"-{strat.band_std_dev}\u03C3 Vol Band", legendgroup="Indicators",
                xaxis="x", yaxis="y"
            ),
            go.Scatter(
                x=x_axis, y=df["fast_ma"], line_color="orange", line_dash="dot", opacity=0.8,
                showlegend=False, legendgroup="Indicators", name=f"Fast MA({strat.fast_ma_window})",
            ),
            go.Scatter(
                x=x_axis, y=df["slow_ma"], line_color="purple", line_dash="dot", opacity=0.8,
                showlegend=False, legendgroup="Indicators", name=f"Slow MA({strat.slow_ma_window})",
            ),
            go.Scatter(
                x=x_axis, y=df["vol_d"],
                line_color=px.colors.qualitative.Plotly[0], name="Vol", showlegend=False,
                xaxis="x", yaxis="y2"
            ),
            go.Scatter(
                x=x_axis, y=df["vol_ma"],
                line_color=px.colors.qualitative.Plotly[1], name=f"Vol MA({strat.vol_z_window})", showlegend=False,
                xaxis="x", yaxis="y2"
            ),
            go.Scatter(
                x=x_axis, y=df["vol_z"],
                line_color=px.colors.qualitative.Plotly[0], name="Vol Z-Score", showlegend=False,
                xaxis="x", yaxis="y3"
            ),
            go.Scatter(
                x=x_axis, y=np.repeat(strat.vol_z_entry_threshold, len(x_axis)),
                line_color="gray", showlegend=False, hoverinfo="skip",
                xaxis="x", yaxis="y3"
            ),		
        ]

        spacing=0.01
        rh = (rh := np.array([2, 1, 1])[::-1]) / np.sum(rh)
        ydomains = [(np.sum(rh[:i])+(spacing * (i != 0)), np.sum(rh[:i+1])-(spacing * (i != len(rh)-1))) for i in range(len(rh))][::-1]

        layout = dict(
            title=f"Live Strategy: {vol_z=:.2f} {vol_d=:.2%}",
            hoversubplots="axis",
            hovermode="x",
            grid=dict(rows=len(rh), columns=1),
            legend=dict(
                orientation="h",
                yanchor="top", y=-0.01,
                xanchor="left", x=0,
            ),
            xaxis=dict(
                matches="x",
                showticklabels=False, showgrid=False,
                showspikes=True, spikemode="across", spikethickness=1,
                tickvals=x_axis, ticktext=df.index.strftime("%a %d %b %Y %H:%M %Z").to_list(), # type: ignore
                rangeslider=dict(visible=False)
            ),
            yaxis =dict(domain=ydomains[0], title="Price", automargin=True),
            yaxis2=dict(domain=ydomains[1], title=f"Vol", automargin=True),
            yaxis3=dict(domain=ydomains[2], title="Vol Z-Score", automargin=True),
        )

        fig = go.Figure(data=data, layout=layout)
        return fig
    except Exception as e:
        logger.error(f"Error in dashboard update: {e}")
        return go.Figure()

class Dashboard:
    def __init__(self, strat: HybridStrategy):
        self.strat = strat

    def run(self, **kwargs):
        global dashboard
        assert dashboard is None, "Dash app is already running"
        dashboard = self
        app.run(**kwargs)