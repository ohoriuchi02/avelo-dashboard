"""
Avelo E195-E2 Range & Market Demand Explorer (v2)
--------------------------------------------------
Streamlit port of range_map_lib.ipynb / prepare_data.ipynb. Pick an origin
airport and see every destination airport as a dot: color = route capability
(the WEAKER of the origin's and destination's own runway capability), fill =
reachable to AND from (distance within both airports' own range). Demand
circles are drawn per geographically-clustered destination market.

Run locally:    streamlit run app.py
Deploy:         push this repo to GitHub, deploy on share.streamlit.io
"""

import numpy as np
import pandas as pd
import streamlit as st
import folium
import branca.colormap as cm
from pyproj import Geod
from pathlib import Path
from streamlit_folium import st_folium

st.set_page_config(
    page_title="Avelo Range & Demand Explorer",
    page_icon="\u2708\ufe0f",
    layout="wide",
)

DATA_DIR = Path(__file__).parent / "data"

# --- demand sheet column names: matched to your DB1B-style market file ---
ORIGIN_AIRPORT_COL = "Apt_1"
ORIGIN_MARKET_COL = "CityMktID_1"
DEST_AIRPORT_COL = "Apt_2"
DEST_MARKET_COL = "CityMktID_2"
PAX_COL = "passengers"
FARE_COL = "fare"

# OD40 (bts.gov/OD-40) samples 40% of tickets, not 100% -- every passenger
# count is scaled up by this factor when demand data is loaded, so displayed
# totals reflect true volume rather than the raw sample.
OD_SAMPLE_RATE = 0.40

# --- airport dot styling ---
AIRPORT_DOT_RADIUS_PX = 3
MTOW_DOT_COLOR = "#000000"
RESTRICTED_DOT_COLOR = "#e67e22"
INSUFFICIENT_DOT_COLOR = "#999999"

CAPABILITY_RANK = {"INSUFFICIENT": 0, "RESTRICTED": 1, "MTOW": 2}
RANK_TO_CAPABILITY = {v: k for k, v in CAPABILITY_RANK.items()}

# CARTO's Positron tiles require a free API key as of 2026 (request one at
# carto.com/basemaps/apikey). Store it in .streamlit/secrets.toml locally
# (CARTO_API_KEY = "...") and in the app's "Secrets" settings on Streamlit
# Community Cloud -- never hardcode it here, since app.py is public on GitHub.
# Falls back to plain OpenStreetMap tiles (no key needed) if it's not set.
try:
    CARTO_API_KEY = st.secrets.get("CARTO_API_KEY", "")
except Exception:
    # No secrets.toml at all (e.g. first local run before you've created one)
    CARTO_API_KEY = ""
if CARTO_API_KEY:
    # "light_nolabels" instead of "light_all" -- same muted grey Positron
    # style, but without place-name labels and the pale road-color coding
    # layered on top, for a cleaner backdrop under the dots/circles.
    CARTO_TILES = (
        "https://{s}.basemaps.cartocdn.com/rastertiles/light_nolabels/{z}/{x}/{y}{r}.png"
        f"?key={CARTO_API_KEY}"
    )
    CARTO_ATTR = (
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
        'contributors, &copy; <a href="https://carto.com/attributions">CARTO</a>'
    )
else:
    # No CARTO key configured -- Esri's legacy "Light Gray Canvas" tiles
    # remain free and keyless (unlike CARTO's raster basemaps as of 2026),
    # and are a muted grey basemap rather than full-color OpenStreetMap, so
    # the map isn't visually noisy even without a CARTO key set up.
    CARTO_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
    CARTO_ATTR = "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ"

DEFAULT_CLUSTER_RADIUS_NM = 50
MIN_MARKET_CIRCLE_RADIUS_NM = 0.5
MAX_MARKET_CIRCLE_RADIUS_NM = 60
# Circle size is relative to this reference passenger count, not an absolute
# per-passenger increment -- a market AT this many passengers gets the full
# 60nm circle; everything below scales down smoothly (sqrt curve). Tune this
# in the sidebar against your real data: set it to roughly the total_pax of
# your biggest true hub markets (ATL, LGA, LAX, MCO, etc.) so ONLY markets in
# that tier reach the max circle, rather than every mid-size market maxing
# out. There's no way to pick this correctly without seeing your actual
# passenger figures, hence it's exposed as a live control rather than fixed.
DEFAULT_REFERENCE_MAX_PAX = 50000
DEFAULT_ORIGIN_CIRCLE_RADIUS_NM = 50

# elevation-adjusted required takeoff field length curves, for the raw-file
# fallback path in load_data() — must match prepare_data.ipynb exactly
M_TO_FT = 3.28084
RESTRICTED_TOFL_M = {0: 1250, 2000: 1375, 4000: 1500}
_ALTS_FT = np.array(sorted(RESTRICTED_TOFL_M.keys()))
_RESTRICTED_TOFL_FT = np.array([RESTRICTED_TOFL_M[a] for a in _ALTS_FT]) * M_TO_FT
TOFL_CURVES_M = {
    0: [(34000, 965), (48000, 1300), (55000, 1550), (61500, 1843)],
    2000: [(34000, 985), (48000, 1420), (55000, 1700), (61500, 2020)],
    4000: [(34000, 1010), (48000, 1560), (55000, 1880), (61500, 2229)],
}
_TOFL_AT_MTOW_FT = np.array([TOFL_CURVES_M[a][-1][1] for a in _ALTS_FT]) * M_TO_FT


def required_restricted_ft(elevation_ft):
    return np.interp(elevation_ft, _ALTS_FT, _RESTRICTED_TOFL_FT)


def required_mtow_ft(elevation_ft):
    return np.interp(elevation_ft, _ALTS_FT, _TOFL_AT_MTOW_FT)


# ============================================================
# Core logic (ported directly from range_map_lib.ipynb)
# ============================================================

def geodesic_circle(lat, lon, radius_nm, n_points=180):
    radius_m = radius_nm * 1852
    geod = Geod(ellps="WGS84")
    points = []
    for angle in np.linspace(0, 360, n_points):
        lon2, lat2, _ = geod.fwd(lon, lat, angle, radius_m)
        points.append((lat2, lon2))
    return points


def get_nearby_origin_airports(origin_ident, demand_df, airports_df,
                                origin_airport_col=ORIGIN_AIRPORT_COL,
                                radius_nm=DEFAULT_CLUSTER_RADIUS_NM):
    origin_row = airports_df[airports_df["IDENT"] == origin_ident]
    if origin_row.empty:
        raise ValueError(f"Airport '{origin_ident}' not found in dataset.")
    lat0 = float(origin_row["Y"].values[0])
    lon0 = float(origin_row["X"].values[0])

    candidate_idents = demand_df[origin_airport_col].dropna().unique().tolist()
    coords = airports_df.set_index("IDENT")[["X", "Y"]]

    geod = Geod(ellps="WGS84")
    nearby = []
    for ident in candidate_idents:
        if ident not in coords.index:
            continue
        lon, lat = coords.loc[ident, "X"], coords.loc[ident, "Y"]
        _, _, dist_m = geod.inv(lon0, lat0, lon, lat)
        if (dist_m / 1852) <= radius_nm:
            nearby.append(ident)

    return nearby


def get_destination_clusters_by_origin(origin_ident, demand_df, airports_df,
                                        origin_market_col=ORIGIN_MARKET_COL,
                                        origin_airport_col=ORIGIN_AIRPORT_COL,
                                        dest_airport_col=DEST_AIRPORT_COL,
                                        pax_col=PAX_COL, fare_col=FARE_COL,
                                        manual_origin_market=None,
                                        cluster_radius_nm=DEFAULT_CLUSTER_RADIUS_NM,
                                        origin_cluster_radius_nm=None,
                                        reference_max_pax=DEFAULT_REFERENCE_MAX_PAX):
    empty_result = pd.DataFrame(columns=["anchor_airport", "anchor_lat", "anchor_lon",
                                          "total_pax", "avg_fare", "member_airports",
                                          "circle_radius_nm"])

    if manual_origin_market is not None:
        demand_df = demand_df.copy()
        demand_df[origin_market_col] = demand_df[origin_market_col].astype("Int64")
        subset = demand_df[demand_df[origin_market_col] == manual_origin_market]
        if subset.empty:
            st.info(f"No demand rows found for manual_origin_market={manual_origin_market}.")
            return empty_result
    else:
        radius = origin_cluster_radius_nm if origin_cluster_radius_nm is not None else cluster_radius_nm
        nearby_origins = get_nearby_origin_airports(
            origin_ident, demand_df, airports_df, origin_airport_col, radius
        )
        if not nearby_origins:
            st.info(f"No demand-file origin airports found within {radius} nm of {origin_ident}.")
            return empty_result
        subset = demand_df[demand_df[origin_airport_col].isin(nearby_origins)]
        if subset.empty:
            st.info(f"No demand rows found for origin cluster of {origin_ident}.")
            return empty_result

    def _agg(g):
        total = g[pax_col].sum()
        weighted_fare = np.average(g[fare_col], weights=g[pax_col]) if total > 0 else g[fare_col].mean()
        return pd.Series({"total_pax": total, "avg_fare": weighted_fare})

    airport_totals = subset.groupby(dest_airport_col).apply(_agg).reset_index()

    coords = airports_df.set_index("IDENT")[["X", "Y"]]
    airport_totals = airport_totals.merge(coords, left_on=dest_airport_col, right_index=True, how="left")
    airport_totals = airport_totals.dropna(subset=["X", "Y"])
    if airport_totals.empty:
        return empty_result

    airport_totals = airport_totals.sort_values("total_pax", ascending=False).reset_index(drop=True)
    geod = Geod(ellps="WGS84")
    assigned = set()
    clusters = []

    for _, row in airport_totals.iterrows():
        ident = row[dest_airport_col]
        if ident in assigned:
            continue
        assigned.add(ident)
        members = [row]
        for _, other in airport_totals.iterrows():
            other_ident = other[dest_airport_col]
            if other_ident in assigned:
                continue
            _, _, dist_m = geod.inv(row["X"], row["Y"], other["X"], other["Y"])
            if (dist_m / 1852) <= cluster_radius_nm:
                members.append(other)
                assigned.add(other_ident)

        total_pax = sum(m["total_pax"] for m in members)
        fares = [m["avg_fare"] for m in members]
        weights = [m["total_pax"] for m in members]
        avg_fare = np.average(fares, weights=weights) if total_pax > 0 else float(np.mean(fares))

        # Relative to reference_max_pax, not an absolute per-passenger rate --
        # a market AT reference_max_pax gets the full 60nm circle, everything
        # below scales down on a sqrt curve. Ratio is capped at 1.0 so a
        # market larger than the reference still just gets the max circle,
        # not an oversized one.
        pax_ratio = min(total_pax / reference_max_pax, 1.0) if reference_max_pax > 0 else 0.0
        circle_radius_nm = MIN_MARKET_CIRCLE_RADIUS_NM + (
            MAX_MARKET_CIRCLE_RADIUS_NM - MIN_MARKET_CIRCLE_RADIUS_NM
        ) * np.sqrt(pax_ratio)

        clusters.append({
            "anchor_airport": ident,
            "anchor_lat": row["Y"],
            "anchor_lon": row["X"],
            "total_pax": total_pax,
            "avg_fare": avg_fare,
            "member_airports": [m[dest_airport_col] for m in members],
            "circle_radius_nm": circle_radius_nm,
        })

    return pd.DataFrame(clusters)


def plot_market_circles(origin_ident, airports_df, demand_df,
                         manual_origin_market=None,
                         cluster_radius_nm=DEFAULT_CLUSTER_RADIUS_NM,
                         origin_cluster_radius_nm=None,
                         show_origin_circle=True,
                         origin_circle_radius_nm=DEFAULT_ORIGIN_CIRCLE_RADIUS_NM,
                         reference_max_pax=DEFAULT_REFERENCE_MAX_PAX,
                         zoom_start=4, save_path=None):
    origin = airports_df[airports_df["IDENT"] == origin_ident]
    if origin.empty:
        raise ValueError(f"Airport '{origin_ident}' not found in dataset.")
    lat0 = float(origin["Y"].values[0])
    lon0 = float(origin["X"].values[0])
    origin_capability = origin["capability"].values[0]
    origin_range = float(origin["range"].values[0])

    m = folium.Map(location=[lat0, lon0], zoom_start=zoom_start, tiles=CARTO_TILES, attr=CARTO_ATTR)
    folium.Marker(
        [lat0, lon0], popup=origin_ident,
        icon=folium.Icon(color="blue", icon="plane", prefix="fa"),
    ).add_to(m)

    if show_origin_circle:
        folium.PolyLine(
            geodesic_circle(lat0, lon0, origin_circle_radius_nm),
            color="#3388ff", weight=2, dash_array="6,6", opacity=0.8,
            tooltip=f"{origin_circle_radius_nm} nm from {origin_ident}",
        ).add_to(m)

    clusters = get_destination_clusters_by_origin(
        origin_ident, demand_df, airports_df,
        manual_origin_market=manual_origin_market,
        cluster_radius_nm=cluster_radius_nm,
        origin_cluster_radius_nm=origin_cluster_radius_nm,
        reference_max_pax=reference_max_pax,
    )

    if not clusters.empty:
        fare_min = clusters["avg_fare"].min()
        fare_max = clusters["avg_fare"].max()
        fare_p25 = clusters["avg_fare"].quantile(0.15)
        fare_colormap = cm.LinearColormap(
            colors=["#e74c3c", "#f1c40f", "#2ecc71"],
            index=[fare_min, fare_p25, fare_max],
            vmin=fare_min, vmax=fare_max,
            caption="Destination Cluster Avg Fare",
        )

        for _, row in clusters.iterrows():
            radius_nm = row["circle_radius_nm"]
            radius_m = radius_nm * 1852
            fill_color = fare_colormap(row["avg_fare"])
            members = ", ".join(row["member_airports"])

            folium.Circle(
                location=[row["anchor_lat"], row["anchor_lon"]],
                radius=radius_m,
                color=fill_color, weight=1.5,
                fill=True, fill_color=fill_color, fill_opacity=0.25,
                tooltip=(
                    f"Anchor: {row['anchor_airport']}<br>"
                    f"Members: {members}<br>"
                    f"Total passengers (scaled to 100%)*: {row['total_pax']:,.0f}<br>"
                    f"Avg fare: ${row['avg_fare']:,.0f}<br>"
                    f"Circle radius: {radius_nm:,.0f} nm"
                ),
            ).add_to(m)

        fare_colormap.add_to(m)

    others = airports_df[airports_df["IDENT"] != origin_ident].copy()

    geod = Geod(ellps="WGS84")
    def _dist_nm(row):
        _, _, dist_m = geod.inv(lon0, lat0, row["X"], row["Y"])
        return dist_m / 1852

    others["dist_from_origin_nm"] = others.apply(_dist_nm, axis=1)
    others["route_capability_rank"] = np.minimum(
        CAPABILITY_RANK[origin_capability],
        others["capability"].map(CAPABILITY_RANK),
    )
    others["route_capability"] = others["route_capability_rank"].map(RANK_TO_CAPABILITY)
    others["route_range_nm"] = np.minimum(origin_range, others["range"])
    others["within_route_range"] = (
        (others["route_capability_rank"] > CAPABILITY_RANK["INSUFFICIENT"])
        & (others["dist_from_origin_nm"] <= others["route_range_nm"])
    )

    for _, row in others.iterrows():
        cap = row["route_capability"]
        dist = row["dist_from_origin_nm"]
        reachable = bool(row["within_route_range"])

        if cap == "MTOW":
            color = MTOW_DOT_COLOR
        elif cap == "RESTRICTED":
            color = RESTRICTED_DOT_COLOR
        else:
            color = INSUFFICIENT_DOT_COLOR

        fill = reachable and cap != "INSUFFICIENT"
        fill_opacity = 1.0 if fill else 0.0
        opacity = 0.9 if cap != "INSUFFICIENT" else 0.5

        if cap == "INSUFFICIENT":
            status = (
                f"Route insufficient -- {origin_ident} ({origin_capability}) or "
                f"{row['IDENT']} ({row['capability']}) runway too short for this route"
            )
        elif reachable:
            status = (
                f"{cap.title()}-capable route -- reachable to AND from "
                f"(within {row['route_range_nm']:,.0f} nm combined range)"
            )
        else:
            status = (
                f"{cap.title()}-capable route, but out of range "
                f"(needs &le;{row['route_range_nm']:,.0f} nm, this trip is {dist:,.0f} nm)"
            )

        folium.CircleMarker(
            location=[row["Y"], row["X"]],
            radius=AIRPORT_DOT_RADIUS_PX,
            color=color, weight=0.75,
            fill=fill, fill_color=color, fill_opacity=fill_opacity, opacity=opacity,
            tooltip=f"{row['IDENT']}<br>{status}<br>{dist:,.0f} nm from {origin_ident}",
        ).add_to(m)

    legend_html = f"""
    <div style="position: fixed; bottom: 30px; left: 30px; z-index:9999;
                background: white; padding: 10px 14px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 13px; color: #000000;">
        <b>{origin_ident} Demand Map</b><br>
        <span style="color:#3388ff;">- - -</span> {origin_circle_radius_nm} nm clustering reference circle<br>
        &#9679; Black = MTOW-capable route (both ends) &nbsp; &#9679; <span style="color:{RESTRICTED_DOT_COLOR};">Orange</span> = Restricted-capable route (both ends)<br>
        &#9675; Grey outline = Route insufficient (either runway too short)<br>
        <b>Filled</b> = reachable to AND from origin (within both airports' combined range)<br>
        <b>Outline</b> = runway ok but out of range, or route insufficient<br>
        Shaded circle = destination cluster demand<br>
        (size = passengers, color = avg fare, green = high)<br>
        Both origin and destination sides cluster airports<br>
        within {cluster_radius_nm} nm of each other
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    if save_path:
        m.save(save_path)

    return m, others, clusters


# ============================================================
# Data loading (cached — this is the slow step, runs once)
# ============================================================

@st.cache_data(show_spinner="Loading airport & demand data...")
def load_data():
    """
    Fast path: reads prebuilt airports_prepared.pkl / demand_prepared.pkl from
    data/ if present (produced by build_data.py).

    Fallback: builds them from raw Airports.csv / Runways.csv / markets.xlsx
    in data/, using the same formulas as prepare_data.ipynb.
    """
    airports_pkl = DATA_DIR / "airports_prepared.pkl"
    demand_pkl = DATA_DIR / "demand_prepared.pkl"

    if airports_pkl.exists() and demand_pkl.exists():
        airports = pd.read_pickle(airports_pkl)
        demand = pd.read_pickle(demand_pkl)
        if "capability" not in airports.columns or "range" not in airports.columns:
            raise ValueError(
                "airports_prepared.pkl is in the OLD format (no 'capability'/'range' "
                "columns). Re-run build_data.py to regenerate it in the new format."
            )
        return airports, demand

    airports_csv = DATA_DIR / "Airports.csv"
    runways_csv = DATA_DIR / "Runways.csv"
    markets_xlsx = DATA_DIR / "markets.xlsx"
    missing = [p.name for p in (airports_csv, runways_csv, markets_xlsx) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "No prebuilt pickles found, and these raw source files are missing from "
            f"the data/ folder: {', '.join(missing)}."
        )

    airports = pd.read_csv(airports_csv)
    runways = pd.read_csv(runways_csv)
    longest_runways = (
        runways.sort_values(by="LENGTH", ascending=False)
        .drop_duplicates(subset="AIRPORT_ID", keep="first")[["AIRPORT_ID", "LENGTH", "WIDTH"]]
    )
    airports = airports.merge(longest_runways, left_on="GLOBAL_ID", right_on="AIRPORT_ID", how="left")
    airports = airports[["X", "Y", "IDENT", "LENGTH", "WIDTH", "ELEVATION"]].copy()

    airports["required_restricted"] = required_restricted_ft(airports["ELEVATION"].values)
    airports["restricted"] = np.where(airports["LENGTH"] > airports["required_restricted"], 1, 0)
    airports = airports[airports["ELEVATION"] < 4000]
    airports["required_MTOW"] = required_mtow_ft(airports["ELEVATION"].values)
    airports["MTOW"] = np.where(airports["LENGTH"] > airports["required_MTOW"], 1, 0)
    airports["capability"] = np.select(
        condlist=[airports["LENGTH"] > airports["required_MTOW"], airports["LENGTH"] > airports["required_restricted"]],
        choicelist=["MTOW", "RESTRICTED"],
        default="INSUFFICIENT",
    )
    airports["range"] = (1.452 * airports["LENGTH"] - 5454).clip(upper=3000)
    airports = airports[~airports["IDENT"].str.contains(r"\d", na=False)]

    demand = pd.read_excel(markets_xlsx)
    demand[PAX_COL] = demand[PAX_COL] / OD_SAMPLE_RATE
    return airports, demand


# ============================================================
# UI
# ============================================================

st.title("\u2708\ufe0f Avelo E195-E2 Range & Market Demand Explorer")
st.caption(
    "Dot color = route capability (the weaker of the origin's and destination's own "
    "runway capability). Filled = reachable to AND from origin. Shaded circles = "
    "geographically clustered destination market demand."
)

try:
    airports, demand = load_data()
except (FileNotFoundError, ValueError) as e:
    st.error(str(e))
    st.stop()

with st.sidebar:
    st.header("Settings")
    idents = sorted(airports["IDENT"].dropna().unique())
    default_index = idents.index("TKI") if "TKI" in idents else 0
    origin = st.selectbox("Origin airport (IDENT)", idents, index=default_index)

    st.subheader("Clustering")
    cluster_radius_nm = st.slider(
        "Cluster radius (nm)", min_value=10, max_value=150,
        value=DEFAULT_CLUSTER_RADIUS_NM, step=5,
        help="Airports within this distance of each other are treated as one market, "
             "on both the origin side (pulls in nearby airports' demand rows) and the "
             "destination side (e.g. groups MCO and SFB into one circle).",
    )
    show_origin_circle = st.checkbox("Show clustering reference circle", value=True)

    st.subheader("Circle sizing")
    reference_max_pax = st.number_input(
        "Passengers for max circle (60nm)",
        min_value=1000, max_value=1_000_000,
        value=DEFAULT_REFERENCE_MAX_PAX, step=1000,
        help="A market at this many passengers (scaled to 100%) gets the full 60nm "
             "circle; smaller markets scale down smoothly. Tune this against your own "
             "data: pick an origin with a known mega-hub market (ATL, LGA, LAX, MCO, "
             "etc.), note that market's passenger total below, and set this close to "
             "it so only markets in that tier reach the max circle.",
    )

    with st.expander("Advanced"):
        manual_market_str = st.text_input(
            "Manual origin market override",
            value="",
            help="Bypasses geographic clustering and filters demand to this exact "
                 "market code instead. Leave blank to use geographic clustering.",
        )
        manual_market = int(manual_market_str) if manual_market_str.strip().isdigit() else None

    st.divider()
    st.caption(
        "Built with Streamlit + folium. Data: FAA airport/runway data and DOT DB1B "
        "market data. Portfolio project by Otto Horiuchi."
    )

try:
    m, others, clusters = plot_market_circles(
        origin, airports, demand,
        manual_origin_market=manual_market,
        cluster_radius_nm=cluster_radius_nm,
        show_origin_circle=show_origin_circle,
        origin_circle_radius_nm=cluster_radius_nm,
        reference_max_pax=reference_max_pax,
    )
except ValueError as e:
    st.error(str(e))
    st.stop()

reachable = others[others["within_route_range"]]
col1, col2, col3 = st.columns(3)
col1.metric("Reachable airports", len(reachable))
col2.metric("MTOW-capable routes", (reachable["route_capability"] == "MTOW").sum())
col3.metric("Restricted-only routes", (reachable["route_capability"] == "RESTRICTED").sum())

st_folium(m, width=None, height=650, returned_objects=[])

# --- top destination markets, each as a section with a secondary-airport sub-table ---
st.subheader(f"Top destination markets from {origin}")

if clusters.empty:
    st.info("No destination market data available for this origin.")
else:
    top_markets = clusters.sort_values("total_pax", ascending=False).head(10).copy()
    geod = Geod(ellps="WGS84")

    def _airports_within_circle(anchor_ident, anchor_lat, anchor_lon, radius_nm, others_df):
        """
        Every USABLE airport (route_capability != INSUFFICIENT, within combined
        range of the selected origin) whose actual location falls inside this
        market's drawn demand circle -- regardless of whether that airport has
        any demand rows of its own. This is geographic, matching what's visibly
        drawn on the map, not based on which airports happen to have demand data.
        """
        candidates = others_df[others_df["IDENT"] != anchor_ident].copy()
        _, _, dist_m = geod.inv(
            np.full(len(candidates), anchor_lon), np.full(len(candidates), anchor_lat),
            candidates["X"].values, candidates["Y"].values,
        )
        candidates["dist_from_anchor_nm"] = dist_m / 1852
        candidates = candidates[
            (candidates["dist_from_anchor_nm"] <= radius_nm)
            & (candidates["within_route_range"])
        ]
        return candidates.sort_values("dist_from_anchor_nm")

    chart_data = pd.DataFrame(
        {"Total passengers (scaled to 100%)*": top_markets["total_pax"].values},
        index=top_markets["anchor_airport"],
    )
    st.bar_chart(
        chart_data,
        horizontal=True,
        sort="-Total passengers (scaled to 100%)*",
    )

    for _, row in top_markets.iterrows():
        anchor = row["anchor_airport"]
        secondary_df = _airports_within_circle(
            anchor, row["anchor_lat"], row["anchor_lon"], row["circle_radius_nm"], others
        )

        st.markdown(
            f"**Market anchor: {anchor}**  \n"
            f"Total passengers (scaled to 100%)\\*: {row['total_pax']:,.0f}  \n"
            f"Avg fare: ${row['avg_fare']:,.0f}"
        )

        if not secondary_df.empty:
            secondary_table = pd.DataFrame({
                "Secondary airport": secondary_df["IDENT"].values,
                "Runway length (ft)": secondary_df["LENGTH"].map("{:,.0f}".format).values,
            })
            st.dataframe(secondary_table, hide_index=True, use_container_width=True)
        else:
            st.caption("No secondary/tertiary airports in this market.")

        st.divider()

st.divider()
st.caption(
    f"\\* Passenger volumes are scaled from DOT's Origin-Destination 40% Survey "
    f"(OD40) \u2014 a true {OD_SAMPLE_RATE:.0%} sample of tickets, per "
    f"[bts.gov/OD-40](https://www.bts.gov/OD-40) \u2014 up by a factor of "
    f"{1 / OD_SAMPLE_RATE:g}x to represent estimated 100% traffic volume, "
    "not raw sampled counts."
)
