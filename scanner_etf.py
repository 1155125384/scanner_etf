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
from pathlib import Path

# =============================================================================
# Configuration
# =============================================================================
# Change values in this section only. The rest of the file implements the
# pipeline: collect holdings, download ratings, calculate scores, and export
# the final ETF screen.
CONFIG = {
    "webull": {
        "sandbox_app_key": os.getenv('SANDBOX_KEY'),
        "sandbox_app_secret": os.getenv('SANDBOX_SECRET'),
        "sandbox_host": "api.sandbox.webull.hk",
        "account_app_key": os.getenv('APP_KEY'),
        "account_app_secret": os.getenv('APP_SECRET'),
        "token_host": "api.sandbox.webull.hk",
        "token_api_version": "v3",
        "region": Region.HK.value,
        "token_environment_variable": "WEBULL_ACCESS_TOKEN",
        "token_file": "conf/token.txt",
        "signature_algorithm": "HMAC-SHA1",
        "signature_version": "1.0",
        "api_version": "v2",
        "token_create_endpoint": "/auth/tokens/create",
        "refresh_token_on_start": True,
        "ratings_endpoint": "/market-data/fundamentals/fund-ratings/get",
        "ratings_category": "US_STOCK",
        "account_page_size": 100,
    },
    "ftp": {
        "host": "ftp.nasdaqtrader.com",
        "directory": "symboldirectory",
        "listed_file": "nasdaqlisted.txt",
        "other_listed_file": "otherlisted.txt",
        "delimiter": "|",
    },
    "ratings": {
        "request_delay_seconds": 0.1,
        "retry_wait_seconds": 10,
        "max_retries": 5,
        "retry_backoff_base_seconds": 2,
        "retry_backoff_max_seconds": 60,
        "lookback_years": 1,
        "cycle_weights": {3: 1.0, 5: 1.5, 10: 2.0},
        "rating_scale_max": 5,
        "rating_decimal_places": 1,
        "minimum_weighted_rating": 4.0,
        "not_found_status_codes": {404, 417},
        "rate_limit_status_code": 429,
        "not_found_markers": (
            "not found",
            "no rating",
            "rating not available",
            "no data",
        ),
    },
    "progress": {
        "update_interval_seconds": 1.0,
        "poll_interval_seconds": 0.1,
        "bar_length": 30,
        "line_width": 200,
    },
    "screen": {
        "timeframe": "hourly",
        "minimum_history_bars": 30,
        "holdings_metadata_delay_seconds": 0.3,
        "auto_adjust_prices": True,
        "request_delay_seconds": 1.5,
        "max_retries": 3,
        "retry_backoff_seconds": 5,
        "rating_weight": 0.2,
        "technical_weight": 0.8,
        "annualized_trading_days": 252,
        "annualized_bars_per_day": 6.5,
        "liquidity_lookback_days": 30,
        "volume_surge_threshold": 1.5,
        "minimum_average_dollar_volume": 1_000_000,
        "exclude_low_liquidity": False,
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "rsi": {"ideal": 55, "overbought": 70, "oversold": 30, "distance": 45},
        "score_ranges": {
            "trend": (-10, 10),
            "volume": (0.5, 2.5),
            "price_momentum": (-40, 40),
            "relative_strength": (-20, 20),
        },
        "rating_thresholds": {
            "strong_hold": 75,
            "hold": 60,
            "neutral": 50,
            "sell": 40,
        },
        "timeframes": {
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
                "min_expected_bars": 1800,
                "bars_per_day": 7,
            },
        },
        "score_weights": {
            "trend": 20,
            "momentum": 10,
            "price_momentum": 15,
            "volume": 20,
            "relative_strength": 20,
            "risk_adjustment": 15,
        },
        "category_overrides": {
            "BBLU": "equity_us",
            "BCPL": "bond",
            "CEFZ": "equity_us",
            "CLSE": "equity_us",
            "DBAW": "equity_intl",
            "DXJ": "equity_intl",
            "EWT": "equity_em",
            "FAD": "equity_us",
            "FLTR": "bond",
            "GCSH": "bond",
            "GRID": "equity_us",
            "HAWX": "equity_intl",
            "HTUS": "equity_us",
            "HYGH": "bond",
            "HYHG": "bond",
        },
        "leveraged_overrides": {"HTUS"},
        "ultra_short_bond_overrides": {"GCSH"},
        "ultra_short_risk_range": {"vol": (0.3, 4), "dd": (0, 3)},
        "benchmarks_by_category": {
            "equity_us": {"SPY": "SPY", "DJI": "^DJI", "SPX": "^GSPC", "IXIC": "^IXIC"},
            "equity_intl": {"EFA": "EFA", "VXUS": "VXUS"},
            "equity_em": {"EEM": "EEM"},
            "bond": {"AGG": "AGG", "BND": "BND"},
            "commodity": {"DBC": "DBC", "GLD": "GLD"},
            "real_estate": {"VNQ": "VNQ"},
        },
        "default_category": "equity_us",
        "risk_ranges_by_category": {
            "equity_us": {"vol": (8, 35), "dd": (0, 30)},
            "equity_intl": {"vol": (8, 35), "dd": (0, 30)},
            "equity_em": {"vol": (10, 45), "dd": (0, 40)},
            "bond": {"vol": (2, 15), "dd": (0, 15)},
            "commodity": {"vol": (10, 45), "dd": (0, 40)},
            "real_estate": {"vol": (8, 35), "dd": (0, 30)},
        },
        "leveraged_risk_range": {"vol": (15, 80), "dd": (0, 60)},
        "leveraged_substring_keywords": ["2x", "3x", "-1x", "ultrapro"],
        "leveraged_word_keywords": ["ultra", "leveraged", "inverse", "bull", "bear"],
    },
    "output": {
        "scanner_csv": "etf_scanner.csv",
        "csv_include_index": False,
        "display_width": 220,
        "display_max_columns": None,
    },
}

WEBULL_CONFIG = CONFIG["webull"]
FTP_CONFIG = CONFIG["ftp"]
RATING_CONFIG = CONFIG["ratings"]
PROGRESS_CONFIG = CONFIG["progress"]
SCREEN_CONFIG = CONFIG["screen"]
OUTPUT_CONFIG = CONFIG["output"]

SANDBOX_APP_KEY = WEBULL_CONFIG["sandbox_app_key"]
SANDBOX_APP_SECRET = WEBULL_CONFIG["sandbox_app_secret"]
print(SANDBOX_APP_KEY)
print(SANDBOX_APP_SECRET)
SANDBOX_HOST = WEBULL_CONFIG["sandbox_host"]
ACCOUNT_APP_KEY = WEBULL_CONFIG["account_app_key"]
ACCOUNT_APP_SECRET = WEBULL_CONFIG["account_app_secret"]
SANDBOX_BASE_URL = f"https://{SANDBOX_HOST}"
TOKEN_BASE_URL = f"https://{WEBULL_CONFIG['token_host']}"


def load_access_token():
    token = os.getenv(WEBULL_CONFIG["token_environment_variable"], "").strip()
    if token:
        return token

    token_file = Path(__file__).resolve().parent / WEBULL_CONFIG["token_file"]
    try:
        with token_file.open(encoding="utf-8") as file:
            return next((line.strip() for line in file if line.strip()), "")
    except OSError:
        return ""


ACCESS_TOKEN = load_access_token()


def generate_signature(path, query_params, body_string, app_key, app_secret, host, timestamp, nonce):
    signing_headers = {
        "x-app-key": app_key,
        "x-timestamp": timestamp,
        "x-signature-algorithm": WEBULL_CONFIG["signature_algorithm"],
        "x-signature-version": WEBULL_CONFIG["signature_version"],
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


def call_api(
    method,
    path,
    query_params=None,
    body=None,
    access_token=None,
    app_key=SANDBOX_APP_KEY,
    app_secret=SANDBOX_APP_SECRET,
    host=SANDBOX_HOST,
    base_url=SANDBOX_BASE_URL,
    api_version=None,
    include_app_secret_header=False,
):
    query_params = query_params or {}
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = uuid.uuid4().hex

    body_string = json.dumps(body, separators=(",", ":")) if body else None

    signature = generate_signature(
        path, query_params, body_string,
        app_key, app_secret, host, timestamp, nonce,
    )

    headers = {
        "Accept": "application/json",
        "x-app-key": app_key,
        "x-timestamp": timestamp,
        "x-signature": signature,
        "x-signature-algorithm": WEBULL_CONFIG["signature_algorithm"],
        "x-signature-version": WEBULL_CONFIG["signature_version"],
        "x-signature-nonce": nonce,
        "x-version": api_version or WEBULL_CONFIG["api_version"],
    }
    if include_app_secret_header:
        headers["x-app-secret"] = app_secret
    if access_token is not None:
        if not access_token:
            raise ValueError(
                f"{WEBULL_CONFIG['token_environment_variable']} is not set. "
                "Set it in the environment or configure the token file."
            )
        headers["x-access-token"] = access_token

    url = f"{base_url}{path}"

    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, params=query_params)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, data=body_string)

    return resp


def create_access_token():
    """Create a fresh short-lived token for this run.

    The token-create request is signed with the sandbox app credentials and
    does not send an existing access token. The returned token is then used
    for the subsequent market-data requests.
    """
    response = call_api(
        "POST",
        WEBULL_CONFIG["token_create_endpoint"],
        body={},
        access_token=None,
        host=SANDBOX_HOST,
        base_url=TOKEN_BASE_URL,
        api_version=WEBULL_CONFIG["token_api_version"],
        include_app_secret_header=True,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if not response.ok or not isinstance(payload, dict):
        raise RuntimeError(
            f"Unable to create Webull access token: HTTP {response.status_code} "
            f"{response.text[:500]}"
        )

    token = (
        payload.get("access_token")
        or payload.get("accessToken")
        or payload.get("token")
    )
    if not token:
        raise RuntimeError(
            f"Webull token response did not contain an access token: {payload}"
        )
    return token


if WEBULL_CONFIG["refresh_token_on_start"]:
    if not SANDBOX_APP_KEY or not SANDBOX_APP_SECRET:
        raise RuntimeError(
            "SANDBOX_KEY and SANDBOX_SECRET must be configured before "
            "creating a Webull sandbox access token."
        )
    ACCESS_TOKEN = create_access_token()
elif not ACCESS_TOKEN:
    raise RuntimeError(
        f"No Webull access token found. Set the GitHub Actions secret "
        f"WEBULL_ACCESS_TOKEN and map it to the environment, or add the "
        f"token as the first non-empty line of {WEBULL_CONFIG['token_file']}."
    )

def fetch_ftp_file(filename):
    ftp = ftplib.FTP(FTP_CONFIG["host"])
    ftp.login()
    ftp.cwd(FTP_CONFIG["directory"])
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {filename}", buf.write)
    ftp.quit()
    return buf.getvalue().decode("utf-8")

# Download both Nasdaq symbol-directory files. Each file ends with a metadata
# footer, so the final row is removed before the tables are combined.
nasdaq_text = fetch_ftp_file(FTP_CONFIG["listed_file"])
other_text = fetch_ftp_file(FTP_CONFIG["other_listed_file"])
nasdaq = pd.read_csv(io.StringIO(nasdaq_text), sep=FTP_CONFIG["delimiter"])[:-1]
other = pd.read_csv(io.StringIO(other_text), sep=FTP_CONFIG["delimiter"])[:-1]

# Keep only ETFs and normalize the different symbol-column names.
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

# The account client uses production credentials only to read current holdings.
api_client = ApiClient(ACCOUNT_APP_KEY, ACCOUNT_APP_SECRET, WEBULL_CONFIG["region"])
api = API(api_client)

res_acct = api.account.get_app_subscriptions()
account_id = None

result = res_acct.json()
account_id = result[0]['account_id']

res_stock = api.account.get_account_position(
    account_id, page_size=WEBULL_CONFIG["account_page_size"]
)
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
    time.sleep(SCREEN_CONFIG["holdings_metadata_delay_seconds"])

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
request_delay_seconds = RATING_CONFIG["request_delay_seconds"]
retry_wait_seconds = RATING_CONFIG["retry_wait_seconds"]
max_retries = RATING_CONFIG["max_retries"]
total_symbols = len(etf_symbols)
started_at = time.time()
progress_update_interval_seconds = PROGRESS_CONFIG["update_interval_seconds"]
last_progress_update_at = 0.0
last_progress_key = None

def show_progress(completed, current_symbol, state="requesting"):
    global last_progress_update_at, last_progress_key

    now = time.time()
    progress_key = (completed, current_symbol, state, len(fund_rating_rows), len(fund_rating_errors))
    is_state_change = progress_key != last_progress_key
    if (
        now - last_progress_update_at < progress_update_interval_seconds
        and not is_state_change
    ):
        return

    elapsed = time.time() - started_at
    rate = completed / elapsed if elapsed > 0 and completed else 0
    remaining = (total_symbols - completed) / rate if rate > 0 else 0
    bar_length = PROGRESS_CONFIG["bar_length"]
    filled = int(bar_length * completed / total_symbols) if total_symbols else 0
    progress_bar = "#" * filled + "-" * (bar_length - filled)
    eta = f"ETA {remaining / 60:.1f} min" if rate else "ETA calculating"
    percentage = completed / total_symbols if total_symbols else 0
    message = (
        f"\r[{progress_bar}] {completed:>4}/{total_symbols} "
        f"({percentage:.1%}) | "
        f"{current_symbol:<6} | {state:<20} | Success {len(fund_rating_rows):>4} | "
        f"Failed {len(fund_rating_errors):>3}"
    )
    line_width = PROGRESS_CONFIG["line_width"]
    sys.stdout.write(message[:line_width].ljust(line_width))
    sys.stdout.flush()
    last_progress_update_at = now
    last_progress_key = progress_key


def wait_with_progress(seconds, completed, symbol, reason):
    end_time = time.time() + seconds
    while True:
        seconds_left = max(0, int(end_time - time.time() + 0.999))
        show_progress(completed, symbol, f"{reason}, wait {seconds_left}s")
        if seconds_left == 0:
            break
        time.sleep(min(PROGRESS_CONFIG["poll_interval_seconds"], seconds_left))


print(f"Starting ETF fund ratings download for {total_symbols} symbols at {datetime.now():%H:%M:%S}")
show_progress(0, "-", "starting")

def get_rating_error_message(response):
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()

    if isinstance(payload, dict):
        return str(
            payload.get("message")
            or payload.get("error")
            or payload.get("msg")
            or response.text
        ).strip()
    return response.text.strip()


def is_rating_not_found(response, error_message):
    if response.status_code in RATING_CONFIG["not_found_status_codes"]:
        return True

    normalized_message = error_message.lower()
    return any(
        marker in normalized_message
        for marker in RATING_CONFIG["not_found_markers"]
    )


for completed, symbol in enumerate(etf_symbols, start=1):
    show_progress(completed - 1, symbol, "requesting")
    for attempt in range(max_retries + 1):
        try:
            rating_response = call_api(
                "GET",
                WEBULL_CONFIG["ratings_endpoint"],
                query_params={
                    "symbol": symbol,
                    "category": WEBULL_CONFIG["ratings_category"],
                },
                access_token=ACCESS_TOKEN,
            )

            error_message = get_rating_error_message(rating_response)
            if (
                rating_response.status_code != RATING_CONFIG["rate_limit_status_code"]
                and is_rating_not_found(
                    rating_response, error_message
                )
            ):
                fund_rating_errors.append(
                    {
                        "symbol": symbol,
                        "error": f"Skipped: rating not found ({error_message})",
                    }
                )
                break

            if rating_response.status_code == RATING_CONFIG["rate_limit_status_code"]:
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
            if not ratings:
                fund_rating_errors.append(
                    {"symbol": symbol, "error": "Skipped: no rating data returned"}
                )
                break

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
            wait_with_progress(
                min(
                    RATING_CONFIG["retry_backoff_max_seconds"],
                    2 ** attempt * RATING_CONFIG["retry_backoff_base_seconds"],
                ),
                completed - 1,
                symbol,
                "retrying",
            )

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

# Convert the raw agency/cycle rows into one weighted rating per ETF. The
# summary uses only recent observations and gives longer rating cycles more
# weight according to the top-level configuration.
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

# Keep ratings from the configured trailing window, measured from the newest
# date returned by the API rather than from the local machine's current date.
cutoff_date = ratings_for_summary["rating_date"].max() - pd.DateOffset(
    years=RATING_CONFIG["lookback_years"]
)
ratings_for_summary = ratings_for_summary[
    ratings_for_summary["rating_date"] >= cutoff_date
]

# Average duplicate agency results within each symbol and rating cycle.
cycle_ratings = (
    ratings_for_summary.dropna(subset=["symbol", "rating_cycle", "rating_results"])
    .groupby(["symbol", "rating_cycle"], as_index=False)["rating_results"]
    .mean()
)

cycle_weights = RATING_CONFIG["cycle_weights"]

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
).round(RATING_CONFIG["rating_decimal_places"])

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

filtered_df = sorted_df[
    sorted_df["weighted_rating"] >= RATING_CONFIG["minimum_weighted_rating"]
]
filtered_df = filtered_df[["symbol", "rating_date", "weighted_rating", "rating_cycles"]]

print(filtered_df)

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ----------------------------------------------------------------------------
# Build the technical-screen input from the weighted fund ratings above.
# Each row contains a symbol, its latest rating date, and its blended rating.
# ----------------------------------------------------------------------------
if "filtered_df" not in globals():
    raise RuntimeError(
        "filtered_df is not defined. Load your ratings DataFrame "
        "(with columns: symbol, rating_date, weighted_rating, rating_cycles) "
        "before running this script."
    )

# Keep the last duplicate symbol explicitly so the rating lookup is predictable.
_dupe_mask = filtered_df["symbol"].duplicated(keep=False)
if _dupe_mask.any():
    _dupes = sorted(filtered_df.loc[_dupe_mask, "symbol"].unique().tolist())
    print(
        f"WARNING: filtered_df has duplicate symbol rows for {_dupes}; "
        f"keeping the last occurrence of each and dropping the rest."
    )
    filtered_df = filtered_df.drop_duplicates(subset="symbol", keep="last").reset_index(drop=True)

# ----------------------------------------------------------------------------
# Runtime inputs derived from the downloaded ratings.
# User-editable values belong in CONFIG at the top of this file.
# ----------------------------------------------------------------------------
TICKERS = filtered_df["symbol"].tolist()
RATING_SCORES = filtered_df.set_index("symbol")[
    ["rating_date", "weighted_rating", "rating_cycles"]
].to_dict("index")

# Ratings use a 0-to-5 scale and are converted to 0-to-100 before blending.
RATING_SCALE_MAX = RATING_CONFIG["rating_scale_max"]

# Reject invalid input instead of silently compressing an out-of-range score.
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


# Blend the fund rating with the technical score using the top-level settings.
RATING_WEIGHT = SCREEN_CONFIG["rating_weight"]
TECHNICAL_WEIGHT = SCREEN_CONFIG["technical_weight"]

_screen = SCREEN_CONFIG
CATEGORY_OVERRIDES = _screen["category_overrides"]
LEVERAGED_OVERRIDES = _screen["leveraged_overrides"]
ULTRA_SHORT_BOND_OVERRIDES = _screen["ultra_short_bond_overrides"]
ULTRA_SHORT_RISK_RANGE = _screen["ultra_short_risk_range"]
BENCHMARKS_BY_CATEGORY = _screen["benchmarks_by_category"]
DEFAULT_CATEGORY = _screen["default_category"]

TIMEFRAME = _screen["timeframe"]
REQUEST_DELAY_SEC = _screen["request_delay_seconds"]
MAX_RETRIES = _screen["max_retries"]
RETRY_BACKOFF_SEC = _screen["retry_backoff_seconds"]
TIMEFRAME_CONFIG = _screen["timeframes"]
SCORE_WEIGHTS = _screen["score_weights"]
assert sum(SCORE_WEIGHTS.values()) == 100

RISK_RANGE_BY_CATEGORY = _screen["risk_ranges_by_category"]
LEVERAGED_RISK_RANGE = _screen["leveraged_risk_range"]

LEVERAGED_SUBSTRING_KEYWORDS = _screen["leveraged_substring_keywords"]
LEVERAGED_WORD_KEYWORDS = _screen["leveraged_word_keywords"]

# Flag ETFs below the configured daily dollar-volume threshold. They are only
# excluded when `exclude_low_liquidity` is enabled in the top configuration.
MIN_AVG_DOLLAR_VOLUME = _screen["minimum_average_dollar_volume"]
EXCLUDE_LOW_LIQUIDITY = _screen["exclude_low_liquidity"]


# ----------------------------------------------------------------------------
# Indicator helpers
# ----------------------------------------------------------------------------
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    series,
    fast=SCREEN_CONFIG["macd"]["fast"],
    slow=SCREEN_CONFIG["macd"]["slow"],
    signal=SCREEN_CONFIG["macd"]["signal"],
):
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


def avg_daily_dollar_volume(
    close,
    volume,
    bars_per_day,
    lookback_days=SCREEN_CONFIG["liquidity_lookback_days"],
):
    """Estimate average daily dollar volume from intraday bars.

    Dollar volume is summed into day-sized groups before averaging, so the
    result can be compared with the daily liquidity threshold in CONFIG.
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
# Yahoo download helpers with retry handling
# ----------------------------------------------------------------------------
def safe_download(ticker, interval, period):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            hist = yf.Ticker(ticker).history(
                period=period,
                interval=interval,
                auto_adjust=SCREEN_CONFIG["auto_adjust_prices"],
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
    """Fetch Yahoo metadata with the same retry policy as price history."""
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
# ETF category and leverage classification
# ----------------------------------------------------------------------------
_category_cache = {}


def _is_leveraged(text):
    if any(w in text for w in LEVERAGED_SUBSTRING_KEYWORDS):
        return True
    # Word boundaries prevent terms such as "bull" from matching "bullish" in
    # unrelated marketing text.
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
# Classify all selected ETFs before downloading price history
# ----------------------------------------------------------------------------
def prepare_etf_categories():
    """Return the categories needed for the benchmark download."""
    categories_needed = set()
    bar = tqdm(TICKERS, desc="Step 1/3: Classifying ETFs", unit="ticker")
    for t in bar:
        cat, _lev, used_live_lookup = classify_etf(t)
        categories_needed.add(cat)
        # Overrides and cached values do not make a Yahoo request, so only
        # throttle after a live metadata lookup.
        if used_live_lookup:
            time.sleep(REQUEST_DELAY_SEC)
    categories_needed.add(DEFAULT_CATEGORY)

    return categories_needed


# ----------------------------------------------------------------------------
# Calculate indicators and scores for one ETF
# ----------------------------------------------------------------------------
def analyze_ticker(ticker, cfg, bench_rets_by_category, rating_scores):
    hist = safe_download(ticker, cfg["interval"], cfg["period"])
    if hist.empty or len(hist) < max(
        cfg["ma_short"], SCREEN_CONFIG["minimum_history_bars"]
    ):
        return None

    # Keep short Yahoo histories in the output, but flag them because long-term
    # indicators may be based on less data than the configured history window.
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

    # Convert intraday volume into a daily-equivalent liquidity measure.
    bars_per_day = cfg.get("bars_per_day", 1)
    dollar_vol = avg_daily_dollar_volume(
        close,
        volume,
        bars_per_day,
        lookback_days=SCREEN_CONFIG["liquidity_lookback_days"],
    )
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

    # Returns are measured per downloaded bar and annualized using the
    # trading-session assumptions configured at the top of the file.
    bar_ret = close.pct_change(fill_method=None).dropna()
    ann_vol = float(
        bar_ret.std()
        * np.sqrt(
            SCREEN_CONFIG["annualized_trading_days"]
            * SCREEN_CONFIG["annualized_bars_per_day"]
        )
        * 100
    )
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

    trend_range = SCREEN_CONFIG["score_ranges"]["trend"]
    score_short = clip_scale(vs_short_pct, *trend_range, SCORE_WEIGHTS["trend"] / 2)
    score_long = (clip_scale(vs_long_pct, *trend_range, SCORE_WEIGHTS["trend"] / 2)
                  if not np.isnan(vs_long_pct) else 0.0)
    trend_score = score_short + score_long

    # A missing RSI is treated as unavailable data, not as a bearish signal.
    if np.isnan(r):
        score_rsi = 0.0
    else:
        rsi_config = SCREEN_CONFIG["rsi"]
        score_rsi = SCORE_WEIGHTS["momentum"] / 2 * max(
            0,
            1 - abs(r - rsi_config["ideal"]) / rsi_config["distance"],
        )
    score_macd = SCORE_WEIGHTS["momentum"] / 2 if macd_bullish else 0
    momentum_score = score_rsi + score_macd

    volume_score = clip_scale(
        vol_ratio,
        *SCREEN_CONFIG["score_ranges"]["volume"],
        SCORE_WEIGHTS["volume"],
    )

    valid_mom = [v for v in mom_returns.values() if not np.isnan(v)]
    avg_mom = float(np.mean(valid_mom)) if valid_mom else np.nan
    price_momentum_score = clip_scale(
        avg_mom,
        *SCREEN_CONFIG["score_ranges"]["price_momentum"],
        SCORE_WEIGHTS["price_momentum"],
    )

    rel_strength_score = clip_scale(
        rel_strength,
        *SCREEN_CONFIG["score_ranges"]["relative_strength"],
        SCORE_WEIGHTS["relative_strength"],
    )
    risk_vol_score = inverse_clip_scale(ann_vol, *risk_range["vol"], SCORE_WEIGHTS["risk_adjustment"] / 2)
    risk_dd_score = inverse_clip_scale(abs(dd), *risk_range["dd"], SCORE_WEIGHTS["risk_adjustment"] / 2)
    risk_score = risk_vol_score + risk_dd_score

    total_score = round(trend_score + momentum_score + volume_score
                        + price_momentum_score + rel_strength_score + risk_score)
    technical_score = int(min(max(total_score, 0), 100))

    rating_data = rating_scores.get(ticker, {})
    weighted_rating = rating_data.get("weighted_rating", np.nan)
    fund_rating_score = rating_to_100(weighted_rating)

    # Unrated ETFs keep their technical score. Rated ETFs use the configured
    # blend, and score_basis makes the distinction visible in the CSV output.
    if pd.isna(fund_rating_score):
        final_score = round(float(technical_score), 2)
        score_basis = "technical_only"
    else:
        final_score = round(fund_rating_score * RATING_WEIGHT + technical_score * TECHNICAL_WEIGHT, 2)
        score_basis = "blended"

    rating_thresholds = SCREEN_CONFIG["rating_thresholds"]
    if final_score >= rating_thresholds["strong_hold"]:
        rating = "Strong hold"
    elif final_score >= rating_thresholds["hold"]:
        rating = "Hold"
    elif final_score >= rating_thresholds["neutral"]:
        rating = "Neutral"
    elif final_score >= rating_thresholds["sell"]:
        rating = "Sell"
    else:
        rating = "Strong sell"

    flags = []
    if not np.isnan(vs_long_pct) and price > ma_short > ma_long:
        flags.append("Uptrend")
    elif not np.isnan(vs_long_pct) and price < ma_short < ma_long:
        flags.append("Downtrend")
    rsi_thresholds = SCREEN_CONFIG["rsi"]
    if r >= rsi_thresholds["overbought"]:
        flags.append("Overbought(RSI)")
    elif r <= rsi_thresholds["oversold"]:
        flags.append("Oversold(RSI)")
    if (
        not np.isnan(vol_ratio)
        and vol_ratio >= SCREEN_CONFIG["volume_surge_threshold"]
    ):
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
# Run the benchmark download and ETF scoring pipeline
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

    pd.set_option("display.width", OUTPUT_CONFIG["display_width"])
    pd.set_option("display.max_columns", OUTPUT_CONFIG["display_max_columns"])

    df.to_csv(
        OUTPUT_CONFIG["scanner_csv"],
        index=OUTPUT_CONFIG["csv_include_index"],
    )
    return df

# ----------------------------------------------------------------------------
# Execute the screen and expose the resulting DataFrame
# ----------------------------------------------------------------------------
print(df)
df = run_etf_screen()
