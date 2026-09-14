"""
build_data.py
--------------
Run this once, and again anytime your source files change (new Airports.csv,
Runways.csv, or markets.xlsx). Ports the exact logic from prepare_data.ipynb:
elevation-adjusted required takeoff field length curves, the MTOW/RESTRICTED/
INSUFFICIENT capability label, and the continuous runway-length-based `range`
value. Saves the results as pickles that app.py loads instantly.

Usage:
    python build_data.py
"""

import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

M_TO_FT = 3.28084

# --- "Restricted" case: full-pax, 500 nm reserve fuel weight (~52,000 kg) ---
RESTRICTED_TOFL_M = {
    0: 1250,
    2000: 1375,
    4000: 1500,
}
_ALTS_FT = np.array(sorted(RESTRICTED_TOFL_M.keys()))
_RESTRICTED_TOFL_FT = np.array([RESTRICTED_TOFL_M[a] for a in _ALTS_FT]) * M_TO_FT


def required_restricted_ft(elevation_ft):
    return np.interp(elevation_ft, _ALTS_FT, _RESTRICTED_TOFL_FT)


# --- MTOW case ---
TOFL_CURVES_M = {
    0: [(34000, 965), (48000, 1300), (55000, 1550), (61500, 1843)],
    2000: [(34000, 985), (48000, 1420), (55000, 1700), (61500, 2020)],
    4000: [(34000, 1010), (48000, 1560), (55000, 1880), (61500, 2229)],
}
_TOFL_AT_MTOW_FT = np.array([TOFL_CURVES_M[a][-1][1] for a in _ALTS_FT]) * M_TO_FT


def required_mtow_ft(elevation_ft):
    return np.interp(elevation_ft, _ALTS_FT, _TOFL_AT_MTOW_FT)


def main():
    required = ["Airports.csv", "Runways.csv", "markets.xlsx"]
    missing = [f for f in required if not (DATA_DIR / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing from {DATA_DIR}: {', '.join(missing)}. "
            "Place your raw source files there first."
        )

    print("Loading airports and runways...")
    airports = pd.read_csv(DATA_DIR / "Airports.csv")
    runways = pd.read_csv(DATA_DIR / "Runways.csv")

    longest_runways = runways.sort_values(by="LENGTH", ascending=False)
    longest_runways = longest_runways.drop_duplicates(subset="AIRPORT_ID", keep="first")
    longest_runways = longest_runways[["AIRPORT_ID", "LENGTH", "WIDTH"]]

    airports = pd.merge(
        airports, longest_runways,
        left_on="GLOBAL_ID", right_on="AIRPORT_ID", how="left",
    )

    airports = airports[["X", "Y", "IDENT", "LENGTH", "WIDTH", "ELEVATION"]].copy()

    print("Computing elevation-adjusted required runway lengths...")
    airports["required_restricted"] = required_restricted_ft(airports["ELEVATION"].values)
    airports["restricted"] = np.where(airports["LENGTH"] > airports["required_restricted"], 1, 0)
    airports = airports[airports["ELEVATION"] < 4000]
    airports["required_MTOW"] = required_mtow_ft(airports["ELEVATION"].values)
    airports["MTOW"] = np.where(airports["LENGTH"] > airports["required_MTOW"], 1, 0)

    # Combined capability label -- MTOW checked first since it's a superset
    # (any runway long enough for MTOW is also long enough for restricted)
    airports["capability"] = np.select(
        condlist=[
            airports["LENGTH"] > airports["required_MTOW"],
            airports["LENGTH"] > airports["required_restricted"],
        ],
        choicelist=["MTOW", "RESTRICTED"],
        default="INSUFFICIENT",
    )

    airports["range"] = (1.452 * airports["LENGTH"] - 5454).clip(upper=3000)

    airports = airports[~airports["IDENT"].str.contains(r"\d", na=False)]

    print("Reading demand (market) data...")
    demand = pd.read_excel(DATA_DIR / "markets.xlsx")

    # OD40 (bts.gov/OD-40) samples 40% of tickets, not 100% -- scale up so
    # passenger totals reflect true volume rather than the raw sample.
    OD_SAMPLE_RATE = 0.40
    PAX_COL = "passengers"
    print(f"Scaling passenger counts from the OD40 {OD_SAMPLE_RATE:.0%} sample to 100% volume...")
    demand[PAX_COL] = demand[PAX_COL] / OD_SAMPLE_RATE

    print("Saving prepared pickles to data/ ...")
    airports.to_pickle(DATA_DIR / "airports_prepared.pkl")
    demand.to_pickle(DATA_DIR / "demand_prepared.pkl")

    print(
        f"Done. {len(airports)} airports, {len(demand)} demand rows. "
        "Capability breakdown: "
        f"{dict(airports['capability'].value_counts())}"
    )


if __name__ == "__main__":
    main()
