# Avelo E195-E2 Range & Market Demand Explorer

An interactive Streamlit dashboard version of the range/demand map workflow:
pick an origin airport, see everywhere the E195-E2 can reach under MTOW vs.
weight-restricted range, sized by destination-market passenger demand and
colored by average fare.

## 1. Project structure

```
avelo-dashboard/
├── app.py              # the dashboard (this is the only code file you need)
├── requirements.txt
├── README.md
└── data/                # you add this — see below
    ├── airports_prepared.pkl      \  fast path (recommended): the same
    ├── demand_prepared.pkl         > pickles Avelo_ViewMap.ipynb already
    └── airport_market_map.pkl     /  loads
    # OR, if you'd rather let the app build them itself:
    ├── Airports.csv
    ├── Runways.csv
    └── markets.xlsx
```

Note there are now only two pickles (`airport_market_map.pkl` is gone —
destination grouping is geographic now, computed on the fly from
coordinates, not from a precomputed market-code lookup).

The app checks for the two `.pkl` files first (fast, loads instantly) and
falls back to building them from the raw `Airports.csv` / `Runways.csv` /
`markets.xlsx` if the pickles aren't there. Either way works — just drop
whichever set of files you have into a `data/` folder next to `app.py`.

**If you have pickles from an earlier version of this project:** the data
schema changed (runway capability is now a continuous, elevation-adjusted
`capability`/`range` pair instead of the old fixed MTOW/restricted nm
constants, and there's no more `airport_market_map.pkl` — destination
grouping is now geographic, not market-code-based). Delete any old pickles
and re-run `python build_data.py` to regenerate them in the new format;
`app.py` will raise a clear error if it finds old-format pickles rather than
silently producing wrong results.

## 2. Run it locally first

```bash
cd avelo-dashboard
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
streamlit run app.py
```

It'll open at `http://localhost:8501`. Confirm the airport dropdown, range
inputs, and map all work before deploying.

## 3. Put it on GitHub

```bash
git init
git add app.py requirements.txt README.md data/
git commit -m "Avelo range/demand dashboard"
git branch -M main
git remote add origin https://github.com/<your-username>/avelo-dashboard.git
git push -u origin main
```

**One thing to check first:** GitHub blocks files over 100MB, and repos get
slow above ~1GB. If `markets.xlsx` (DB1B market data) or the pickles are
large:
- Prefer the pickles over the raw CSV/xlsx — they're usually much smaller.
- If still too big, use [Git LFS](https://git-lfs.github.com/) for the data
  files, or host the data file(s) somewhere public (e.g. a public S3/GCS
  URL) and have `load_data()` fetch it with `pd.read_pickle(url)` /
  `pd.read_csv(url)` instead of reading from `data/`.
- FAA airport/runway data and DOT DB1B market data are both public, so
  there's no privacy concern with committing them — just a size one.

## 4. Deploy for free on Streamlit Community Cloud

1. Go to **share.streamlit.io** and sign in with your GitHub account.
2. Click **"Create app"** → **"Deploy a public app from GitHub"**.
3. Pick your `avelo-dashboard` repo, the `main` branch, and `app.py` as the
   entry point.
4. Click **Deploy**. First build takes a couple minutes (installing
   dependencies); after that it's cached and fast.
5. You'll get a public URL like:

   ```
   https://avelo-dashboard-<random-or-chosen-suffix>.streamlit.app
   ```

   You can pick a custom subdomain slug in the deploy settings — e.g.
   `avelo-range-demand.streamlit.app` — which is the one worth putting on a
   resume or portfolio site.

That's it — no server to manage, and it stays live and free on the
Community Cloud tier (it does spin down after inactivity and take a few
seconds to wake back up on the next visit, which is normal and fine for a
portfolio link).

## 5. Making it portfolio-ready

A few small additions that go a long way for a resume/portfolio link:
- Add a one-paragraph "About this project" section (in an `st.expander` or
  the sidebar) explaining the methodology: geodesic range rings, runway
  length vs. weight-adjusted required runway length, market-level demand
  aggregation from DOT DB1B data.
- Link back to the GitHub repo from inside the app (`st.markdown("[View
  source](https://github.com/...)")`) so visitors can see the code, not just
  the demo.
- Consider adding a default/example airport that loads with a compelling
  map on first load, since that's the first thing anyone sees.

## 6. Updating it later

Any `git push` to `main` triggers an automatic redeploy on Streamlit
Community Cloud — no separate deploy step needed.
