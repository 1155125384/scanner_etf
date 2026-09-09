import hashlib
import hmac
import base64
import json
import os
import uuid
import urllib.parse
from datetime import datetime, timezone
from webullsdkcore.client import ApiClient
from webullsdktrade.api import API
from webullsdkcore.common.region import Region
from webullsdkmdata.common.category import Category
import ftplib

import requests
import yfinance as yf
import pandas as pd
import numpy as np
import time
from tqdm import tqdm
import sys
from io import StringIO
import io
import logging

APP_KEY = os.getenv('APP_KEY')
APP_SECRET = os.getenv('APP_SECRET')
HOST = "api.webull.hk" 
BASE_URL = f"https://{HOST}"
ACCESS_TOKEN = os.getenv("WEBULL_ACCESS_TOKEN", "").strip()


def generate_signature(path, query_params, body_string, app_key, app_secret, host, timestamp, nonce):
    signing_headers = {
        "x-app-key": app_key,
        "x-timestamp": timestamp,
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-version": "1.0",
        "x-signature-nonce": nonce,
        "host": host,
    }

    all_params = {}
    all_params.update(query_params)
    all_params.update(signing_headers)
    str1 = "&".join(f"{k}={all_params[k]}" for k in sorted(all_params.keys()))
    if body_string:
        str2 = hashlib.md5(body_string.encode("utf-8")).hexdigest().upper()
        str3 = f"{path}&{str1}&{str2}"
    else:
        str3 = f"{path}&{str1}"
    encoded_string = urllib.parse.quote(str3, safe="")

    signing_key = f"{app_secret}&"
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), encoded_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")

    return signature


def call_api(method, path, query_params=None, body=None, access_token=None):
    query_params = query_params or {}
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = uuid.uuid4().hex

    body_string = json.dumps(body, separators=(",", ":")) if body else None

    signature = generate_signature(
        path, query_params, body_string,
        APP_KEY, APP_SECRET, HOST, timestamp, nonce,
    )

    headers = {
        "Accept": "application/json",
        "x-app-key": APP_KEY,
        "x-timestamp": timestamp,
        "x-signature": signature,
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-version": "1.0",
        "x-signature-nonce": nonce,
        "x-version": "v2",
    }
    if access_token is not None:
        if not access_token:
            raise ValueError(
                "WEBULL_ACCESS_TOKEN is not set. Set it in the environment and rerun this cell."
            )
        headers["x-access-token"] = access_token

    url = f"{BASE_URL}{path}"

    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, params=query_params)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, data=body_string)

    return resp

# --- Create access token ---
token_response = call_api("POST", "/auth/tokens/create")
print(f"Status: {token_response.status_code}")

try:
    token_data = token_response.json()
except ValueError:
    token_data = None

if token_response.ok and isinstance(token_data, dict):
    ACCESS_TOKEN = (
        token_data.get("access_token")
        or token_data.get("accessToken")
        or token_data.get("token")
    )
    if not ACCESS_TOKEN:
        raise KeyError(f"Token field not found in response: {token_data}")
    print("Access token created successfully.")
else:
    print(token_response.text)


def fetch_ftp_file(filename):
    ftp = ftplib.FTP("ftp.nasdaqtrader.com")
    ftp.login()
    ftp.cwd("symboldirectory")
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {filename}", buf.write)
    ftp.quit()
    return buf.getvalue().decode("utf-8")

# 抓两个档案
nasdaq_text = fetch_ftp_file("nasdaqlisted.txt")
other_text = fetch_ftp_file("otherlisted.txt")

# 转成 DataFrame(最后一行是档案时间戳footer,要去掉)
nasdaq = pd.read_csv(io.StringIO(nasdaq_text), sep="|")[:-1]
other = pd.read_csv(io.StringIO(other_text), sep="|")[:-1]

# 筛选 ETF
nasdaq_etfs = nasdaq[nasdaq["ETF"] == "Y"][["Symbol", "Security Name"]]
other_etfs = other[other["ETF"] == "Y"][["ACT Symbol", "Security Name"]].rename(
    columns={"ACT Symbol": "Symbol"}
)

all_etfs = (
    pd.concat([nasdaq_etfs, other_etfs], ignore_index=True)
    .drop_duplicates(subset="Symbol")
    .sort_values(by="Symbol")
    .reset_index(drop=True)
)

print(f"Total US ETFs found: {len(all_etfs)}")
with pd.option_context('display.max_rows', 10, 'display.max_columns', None):
    print(all_etfs)

api_client = ApiClient(APP_KEY, APP_SECRET, Region.HK.value)
api = API(api_client)

res_acct = api.account.get_app_subscriptions()
account_id = None

result = res_acct.json()
account_id = result[0]['account_id']

res_stock = api.account.get_account_position(account_id,page_size=100)
account_position = res_stock.json()

holdings = account_position.get("holdings", [])
current_holdings_list = [item['symbol'] for item in holdings]

print("My Current Holdings:", current_holdings_list)

holdings = current_holdings_list

results = []

# tqdm wraps the iterable and renders a live progress bar in the terminal/notebook
for ticker in tqdm(holdings, desc="Fetching ticker data", unit="ticker"):
    try:
        info = yf.Ticker(ticker).info
        quote_type = info.get('quoteType', 'UNKNOWN')
        long_name = info.get('longName', info.get('shortName', ''))
        results.append({'Ticker': ticker, 'Type': quote_type, 'Name': long_name})
    except Exception as e:
        results.append({'Ticker': ticker, 'Type': 'ERROR', 'Name': str(e)})
    time.sleep(0.3)

df = pd.DataFrame(results)

stocks = df[df['Type'] == 'EQUITY']
etfs = df[df['Type'] == 'ETF']
other = df[~df['Type'].isin(['EQUITY', 'ETF'])]

current_etf_holdings = etfs['Ticker'].tolist()

print(current_etf_holdings)

etf_symbols = (
    all_etfs["Symbol"]
    .dropna()
    .astype(str)
    .str.strip()
    .loc[lambda values: values.ne("")]
    .drop_duplicates()
    .tolist()
)

etf_symbols = list(dict.fromkeys(list(etf_symbols) + current_etf_holdings))

fund_rating_rows = []
fund_rating_errors = []
request_delay_seconds = 0.1
retry_wait_seconds = 10
max_retries = 5
total_symbols = len(etf_symbols)
started_at = time.time()


def show_progress(completed, current_symbol, state="requesting"):
    elapsed = time.time() - started_at
    rate = completed / elapsed if elapsed > 0 and completed else 0
    remaining = (total_symbols - completed) / rate if rate > 0 else 0
    bar_length = 30
    filled = int(bar_length * completed / total_symbols) if total_symbols else 0
    progress_bar = "#" * filled + "-" * (bar_length - filled)
    eta = f"ETA {remaining / 60:.1f} min" if rate else "ETA calculating"
    percentage = completed / total_symbols if total_symbols else 0
    message = (
        f"\r[{progress_bar}] {completed:>4}/{total_symbols} "
        f"({percentage:.1%}) | "
        f"{current_symbol:<6} | Success {len(fund_rating_rows):>4} | "
        f"Failed {len(fund_rating_errors):>3}"
    )
    sys.stdout.write(message[:160].ljust(160))
    sys.stdout.flush()


def wait_with_progress(seconds, completed, symbol, reason):
    end_time = time.time() + seconds
    while True:
        seconds_left = max(0, int(end_time - time.time() + 0.999))
        show_progress(completed, symbol, f"{reason}, wait {seconds_left}s")
        if seconds_left == 0:
            break
        time.sleep(min(0.1, seconds_left))


print(f"Starting ETF fund ratings download for {total_symbols} symbols at {datetime.now():%H:%M:%S}")
show_progress(0, "-", "starting")

for completed, symbol in enumerate(etf_symbols, start=1):
    show_progress(completed - 1, symbol, "requesting")
    for attempt in range(max_retries + 1):
        try:
            rating_response = call_api(
                "GET",
                "/market-data/fundamentals/fund-ratings/get",
                query_params={"symbol": symbol, "category": "US_STOCK"},
                access_token=ACCESS_TOKEN,
            )

            if rating_response.status_code == 417:
                try:
                    error_payload = rating_response.json()
                    error_message = error_payload.get("message", rating_response.text)
                except ValueError:
                    error_message = rating_response.text
                fund_rating_errors.append(
                    {
                        "symbol": symbol,
                        "error": f"Skipped: {error_message}",
                    }
                )
                break

            if rating_response.status_code == 429:
                if attempt == max_retries:
                    raise requests.HTTPError("Rate limit remained active after retries")

                retry_after = rating_response.headers.get("Retry-After")
                try:
                    wait_seconds = (
                        max(retry_wait_seconds, float(retry_after))
                        if retry_after
                        else retry_wait_seconds
                    )
                except ValueError:
                    wait_seconds = retry_wait_seconds
                wait_with_progress(wait_seconds, completed - 1, symbol, "rate limited")
                continue

            rating_response.raise_for_status()
            ratings = rating_response.json()
            if not isinstance(ratings, list):
                raise TypeError("Expected fund ratings response to be a list")

            for rating in ratings:
                if not isinstance(rating, dict):
                    raise TypeError("Expected each fund rating to be an object")
                fund_rating_rows.append(
                    {
                        "symbol": symbol,
                        "rating_date": rating.get("rating_date"),
                        "rating_agency": rating.get("rating_agency"),
                        "rating_cycle": rating.get("rating_cycle"),
                        "rating_results": rating.get("rating_results"),
                    }
                )
            break
        except (requests.RequestException, ValueError, TypeError) as error:
            if attempt == max_retries:
                fund_rating_errors.append({"symbol": symbol, "error": str(error)})
                break
            wait_with_progress(min(60, 2 ** attempt * 2), completed - 1, symbol, "retrying")

    wait_with_progress(request_delay_seconds, completed, symbol, "throttling")
    show_progress(completed, symbol, "completed")

sys.stdout.write("\n")
etf_ratings_df = pd.DataFrame(fund_rating_rows)
if not etf_ratings_df.empty:
    etf_ratings_df["rating_cycle"] = pd.to_numeric(
        etf_ratings_df["rating_cycle"], errors="coerce"
    ).astype("Int64")
    etf_ratings_df["rating_results"] = pd.to_numeric(
        etf_ratings_df["rating_results"], errors="coerce"
    ).astype("Int64")
    etf_ratings_df = etf_ratings_df.sort_values(
        by=["symbol", "rating_agency", "rating_cycle"],
        ascending=[True, True, True],
        ignore_index=True,
    )

print(f"Finished at {datetime.now():%H:%M:%S}")
print(f"ETF ratings received: {len(etf_ratings_df)} rows for {total_symbols} symbols")
if fund_rating_errors:
    print(f"ETF ratings failed or skipped: {len(fund_rating_errors)}")
    print(pd.DataFrame(fund_rating_errors))

print(etf_ratings_df)

# --- Summarize weighted rating for every ETF symbol ---
ratings_for_summary = etf_ratings_df.copy()
ratings_for_summary["rating_cycle"] = pd.to_numeric(
    ratings_for_summary["rating_cycle"], errors="coerce"
)
ratings_for_summary["rating_results"] = pd.to_numeric(
    ratings_for_summary["rating_results"], errors="coerce"
)
ratings_for_summary["rating_date"] = pd.to_datetime(
    ratings_for_summary["rating_date"], errors="coerce"
)

# Keep only ratings from the trailing 1 year of data (relative to the
# most recent rating_date present, not necessarily "today").
cutoff_date = ratings_for_summary["rating_date"].max() - pd.DateOffset(years=1)
ratings_for_summary = ratings_for_summary[
    ratings_for_summary["rating_date"] >= cutoff_date
]

# Average duplicate agency results within each symbol and rating cycle.
cycle_ratings = (
    ratings_for_summary.dropna(subset=["symbol", "rating_cycle", "rating_results"])
    .groupby(["symbol", "rating_cycle"], as_index=False)["rating_results"]
    .mean()
)

cycle_weights = {3: 1.0, 5: 1.5, 10: 2.0}

def calculate_weighted_rating(symbol_ratings):
    applicable = symbol_ratings[
        symbol_ratings["rating_cycle"].isin(cycle_weights)
    ].copy()
    if applicable.empty:
        return pd.Series(
            {
                "weighted_rating": pd.NA,
                "rating_cycles": "",
            }
        )

    applicable["weight"] = applicable["rating_cycle"].map(cycle_weights)
    weighted_rating = (
        (applicable["rating_results"] * applicable["weight"]).sum()
        / applicable["weight"].sum()
    )
    cycles = ", ".join(
        f"{int(cycle)}yr"
        for cycle in sorted(applicable["rating_cycle"].unique())
    )
    return pd.Series(
        {
            "weighted_rating": weighted_rating,
            "rating_cycles": cycles,
        }
    )


etf_weighted_ratings_df = (
    cycle_ratings.groupby("symbol", group_keys=False)
    .apply(calculate_weighted_rating)
    .reset_index()
)

etf_weighted_ratings_df["weighted_rating"] = pd.to_numeric(
    etf_weighted_ratings_df["weighted_rating"], errors="coerce"
).round(1)

# Most recent rating_date (within the 1-year window) contributing to each symbol.
latest_rating_date = (
    ratings_for_summary.dropna(subset=["symbol", "rating_date"])
    .groupby("symbol", as_index=False)["rating_date"]
    .max()
)

etf_weighted_ratings_df = etf_weighted_ratings_df.merge(
    latest_rating_date, on="symbol", how="left"
)

current_etf_holdings_set = set(current_etf_holdings)
etf_weighted_ratings_df = etf_weighted_ratings_df[
    etf_weighted_ratings_df["weighted_rating"].ge(4)
    | etf_weighted_ratings_df["symbol"].isin(current_etf_holdings_set)
].reset_index(drop=True)

sorted_df = etf_weighted_ratings_df.sort_values(
    by=['weighted_rating', 'rating_cycles'],
    ascending=[False, False]
).reset_index(drop=True)

filtered_df = sorted_df[sorted_df['weighted_rating'] >= 4.0]
filtered_df = filtered_df[["symbol", "rating_date", "weighted_rating", "rating_cycles"]]

print(filtered_df)

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ----------------------------------------------------------------------------
# 0. INPUT DATA
# ----------------------------------------------------------------------------
# filtered_df must exist before this point (symbol / rating_date /
# weighted_rating / rating_cycles columns), e.g. loaded from a prior step:
#   filtered_df = pd.read_csv("your_ratings_input.csv")
if "filtered_df" not in globals():
    raise RuntimeError(
        "filtered_df is not defined. Load your ratings DataFrame "
        "(with columns: symbol, rating_date, weighted_rating, rating_cycles) "
        "before running this script."
    )

# FIX (#minor-dupes): guard against duplicate symbols silently clobbering each
# other in the rating lookup (to_dict('index') keeps only the last row for a
# repeated key with no warning). Keep the last occurrence explicitly and warn.
_dupe_mask = filtered_df["symbol"].duplicated(keep=False)
if _dupe_mask.any():
    _dupes = sorted(filtered_df.loc[_dupe_mask, "symbol"].unique().tolist())
    print(
        f"WARNING: filtered_df has duplicate symbol rows for {_dupes}; "
        f"keeping the last occurrence of each and dropping the rest."
    )
    filtered_df = filtered_df.drop_duplicates(subset="symbol", keep="last").reset_index(drop=True)

# ----------------------------------------------------------------------------
# 1. CONFIG
# ----------------------------------------------------------------------------
TICKERS = filtered_df["symbol"].tolist()
RATING_SCORES = filtered_df.set_index("symbol")[
    ["rating_date", "weighted_rating", "rating_cycles"]
].to_dict("index")

# weighted_rating is on a ~1-5 star-style scale; rescale to 0-100 so it's on
# the same footing as technical_score before blending.
RATING_SCALE_MAX = 5

# FIX (#5 orig): fail loudly if weighted_rating isn't actually on the assumed
# 0-5 scale, instead of silently producing compressed/out-of-range scores.
_rating_vals = filtered_df["weighted_rating"].dropna()
if not _rating_vals.empty:
    _bad_ratings = _rating_vals[(_rating_vals < 0) | (_rating_vals > RATING_SCALE_MAX)]
    if not _bad_ratings.empty:
        raise ValueError(
            f"weighted_rating has {len(_bad_ratings)} value(s) outside the "
            f"expected 0-{RATING_SCALE_MAX} range (e.g. {_bad_ratings.head(3).tolist()}). "
            f"Check RATING_SCALE_MAX or the input data before running the screen."
        )


def rating_to_100(r):
    return (r / RATING_SCALE_MAX) * 100 if pd.notna(r) else np.nan


# How much weight the backward-looking fund rating gets vs. the forward-looking
# technical read. Set explicitly rather than inherited from the single-stock
# screen, since a Morningstar-style rating and a sell-side analyst rating are
# different kinds of signal.
RATING_WEIGHT = 0.2
TECHNICAL_WEIGHT = 1 - RATING_WEIGHT

# Pre-classified based on each fund's actual strategy/prospectus. Also means
# classify_etf() skips its live yf.Ticker(ticker).info lookup for these
# tickers entirely.
CATEGORY_OVERRIDES = {
    "BBLU": "equity_us",   # EA Bridgeway Blue Chip ETF -- active US large-cap blend
    "BCPL": "bond",        # BNY Mellon Core Plus ETF -- core-plus fixed income
    "CEFZ": "equity_us",   # RiverNorth Active Income ETF -- actually multi-asset
                            # (equities + bonds via closed-end funds); no bucket
                            # here fits cleanly, worth a manual look later
    "CLSE": "equity_us",   # Convergence Long/Short Equity ETF -- benchmarked to
                            # Russell 3000, but net exposure floats 50-100%, so
                            # it will structurally lag/lead SPY even when it's
                            # doing exactly what it's designed to do
    "DBAW": "equity_intl", # Xtrackers MSCI All World ex USA Hedged Equity ETF
    "DXJ": "equity_intl",  # WisdomTree Japan Hedged Equity Fund
    "EWT": "equity_em",    # iShares MSCI Taiwan ETF (Taiwan sits in MSCI EM)
    "FAD": "equity_us",    # First Trust Multi Cap Growth AlphaDEX Fund
    "FLTR": "bond",        # VanEck IG Floating Rate ETF
    "GCSH": "bond",        # Guggenheim Ultra Short Income ETF -- near-cash,
                            # gets its own tighter risk range below
    "GRID": "equity_us",   # First Trust Clean Edge Smart Grid Infrastructure Fund
    "HAWX": "equity_intl", # iShares Currency Hedged MSCI ACWI ex U.S. ETF
    "HTUS": "equity_us",   # Hull Tactical US ETF -- tactical model can go long,
                            # short, or leveraged on the S&P 500; see below
    "HYGH": "bond",        # iShares Interest Rate Hedged High Yield Bond ETF
    "HYHG": "bond",        # ProShares High Yield-Interest Rate Hedged ETF
}

LEVERAGED_OVERRIDES = {
    "HTUS",  # can run leveraged/inverse S&P 500 exposure via its own model --
             # won't get caught by LEVERAGED_NAME_KEYWORDS since "leveraged"
             # isn't in the fund's actual name
}

# Ultra-short/cash-alternative bond funds barely move -- the standard bond
# range (2-15% vol / 0-15% DD) is too wide to tell them apart from each other.
ULTRA_SHORT_BOND_OVERRIDES = {"GCSH"}
ULTRA_SHORT_RISK_RANGE = {"vol": (0.3, 4), "dd": (0, 3)}

BENCHMARKS_BY_CATEGORY = {
    "equity_us":   {"SPY": "SPY", "DJI": "^DJI", "SPX": "^GSPC", "IXIC": "^IXIC"},
    "equity_intl": {"EFA": "EFA", "VXUS": "VXUS"},
    "equity_em":   {"EEM": "EEM"},
    "bond":        {"AGG": "AGG", "BND": "BND"},
    "commodity":   {"DBC": "DBC", "GLD": "GLD"},
    "real_estate": {"VNQ": "VNQ"},
}
DEFAULT_CATEGORY = "equity_us"

TIMEFRAME = "hourly"
REQUEST_DELAY_SEC = 1.5
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 5

TIMEFRAME_CONFIG = {
    "hourly": {
        "interval": "1h",
        "period": "730d",
        "ma_short": 50,
        "ma_long": 200,
        "rsi_period": 14,
        "vol_recent_bars": 7,
        "vol_baseline_bars": 130,
        "mom_windows": {"1D%": 7, "1W%": 33, "1M%": 140},
        "bench_bars": 33,
        # FIX (#4 orig): rough expected bar count for a 730-day hourly pull, so
        # we can flag tickers where Yahoo silently truncated the history
        # instead of assuming the full window was actually returned.
        "min_expected_bars": 1800,
        # FIX (#1 new): how many bars make up one trading day at this
        # interval. A regular US equity session is ~6.5 hours, so an hourly
        # bar series has ~7 bars/day (6 full hours + 1 partial). Used to turn
        # the liquidity check into a genuine daily-dollar-volume figure
        # instead of an hourly one.
        "bars_per_day": 7,
    },
}

SCORE_WEIGHTS = {
    "trend": 20,
    "momentum": 10,
    "price_momentum": 15,
    "volume": 20,
    "relative_strength": 20,
    "risk_adjustment": 15,
}
assert sum(SCORE_WEIGHTS.values()) == 100

# Risk clip ranges tuned down from the single-stock version -- diversified
# ETFs sit at much lower vol/drawdown than individual names, so the original
# 15-80% vol / 0-50% DD range barely discriminated between funds. Split by
# category since bonds and equities have very different baseline vol.
RISK_RANGE_BY_CATEGORY = {
    "equity_us":   {"vol": (8, 35),  "dd": (0, 30)},
    "equity_intl": {"vol": (8, 35),  "dd": (0, 30)},
    "equity_em":   {"vol": (10, 45), "dd": (0, 40)},
    "bond":        {"vol": (2, 15),  "dd": (0, 15)},
    "commodity":   {"vol": (10, 45), "dd": (0, 40)},
    "real_estate": {"vol": (8, 35),  "dd": (0, 30)},
}
LEVERAGED_RISK_RANGE = {"vol": (15, 80), "dd": (0, 60)}

# FIX (#4 orig / leverage keywords): the old list ("daily", "bull", "bear")
# was prone to false positives on ordinary fund names/descriptions (e.g. a
# "Daily Rebalanced" methodology blurb, or a marketing name containing "Bull
# Market"). Split into unambiguous tokens (checked as plain substrings, since
# "2x"/"3x"/"-1x" aren't real words) and phrase-like tokens that are now
# matched as whole words via regex so "bull" doesn't fire on "bullish
# outlook" copy and "daily" doesn't fire on ordinary boilerplate.
LEVERAGED_SUBSTRING_KEYWORDS = ["2x", "3x", "-1x", "ultrapro"]
LEVERAGED_WORD_KEYWORDS = ["ultra", "leveraged", "inverse", "bull", "bear"]

# Liquidity guardrail: flag (and optionally exclude) ETFs too thin to trust
# the technical read. This threshold is a DAILY dollar-volume figure.
MIN_AVG_DOLLAR_VOLUME = 1_000_000
EXCLUDE_LOW_LIQUIDITY = False


# ----------------------------------------------------------------------------
# 2. INDICATOR HELPERS
# ----------------------------------------------------------------------------
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def pct_change_over(close, bars):
    if len(close) > bars:
        return float((close.iloc[-1] / close.iloc[-bars] - 1) * 100)
    return np.nan


def max_drawdown(close):
    running_max = close.cummax()
    drawdown = (close / running_max - 1) * 100
    return float(drawdown.min())


def clip_scale(value, lo, hi, out_max):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.0
    v = min(max(value, lo), hi)
    return (v - lo) / (hi - lo) * out_max


def inverse_clip_scale(value, lo, hi, out_max):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.0
    v = min(max(value, lo), hi)
    return (hi - v) / (hi - lo) * out_max


def avg_daily_dollar_volume(close, volume, bars_per_day, lookback_days=30):
    """FIX (#1 new): true daily-equivalent average dollar volume.

    The old version did `(close * volume).tail(30).mean()`, which -- on an
    hourly bar series -- averages the last 30 *hourly* bars (~4.6 trading
    days), not 30 days. That number was then compared against
    MIN_AVG_DOLLAR_VOLUME, a threshold sized for a daily figure, so it
    understated liquidity by roughly bars_per_day-fold and over-flagged
    perfectly liquid ETFs as LowLiquidity.

    This groups bars into day-sized chunks, sums dollar volume within each
    chunk (so each element is a real day's total dollar volume), then
    averages across the trailing `lookback_days` such days.
    """
    if bars_per_day <= 0:
        return np.nan
    dollar_vol_per_bar = close * volume
    n_bars = len(dollar_vol_per_bar)
    max_bars = bars_per_day * lookback_days
    n_bars_used = min(n_bars, max_bars)
    n_full_days = n_bars_used // bars_per_day
    if n_full_days < 1:
        return np.nan
    trimmed = dollar_vol_per_bar.tail(n_full_days * bars_per_day)
    daily_sums = trimmed.groupby(np.arange(len(trimmed)) // bars_per_day).sum()
    return float(daily_sums.mean())


# ----------------------------------------------------------------------------
# 3. ROBUST DOWNLOAD
# ----------------------------------------------------------------------------
def safe_download(ticker, interval, period):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            hist = yf.Ticker(ticker).history(
                period=period,
                interval=interval,
                auto_adjust=True,
            )
            if not hist.empty and "Close" in hist.columns:
                return hist
            last_err = "empty response or missing Close column"
        except Exception as error:
            last_err = error
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC * attempt)
    return pd.DataFrame()


def safe_get_info(ticker):
    """FIX (#2 new): the old classify_etf() called yf.Ticker(ticker).info
    directly -- no retry, no backoff, no delay -- for every ticker not in
    CATEGORY_OVERRIDES. On a large watchlist that's a second, unthrottled
    hammer on Yahoo's API sitting right next to safe_download(), which
    already has retry/backoff/delay. This mirrors that same pattern for
    the .info lookup so classification is no more likely to trip a rate
    limit than the price/volume download is.
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            info = yf.Ticker(ticker).info
            if info:
                return info
            last_err = "empty info response"
        except Exception as error:
            last_err = error
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC * attempt)
    return {}


# ----------------------------------------------------------------------------
# 4. ETF CATEGORY / LEVERAGE DETECTION
# ----------------------------------------------------------------------------
_category_cache = {}


def _is_leveraged(text):
    if any(w in text for w in LEVERAGED_SUBSTRING_KEYWORDS):
        return True
    # FIX (#4 orig): word-boundary match so "bull"/"bear"/"ultra"/etc. don't
    # fire on substrings inside ordinary words or marketing copy (e.g. a fund
    # description that happens to contain "bullish"). \b requires a real word
    # boundary on both sides.
    import re
    return any(re.search(rf"\b{w}\b", text) for w in LEVERAGED_WORD_KEYWORDS)


def classify_etf(ticker):
    """Category + leveraged-fund detection. Tickers in CATEGORY_OVERRIDES skip
    the live yf.Ticker(ticker).info lookup entirely -- add to CATEGORY_OVERRIDES
    for anything new so this stays fast and doesn't depend on Yahoo's metadata.

    Returns (category, is_leveraged, used_live_lookup) -- the third value lets
    the caller decide whether to apply the same request-throttling delay used
    for price downloads (only needed when a live .info call was actually made).
    """
    if ticker in _category_cache:
        cat, lev = _category_cache[ticker]
        return cat, lev, False

    if ticker in CATEGORY_OVERRIDES:
        result = (CATEGORY_OVERRIDES[ticker], ticker in LEVERAGED_OVERRIDES)
        _category_cache[ticker] = result
        return result[0], result[1], False

    info = safe_get_info(ticker)
    text = " ".join(
        str(info.get(k, "")) for k in ("category", "longName", "shortName")
    ).lower()

    if any(w in text for w in ["bond", "fixed income", "credit"]):
        category = "bond"
    elif any(w in text for w in ["commodit", "gold", "precious metal", "oil"]):
        category = "commodity"
    elif any(w in text for w in ["real estate", "reit"]):
        category = "real_estate"
    elif "emerging" in text:
        category = "equity_em"
    elif any(w in text for w in ["foreign", "international", "world", "global",
                                  "japan", "china", "europe", "asia", "hedged"]):
        category = "equity_intl"
    else:
        category = DEFAULT_CATEGORY

    is_leveraged = _is_leveraged(text)

    result = (category, is_leveraged)
    _category_cache[ticker] = result
    return result[0], result[1], True


# ----------------------------------------------------------------------------
# 5. PREP: CLASSIFY ALL TICKERS UP FRONT
# ----------------------------------------------------------------------------
def prepare_etf_categories():
    """Classifies every ticker and writes a snapshot of the input ratings to
    etf_weighted_ratings.csv as a side effect (kept from the original for
    traceability -- flagged here in the docstring since the function name
    alone doesn't hint that it does I/O)."""
    categories_needed = set()
    bar = tqdm(TICKERS, desc="Step 1/3: Classifying ETFs", unit="ticker")
    for t in bar:
        cat, _lev, used_live_lookup = classify_etf(t)
        categories_needed.add(cat)
        # FIX (#2 new): only sleep when we actually made a live network call;
        # override-based / cached lookups are free and shouldn't be throttled.
        if used_live_lookup:
            time.sleep(REQUEST_DELAY_SEC)
    categories_needed.add(DEFAULT_CATEGORY)

    return categories_needed


# ----------------------------------------------------------------------------
# 6. PER-TICKER ANALYSIS
# ----------------------------------------------------------------------------
def analyze_ticker(ticker, cfg, bench_rets_by_category, rating_scores):
    hist = safe_download(ticker, cfg["interval"], cfg["period"])
    if hist.empty or len(hist) < max(cfg["ma_short"], 30):
        return None

    # FIX (#4 orig): flag (don't drop) tickers where Yahoo returned meaningfully
    # fewer bars than a full 730-day hourly pull should have -- MA200 and the
    # 130-bar volume baseline get quietly thin otherwise, with no signal that
    # it happened.
    short_history = len(hist) < cfg.get("min_expected_bars", 0)

    category, is_leveraged, _used_live_lookup = classify_etf(ticker)
    bench_rets = bench_rets_by_category.get(category, bench_rets_by_category[DEFAULT_CATEGORY])

    if ticker in ULTRA_SHORT_BOND_OVERRIDES:
        risk_range = ULTRA_SHORT_RISK_RANGE
    elif is_leveraged:
        risk_range = LEVERAGED_RISK_RANGE
    else:
        risk_range = RISK_RANGE_BY_CATEGORY.get(category, RISK_RANGE_BY_CATEGORY[DEFAULT_CATEGORY])

    close = hist["Close"]
    volume = hist["Volume"]
    price = float(close.iloc[-1])

    # FIX (#1 new): use the bars-per-day-aware daily dollar volume helper
    # instead of averaging raw hourly-bar dollar volume against a
    # daily-sized threshold.
    bars_per_day = cfg.get("bars_per_day", 1)
    dollar_vol = avg_daily_dollar_volume(close, volume, bars_per_day, lookback_days=30)
    low_liquidity = (not np.isnan(dollar_vol)) and dollar_vol < MIN_AVG_DOLLAR_VOLUME
    if low_liquidity and EXCLUDE_LOW_LIQUIDITY:
        return None

    ma_short = close.rolling(cfg["ma_short"]).mean().iloc[-1]
    ma_long = (close.rolling(cfg["ma_long"]).mean().iloc[-1]
               if len(close) >= cfg["ma_long"] else np.nan)

    r = float(rsi(close, cfg["rsi_period"]).iloc[-1])
    macd_line, signal_line = macd(close)
    macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])

    vol_recent_bars = cfg["vol_recent_bars"]
    vol_baseline_bars = cfg["vol_baseline_bars"]
    if len(volume) >= vol_baseline_bars:
        vol_recent = volume.iloc[-vol_recent_bars:].mean()
        vol_baseline = volume.iloc[-vol_baseline_bars:-vol_recent_bars].mean()
        vol_ratio = float(vol_recent / vol_baseline) if vol_baseline else np.nan
    else:
        vol_ratio = np.nan

    mom_returns = {label: pct_change_over(close, bars)
                   for label, bars in cfg["mom_windows"].items()}

    # Renamed from `daily_ret` -- these are per-bar (hourly) returns, not
    # daily returns; the annualization factor below already accounts for
    # that (252 trading days * 6.5 bars/day).
    bar_ret = close.pct_change(fill_method=None).dropna()
    ann_vol = float(bar_ret.std() * np.sqrt(252 * 6.5) * 100)
    dd = max_drawdown(close)

    primary_ret = pct_change_over(close, cfg["bench_bars"])

    rel_strength_by_bench = {}
    if not np.isnan(primary_ret):
        for name, b_ret in bench_rets.items():
            rel_strength_by_bench[name] = (
                primary_ret - b_ret if not np.isnan(b_ret) else np.nan
            )
    else:
        rel_strength_by_bench = {name: np.nan for name in bench_rets}

    valid_rel = [v for v in rel_strength_by_bench.values() if not np.isnan(v)]
    rel_strength = float(np.mean(valid_rel)) if valid_rel else np.nan

    vs_short_pct = (price / ma_short - 1) * 100 if ma_short else np.nan
    vs_long_pct = (price / ma_long - 1) * 100 if not np.isnan(ma_long) else np.nan

    score_short = clip_scale(vs_short_pct, -10, 10, SCORE_WEIGHTS["trend"] / 2)
    score_long = (clip_scale(vs_long_pct, -10, 10, SCORE_WEIGHTS["trend"] / 2)
                  if not np.isnan(vs_long_pct) else 0.0)
    trend_score = score_short + score_long

    # FIX (#1 orig): r can be NaN (e.g. a flat/illiquid run producing 0/0 in
    # the RSI calc). Route it through the same NaN-safe path as everything
    # else instead of letting `max(nan, 0)` silently resolve to 0 -- which
    # read as "strong sell" for what was actually "couldn't compute
    # momentum."
    if np.isnan(r):
        score_rsi = 0.0
    else:
        score_rsi = SCORE_WEIGHTS["momentum"] / 2 * max(0, 1 - abs(r - 55) / 45)
    score_macd = SCORE_WEIGHTS["momentum"] / 2 if macd_bullish else 0
    momentum_score = score_rsi + score_macd

    volume_score = clip_scale(vol_ratio, 0.5, 2.5, SCORE_WEIGHTS["volume"])

    valid_mom = [v for v in mom_returns.values() if not np.isnan(v)]
    avg_mom = float(np.mean(valid_mom)) if valid_mom else np.nan
    price_momentum_score = clip_scale(avg_mom, -40, 40, SCORE_WEIGHTS["price_momentum"])

    rel_strength_score = clip_scale(rel_strength, -20, 20, SCORE_WEIGHTS["relative_strength"])
    risk_vol_score = inverse_clip_scale(ann_vol, *risk_range["vol"], SCORE_WEIGHTS["risk_adjustment"] / 2)
    risk_dd_score = inverse_clip_scale(abs(dd), *risk_range["dd"], SCORE_WEIGHTS["risk_adjustment"] / 2)
    risk_score = risk_vol_score + risk_dd_score

    total_score = round(trend_score + momentum_score + volume_score
                        + price_momentum_score + rel_strength_score + risk_score)
    technical_score = int(min(max(total_score, 0), 100))

    rating_data = rating_scores.get(ticker, {})
    weighted_rating = rating_data.get("weighted_rating", np.nan)
    fund_rating_score = rating_to_100(weighted_rating)

    # FIX (#3 new): unrated tickers get pure technical_score while rated ones
    # get an 80/20 blend -- both land in 0-100, so nothing is "broken," but a
    # rated fund with a mediocre rating can be pulled below an unrated fund
    # with the same technical picture, and previously the only trace of that
    # was the NoRating flag buried in a text column. Made explicit as its own
    # column so it's visible/filterable without parsing Flags.
    if pd.isna(fund_rating_score):
        final_score = round(float(technical_score), 2)
        score_basis = "technical_only"
    else:
        final_score = round(fund_rating_score * RATING_WEIGHT + technical_score * TECHNICAL_WEIGHT, 2)
        score_basis = "blended"

    if final_score >= 75:
        rating = "Strong hold"
    elif final_score >= 60:
        rating = "Hold"
    elif final_score >= 50:
        rating = "Neutral"
    elif final_score >= 40:
        rating = "Sell"
    else:
        rating = "Strong sell"

    flags = []
    if not np.isnan(vs_long_pct) and price > ma_short > ma_long:
        flags.append("Uptrend")
    elif not np.isnan(vs_long_pct) and price < ma_short < ma_long:
        flags.append("Downtrend")
    if r >= 70:
        flags.append("Overbought(RSI)")
    elif r <= 30:
        flags.append("Oversold(RSI)")
    if not np.isnan(vol_ratio) and vol_ratio >= 1.5:
        flags.append("VolumeSurge")
    if macd_bullish:
        flags.append("MACD+")
    if not np.isnan(rel_strength) and rel_strength > 0:
        flags.append("BeatingAvgBench")
    beaten_count = sum(1 for v in rel_strength_by_bench.values()
                       if not np.isnan(v) and v > 0)
    if valid_rel and beaten_count == len(valid_rel):
        flags.append("BeatingAllBench")
    if low_liquidity:
        flags.append("LowLiquidity")
    if is_leveraged:
        flags.append("Leveraged")
    if pd.isna(fund_rating_score):
        flags.append("NoRating")
    if short_history:
        flags.append("ShortHistory")
    if np.isnan(r):
        flags.append("RSI_NaN")

    row = {
        "Ticker": ticker,
        "final_score": final_score,
        "score_basis": score_basis,
        "rating": rating,
        "category": category,
        "price": round(price, 2),
        "weighted_rating": weighted_rating,
        "fund_rating_score": round(fund_rating_score, 1) if not pd.isna(fund_rating_score) else np.nan,
        "rating_cycles": rating_data.get("rating_cycles", ""),
        "rating_date": rating_data.get("rating_date", ""),
        "technical_score": technical_score,
        f"vs{cfg['ma_short']}MA%": round(vs_short_pct, 1) if not np.isnan(vs_short_pct) else np.nan,
        f"vs{cfg['ma_long']}MA%": round(vs_long_pct, 1) if not np.isnan(vs_long_pct) else np.nan,
        "RSI": round(r, 1) if not np.isnan(r) else np.nan,
        "VolSurge": round(vol_ratio, 2) if not np.isnan(vol_ratio) else np.nan,
    }
    for label, val in mom_returns.items():
        row[label] = round(val, 1) if not np.isnan(val) else np.nan
    row["AnnVol%"] = round(ann_vol, 1)
    row["MaxDD%"] = round(dd, 1)
    row["AvgDollarVol"] = round(dollar_vol, 0) if not np.isnan(dollar_vol) else np.nan
    row["BarsFetched"] = len(hist)
    row["RelStrength_AvgBench"] = round(rel_strength, 1) if not np.isnan(rel_strength) else np.nan
    for name, val in rel_strength_by_bench.items():
        row[f"RelStrength_vs_{name}"] = round(val, 1) if not np.isnan(val) else np.nan
    row["Flags"] = ", ".join(flags) if flags else "-"
    return row


# ----------------------------------------------------------------------------
# 7. RUN THE SCREEN
# ----------------------------------------------------------------------------
def run_etf_screen():
    cfg = TIMEFRAME_CONFIG[TIMEFRAME]

    categories_needed = prepare_etf_categories()

    bench_rets_by_category = {}
    bench_jobs = [
        (cat, name, symbol)
        for cat in categories_needed
        for name, symbol in BENCHMARKS_BY_CATEGORY.get(cat, BENCHMARKS_BY_CATEGORY[DEFAULT_CATEGORY]).items()
    ]
    bar = tqdm(bench_jobs, desc="Step 2/3: Downloading benchmarks", unit="benchmark")
    for cat, name, symbol in bar:
        bench_rets_by_category.setdefault(cat, {})
        bench_hist = safe_download(symbol, cfg["interval"], cfg["period"])
        if bench_hist.empty or "Close" not in bench_hist.columns:
            bench_rets_by_category[cat][name] = np.nan
        else:
            bench_rets_by_category[cat][name] = pct_change_over(bench_hist["Close"], cfg["bench_bars"])
        time.sleep(REQUEST_DELAY_SEC)

    if all(np.isnan(v) for rets in bench_rets_by_category.values() for v in rets.values()):
        raise RuntimeError(
            "Unable to download any benchmark data. Yahoo Finance may be "
            "temporarily rate-limiting requests."
        )

    rows = []
    bar = tqdm(TICKERS, desc="Step 3/3: Screening tickers", unit="ticker")
    for ticker in bar:
        bar.set_description(f"Step 3/3: Screening {ticker}")
        row = analyze_ticker(ticker, cfg, bench_rets_by_category, RATING_SCORES)
        if row:
            rows.append(row)
        time.sleep(REQUEST_DELAY_SEC)
    bar.set_description("Step 3/3: Done")

    if not rows:
        raise RuntimeError("No ticker data was downloaded; no output was generated.")

    df = pd.DataFrame(rows).sort_values("final_score", ascending=False).reset_index(drop=True)

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)

    df.to_csv("etf_scanner.csv", index=False)
    return df

# ----------------------------------------------------------------------------
# 8. Execution
# ----------------------------------------------------------------------------
print(df)
df = run_etf_screen()
