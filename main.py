# -*- coding: utf-8 -*-
"""
TSE OPTIONS SUITE - نسخه بازنویسی‌شده کامل (Supabase)
جمع‌آوری داده + محاسبات (IV/Greeks/HV) + استراتژی‌ساز
پایگاه‌داده: Supabase (به جای SQLite محلی)
"""
import os
import sys
import time
import math
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client, Client

# ============================================================
# تنظیمات Supabase
# ============================================================
SUPABASE_URL = "https://tniydmqmulepusuxcobp.supabase.co"
SUPABASE_KEY = "sb_publishable_rqmo_3khSuYAcngMkDHKMA_f2Ad-rC5"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# تنظیمات عمومی
# ============================================================
SYMBOLS = [
    "خودرو", "وبملت", "ذوب", "خساپا", "وتجارت", "شستا", "اهرم",
    "وبصادر", "اخابر", "شپنا", "خبهمن", "فملی", "فزر", "بساما",
    "هم تراز", "طعام", "تاصیکو", "موج", "جوانه کوچک", "توان", "اطلس",
]

DB_FILE = "TSE_Options_History.db"  # دیگر استفاده نمی‌شود، فقط برای سازگاری آرگومان‌ها حفظ شده
JSON_FILE = "TSE_Options_Latest.json"
MAX_STRATEGY_HISTORY_RUNS = 5
MAX_RUN_LOG_ENTRIES = 50
RISK_FREE_RATE = 0.30
TRADING_DAYS = 252
HV_WINDOW = 252
REQUEST_DELAY = 0.30
RETRIES = 4
REBUILD = False
RESET_DB = False

# ============================================================
# تعریف مرکزی ستون‌ها - مرجع واحد برای تمام کد
# ============================================================
CONTRACTS_COLUMNS = [
    "ins_code", "option_ticker", "underlying", "ua_ins_code", "option_type",
    "strike", "contract_size", "begin_date", "begin_date_jalali",
    "maturity", "maturity_jalali", "remained_days", "current_oi",
    "market", "last_seen_run"
]

UNDERLYING_HISTORY_COLUMNS = [
    "ins_code", "symbol", "date", "date_jalali", "open", "high", "low",
    "close", "last", "yesterday", "volume", "value", "transactions"
]

OPTION_HISTORY_COLUMNS = [
    "ins_code", "option_ticker", "underlying", "option_type", "strike",
    "date", "date_jalali", "option_close", "volume", "value", "transactions",
    "oi", "price_change_percent", "underlying_close", "underlying_last",
    "days_to_expiration", "moneyness", "status", "historical_volatility",
    "iv", "intrinsic_value", "time_value", "bs_value", "delta", "gamma",
    "theta", "vega", "rho", "break_even", "pnl_at_expiration",
    "risk_free_rate", "contract_size", "maturity", "maturity_jalali",
    "ua_ins_code", "is_carried"
]

STRATEGIES_COLUMNS = [
    "strategy_id", "run_id", "generated_at", "underlying", "underlying_price",
    "maturity", "maturity_jalali", "days_to_expiration", "strategy_type",
    "net_cost", "max_profit", "max_loss", "risk_reward_ratio",
    "breakeven_low", "breakeven_high", "iv_avg", "hv_20d", "iv_hv_signal",
    "net_delta", "net_gamma", "net_theta", "net_vega", "notes"
]

STRATEGY_LEGS_COLUMNS = [
    "leg_id", "strategy_id", "action", "instrument", "option_type",
    "strike", "price", "contract_size", "iv", "delta", "gamma", "theta", "vega"
]

RUNS_COLUMNS = [
    "run_id", "run_time", "symbols_requested", "contracts_active",
    "strategies_built", "new_rows_underlying", "new_rows_option",
    "carried_rows", "errors", "elapsed_seconds"
]

# بررسی یکپارچگی: تعداد ستون‌ها باید با تعریف جدول یکی باشد
assert len(CONTRACTS_COLUMNS) == 15
assert len(UNDERLYING_HISTORY_COLUMNS) == 13
assert len(OPTION_HISTORY_COLUMNS) == 36  # دقیقاً 36 ستون (35 + oi)
assert len(STRATEGIES_COLUMNS) == 23
assert len(STRATEGY_LEGS_COLUMNS) == 13
assert len(RUNS_COLUMNS) == 10

# ============================================================
# متغیرهای سراسری
# ============================================================
errors = 0
requests_count = 0
new_rows_underlying = 0
new_rows_option = 0
carried_rows_count = 0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tsetmc.com/",
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# توابع کمکی
# ============================================================
def normalize_text(value):
    if value is None:
        return ""
    return (
        str(value)
        .strip()
        .replace("ي", "ی")
        .replace("ك", "ک")
        .replace("\u200c", " ")
    )

_SYMBOLS_NORMALIZED = {normalize_text(s): s for s in SYMBOLS}


def request_json(url):
    global errors, requests_count
    for attempt in range(1, RETRIES + 1):
        try:
            requests_count += 1
            response = session.get(url, timeout=(15, 60))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if attempt == RETRIES:
                errors += 1
                print(f"      خطای API: {e}")
            time.sleep(attempt * 1.2)
    return None


def to_native(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value


def gregorian_to_jalali(value):
    try:
        d = pd.to_datetime(value, errors="coerce")
        if pd.isna(d):
            return ""
        gy, gm, gd = d.year, d.month, d.day
        g_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        if gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0):
            g_days[1] = 29
        gy2 = gy + 1 if gm > 2 else gy
        days = (
            355666 + 365 * gy
            + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
            + gd
        )
        for i in range(gm - 1):
            days += g_days[i]
        jy = -1595 + 33 * (days // 12053)
        days %= 12053
        jy += 4 * (days // 1461)
        days %= 1461
        if days > 365:
            jy += (days - 1) // 365
        days = (days - 1) % 365
        if days < 186:
            jm = 1 + days // 31
            jd = 1 + days % 31
        else:
            jm = 7 + (days - 186) // 30
            jd = 1 + (days - 186) % 30
        return f"{jy:04d}/{jm:02d}/{jd:02d}"
    except Exception:
        return ""


def jalali_to_gregorian(jy, jm, jd):
    jy, jm, jd = int(jy), int(jm), int(jd)
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        gy += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
    if days >= 365:
        days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
    days = (days - 1) % 365
    gd = days + 1
    g_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    leap = gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)
    if leap:
        g_days[1] = 29
    gm = 1
    while gd > g_days[gm - 1]:
        gd -= g_days[gm - 1]
        gm += 1
    return pd.Timestamp(year=gy, month=gm, day=gd)


def parse_date(value):
    if pd.isna(value):
        return pd.NaT
    text = str(value).strip()
    if "/" in text:
        try:
            y, m, d = text.split("/")
            return jalali_to_gregorian(y, m, d)
        except Exception:
            pass
    if "-" in text:
        return pd.to_datetime(text, errors="coerce")
    if text.isdigit() and len(text) == 8:
        return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    return pd.NaT


# ============================================================
# API Calls
# ============================================================
def get_full_history(ins_code):
    url = f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{ins_code}/0"
    data = request_json(url)
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data.get("closingPriceDaily", []))


def get_one_day(ins_code, date):
    date_text = date.strftime("%Y%m%d")
    url = f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDaily/{ins_code}/{date_text}"
    data = request_json(url)
    if not data:
        return pd.DataFrame()
    row = data.get("closingPriceDaily")
    if isinstance(row, dict):
        return pd.DataFrame([row])
    if isinstance(row, list):
        return pd.DataFrame(row)
    return pd.DataFrame()


def get_option_market(market_id):
    url = f"https://cdn.tsetmc.com/api/Instrument/GetInstrumentOptionMarketWatch/{market_id}"
    data = request_json(url)
    if not data:
        return []
    return data.get("instrumentOptMarketWatch", [])


def get_contracts():
    rows = []
    skipped_near_miss = set()
    for market_id, market_name in [(1, "بورس"), (2, "فرابورس")]:
        records = get_option_market(market_id)
        for r in records:
            raw_underlying = r.get("lval30_UA")
            normalized = normalize_text(raw_underlying)
            if normalized not in _SYMBOLS_NORMALIZED:
                if raw_underlying:
                    for target in SYMBOLS:
                        if normalized and (normalized in normalize_text(target) or normalize_text(target) in normalized):
                            skipped_near_miss.add((raw_underlying, target))
                continue
            underlying = _SYMBOLS_NORMALIZED[normalized]
            common = {
                "Underlying": underlying,
                "UAInsCode": r.get("uaInsCode"),
                "Strike": r.get("strikePrice"),
                "ContractSize": r.get("contractSize"),
                "BeginDate": r.get("beginDate"),
                "Maturity": r.get("endDate"),
                "TTM": r.get("remainedDay"),
                "Market": market_name,
            }
            call_ticker = r.get("lVal18AFC_C")
            if call_ticker:
                rows.append({
                    **common, "Option": call_ticker, "OptionType": "CALL",
                    "InsCode": r.get("insCode_C"), "OI": r.get("oP_C"),
                })
            put_ticker = r.get("lVal18AFC_P")
            if put_ticker:
                rows.append({
                    **common, "Option": put_ticker, "OptionType": "PUT",
                    "InsCode": r.get("insCode_P"), "OI": r.get("oP_P"),
                })
    if skipped_near_miss:
        print("   هشدار: نمادهای زیر شبیه SYMBOLS بودند اما دقیقاً match نشدند:")
        for raw_name, target in sorted(skipped_near_miss):
            print(f"      دریافتی: '{raw_name}'   <->  در SYMBOLS: '{target}'")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in ["Strike", "ContractSize", "TTM", "OI"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Maturity"] = pd.to_datetime(df["Maturity"], format="%Y%m%d", errors="coerce")
    df["BeginDate"] = pd.to_datetime(df["BeginDate"], format="%Y%m%d", errors="coerce")
    today = pd.Timestamp.today().normalize()
    df = df[df["Maturity"].notna() & (df["Maturity"] >= today)]
    df = df.drop_duplicates(subset=["InsCode"], keep="last")
    return df.sort_values(["Underlying", "Maturity", "OptionType", "Strike"]).reset_index(drop=True)


def normalize_history(raw):
    if raw.empty:
        return raw
    rename = {
        "dEven": "Date", "pClosing": "OptionClose", "pDrCotVal": "Last",
        "priceFirst": "Open", "priceMax": "High", "priceMin": "Low",
        "qTotTran5J": "Volume", "qTotCap": "Value", "zTotTran": "Transactions",
        "priceYesterday": "Yesterday",
    }
    df = raw.rename(columns=rename).copy()
    numeric = ["Open", "High", "Low", "OptionClose", "Last",
               "Yesterday", "Volume", "Value", "Transactions"]
    for col in numeric:
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Date" not in df.columns:
        return pd.DataFrame()
    df["Date"] = df["Date"].apply(parse_date)
    df = df[df["Date"].notna()]
    return df.sort_values("Date").reset_index(drop=True)


# ============================================================
# محاسبات مالی
# ============================================================
def option_status(option_type, S, K):
    if pd.isna(S) or pd.isna(K):
        return ""
    if option_type == "CALL":
        return "ITM" if S > K else ("ATM" if S == K else "OTM")
    if option_type == "PUT":
        return "ITM" if S < K else ("ATM" if S == K else "OTM")
    return ""


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(option_type, S, K, T, sigma, r):
    if any(pd.isna(x) for x in [S, K, T, sigma]) or min(S, K, T, sigma) <= 0:
        return math.nan
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type == "CALL":
            return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
    except Exception:
        return math.nan


def calculate_iv(option_type, market_price, S, K, T, r):
    if any(pd.isna(x) for x in [market_price, S, K, T]) or min(market_price, S, K, T) <= 0:
        return math.nan
    intrinsic = max(S - K, 0) if option_type == "CALL" else max(K - S, 0)
    if market_price < intrinsic:
        return math.nan
    low, high = 0.0001, 5.0
    price_high = bs_price(option_type, S, K, T, high, r)
    while pd.notna(price_high) and price_high < market_price and high < 20:
        high *= 2
        price_high = bs_price(option_type, S, K, T, high, r)
    if pd.isna(price_high) or price_high < market_price:
        return math.nan
    for _ in range(100):
        mid = (low + high) / 2
        price = bs_price(option_type, S, K, T, mid, r)
        if pd.isna(price):
            return math.nan
        if abs(price - market_price) < 1e-7:
            return mid
        if price > market_price:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def calculate_greeks(option_type, S, K, T, sigma, r):
    result = {k: math.nan for k in ["BlackScholesValue", "Delta", "Gamma", "Theta", "Vega", "Rho"]}
    if any(pd.isna(x) for x in [S, K, T, sigma]) or min(S, K, T, sigma) <= 0:
        return result
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_t)
    d2 = d1 - sqrt_t * sigma
    pdf = norm_pdf(d1)
    if option_type == "CALL":
        value = S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
        delta = norm_cdf(d1)
        theta = -S * pdf * sigma / (2 * sqrt_t) - r * K * math.exp(-r * T) * norm_cdf(d2)
        rho = K * T * math.exp(-r * T) * norm_cdf(d2)
    else:
        value = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        delta = norm_cdf(d1) - 1
        theta = -S * pdf * sigma / (2 * sqrt_t) + r * K * math.exp(-r * T) * norm_cdf(-d2)
        rho = -K * T * math.exp(-r * T) * norm_cdf(-d2)
    gamma = pdf / (S * sigma * sqrt_t)
    vega = S * pdf * sqrt_t / 100
    theta /= 365
    rho /= 100
    return {"BlackScholesValue": value, "Delta": delta, "Gamma": gamma,
            "Theta": theta, "Vega": vega, "Rho": rho}


def calculate_hv(underlying_df):
    if underlying_df.empty:
        return pd.DataFrame()
    u = underlying_df.copy()
    u["Close"] = pd.to_numeric(u["Close"], errors="coerce")
    u = u.sort_values("Date")
    log_return = (
        u["Close"].where(u["Close"] > 0)
        .apply(lambda x: math.log(x) if pd.notna(x) else math.nan)
        .diff()
    )
    hv = log_return.rolling(HV_WINDOW, min_periods=10).std() * math.sqrt(TRADING_DAYS)
    return pd.DataFrame({"Date": u["Date"], "HistoricalVolatility": hv})


def compute_hv_map(underlying_df):
    hv_df = calculate_hv(underlying_df)
    if hv_df.empty:
        return {}
    return dict(zip(hv_df["Date"], hv_df["HistoricalVolatility"]))


# ============================================================
# مدیریت پایگاه‌داده Supabase
# ============================================================
def reset_database():
    print("   توجه: در حالت Supabase، حذف دیتابیس محلی انجام نمی‌شود. جداول باید در داشبورد Supabase مدیریت شوند.")


def init_db():
    print("   بررسی اتصال به Supabase...")
    try:
        result = supabase.table("runs").select("run_id").limit(1).execute()
        print("   ✅ اتصال به Supabase موفق بود")
        return supabase
    except Exception as e:
        print(f"   ❌ خطا در اتصال به Supabase: {e}")
        raise SystemExit(1)


# ============================================================
# به‌روزرسانی دیتابیس
# ============================================================
def update_underlying_history(client, symbol, ins_code):
    global new_rows_underlying
    ins_code = str(ins_code)
    
    if REBUILD:
        client.table("underlying_price_history").delete().eq("ins_code", ins_code).execute()
    
    last_date_res = client.table("underlying_price_history").select("date").eq("ins_code", ins_code).order("date", desc=True).limit(1).execute()
    last_date_str = last_date_res.data[0]["date"] if last_date_res.data else None
    
    if last_date_str is None:
        raw = get_full_history(ins_code)
        time.sleep(REQUEST_DELAY)
        df = normalize_history(raw)
    else:
        last_date = pd.to_datetime(last_date_str)
        today = pd.Timestamp.today().normalize()
        start = last_date + timedelta(days=1)
        pieces = []
        while start <= today:
            day = get_one_day(ins_code, start)
            if not day.empty:
                pieces.append(day)
            start += timedelta(days=1)
            time.sleep(REQUEST_DELAY)
        df = normalize_history(pd.concat(pieces, ignore_index=True)) if pieces else pd.DataFrame()
    
    if df.empty:
        return 0
    
    if "OptionClose" in df.columns:
        df = df.rename(columns={"OptionClose": "Close"})
    
    inserted = 0
    rows_to_upsert = []
    for _, row in df.iterrows():
        date = row["Date"]
        rows_to_upsert.append({
            "ins_code": ins_code,
            "symbol": symbol,
            "date": date.strftime("%Y-%m-%d"),
            "date_jalali": gregorian_to_jalali(date),
            "open": to_native(row.get("Open")),
            "high": to_native(row.get("High")),
            "low": to_native(row.get("Low")),
            "close": to_native(row.get("Close")),
            "last": to_native(row.get("Last")),
            "yesterday": to_native(row.get("Yesterday")),
            "volume": to_native(row.get("Volume")),
            "value": to_native(row.get("Value")),
            "transactions": to_native(row.get("Transactions")),
        })
        inserted += 1
    
    if rows_to_upsert:
        client.table("underlying_price_history").upsert(rows_to_upsert).execute()
    
    new_rows_underlying += inserted
    return inserted


def load_underlying_frame(client, ins_code):
    res = client.table("underlying_price_history").select("date, close, last").eq("ins_code", str(ins_code)).order("date", asc=True).execute()
    if not res.data:
        return pd.DataFrame()
    df = pd.DataFrame(res.data)
    df["Date"] = pd.to_datetime(df["date"])
    df["Close"] = pd.to_numeric(df["close"], errors="coerce")
    df["Last"] = pd.to_numeric(df["last"], errors="coerce")
    return df[["Date", "Close", "Last"]]


def option_last_row(client, ins_code):
    res = client.table("option_price_history").select("date, option_close, underlying_last").eq("ins_code", str(ins_code)).order("date", desc=True).limit(1).execute()
    if not res.data:
        return None
    row = res.data[0]
    return {"date": pd.to_datetime(row["date"]), "option_close": row["option_close"], "last": row["underlying_last"]}


def fetch_new_option_trades(ins_code, since_date):
    if since_date is None:
        raw = get_full_history(ins_code)
        time.sleep(REQUEST_DELAY)
        return normalize_history(raw)
    today = pd.Timestamp.today().normalize()
    start = since_date + timedelta(days=1)
    pieces = []
    while start <= today:
        day = get_one_day(ins_code, start)
        if not day.empty:
            pieces.append(day)
        start += timedelta(days=1)
        time.sleep(REQUEST_DELAY)
    if not pieces:
        return pd.DataFrame()
    return normalize_history(pd.concat(pieces, ignore_index=True))


def update_option_history(client, contract, underlying_df, hv_map, run_date=None):
    global new_rows_option, carried_rows_count, errors
    
    ins_code = str(contract["InsCode"])
    
    raw_contract_oi = contract.get("OI") if hasattr(contract, "get") else None
    try:
        contract_oi = float(raw_contract_oi) if raw_contract_oi is not None and pd.notna(raw_contract_oi) else None
    except (TypeError, ValueError):
        contract_oi = None
    
    if REBUILD:
        client.table("option_price_history").delete().eq("ins_code", ins_code).execute()
        last_row = None
    else:
        last_row = option_last_row(client, ins_code)
    
    since_date = last_row["date"] if last_row else None
    raw_trades = fetch_new_option_trades(ins_code, since_date)
    
    raw_by_date = {}
    if not raw_trades.empty:
        for _, r in raw_trades.iterrows():
            raw_by_date[r["Date"]] = r
    
    if underlying_df.empty:
        return 0
    
    if since_date is not None:
        needed = underlying_df[underlying_df["Date"] > since_date]["Date"].tolist()
    else:
        candidate_dates = sorted(set(underlying_df["Date"].tolist()) | set(raw_by_date.keys()))
        floor_date = None
        if raw_by_date:
            floor_date = min(raw_by_date.keys())
        begin_date = contract.get("BeginDate") if hasattr(contract, "get") else None
        if begin_date is not None and pd.notna(begin_date):
            floor_date = begin_date if floor_date is None else max(floor_date, begin_date)
        if floor_date is not None:
            candidate_dates = [d for d in candidate_dates if d >= floor_date]
        needed = candidate_dates
    
    needed = sorted(set(needed))
    if not needed:
        return 0
    
    underlying_close_map = dict(zip(underlying_df["Date"], underlying_df["Close"]))
    underlying_last_map = dict(zip(underlying_df["Date"], underlying_df.get("Last", pd.Series(dtype=float))))
    running_close = last_row["option_close"] if last_row else None
    
    maturity = contract["Maturity"]
    strike = float(contract["Strike"]) if pd.notna(contract["Strike"]) else math.nan
    option_type = contract["OptionType"]
    contract_size = float(contract["ContractSize"]) if pd.notna(contract["ContractSize"]) else 1.0
    ua_ins_code = str(contract["UAInsCode"])
    option_ticker = contract["Option"]
    underlying_name = contract["Underlying"]
    
    rows_to_upsert = []
    inserted = 0
    
    for date in needed:
        try:
            S = underlying_close_map.get(date)
            last_price = to_native(underlying_last_map.get(date))
            
            if date in raw_by_date:
                r = raw_by_date[date]
                option_close = to_native(r["OptionClose"])
                volume = to_native(r["Volume"]) or 0
                value = to_native(r["Value"]) or 0
                transactions = to_native(r["Transactions"]) or 0
                is_carried = 0
            else:
                option_close = running_close
                volume, value, transactions = 0, 0, 0
                is_carried = 1
                carried_rows_count += 1
            
            price_change_pct = None
            if running_close is not None and option_close is not None and running_close != 0:
                price_change_pct = (option_close - running_close) / running_close
            
            days_to_exp = None
            if pd.notna(maturity):
                days_to_exp = max((maturity - date).days, 0)
            
            status = option_status(option_type, S, strike)
            
            moneyness = None
            if S is not None and pd.notna(strike) and S != 0 and strike != 0:
                moneyness = (S / strike) if option_type == "CALL" else (strike / S)
            
            intrinsic = None
            time_value = None
            if S is not None and pd.notna(strike):
                intrinsic = max(S - strike, 0) if option_type == "CALL" else max(strike - S, 0)
            if option_close is not None and intrinsic is not None:
                time_value = max(option_close - intrinsic, 0)
            
            iv = math.nan
            greeks = {"BlackScholesValue": math.nan, "Delta": math.nan, "Gamma": math.nan,
                      "Theta": math.nan, "Vega": math.nan, "Rho": math.nan}
            if (S is not None and pd.notna(strike) and option_close is not None
                    and days_to_exp and days_to_exp > 0 and option_close > 0):
                T = days_to_exp / 365
                iv = calculate_iv(option_type, option_close, S, strike, T, RISK_FREE_RATE)
                if pd.notna(iv):
                    greeks = calculate_greeks(option_type, S, strike, T, iv, RISK_FREE_RATE)
            
            break_even = None
            if pd.notna(strike) and option_close is not None:
                break_even = strike + option_close if option_type == "CALL" else strike - option_close
            
            pnl = None
            if S is not None and pd.notna(strike) and option_close is not None:
                pnl = (max(S - strike, 0) - option_close) if option_type == "CALL" else (max(strike - S, 0) - option_close)
            
            hv = to_native(hv_map.get(date))
            
            row_oi = None
            if run_date is not None and date == run_date and contract_oi is not None:
                row_oi = contract_oi
            
            row_dict = {
                "ins_code": ins_code,
                "option_ticker": option_ticker,
                "underlying": underlying_name,
                "option_type": option_type,
                "strike": to_native(strike),
                "date": date.strftime("%Y-%m-%d"),
                "date_jalali": gregorian_to_jalali(date),
                "option_close": to_native(option_close),
                "volume": to_native(volume),
                "value": to_native(value),
                "transactions": to_native(transactions),
                "oi": to_native(row_oi),
                "price_change_percent": to_native(price_change_pct),
                "underlying_close": to_native(S),
                "underlying_last": to_native(last_price),
                "days_to_expiration": to_native(days_to_exp),
                "moneyness": to_native(moneyness),
                "status": status,
                "historical_volatility": hv,
                "iv": to_native(iv),
                "intrinsic_value": to_native(intrinsic),
                "time_value": to_native(time_value),
                "bs_value": to_native(greeks["BlackScholesValue"]),
                "delta": to_native(greeks["Delta"]),
                "gamma": to_native(greeks["Gamma"]),
                "theta": to_native(greeks["Theta"]),
                "vega": to_native(greeks["Vega"]),
                "rho": to_native(greeks["Rho"]),
                "break_even": to_native(break_even),
                "pnl_at_expiration": to_native(pnl),
                "risk_free_rate": RISK_FREE_RATE,
                "contract_size": contract_size,
                "maturity": maturity.strftime("%Y-%m-%d") if pd.notna(maturity) else None,
                "maturity_jalali": gregorian_to_jalali(maturity) if pd.notna(maturity) else None,
                "ua_ins_code": ua_ins_code,
                "is_carried": is_carried,
            }
            rows_to_upsert.append(row_dict)
            inserted += 1
            running_close = option_close
            
        except Exception as e:
            errors += 1
            print(f"      خطا در ردیف {date.strftime('%Y-%m-%d')}: {e}")
            continue
    
    if rows_to_upsert:
        chunk_size = 500
        for i in range(0, len(rows_to_upsert), chunk_size):
            chunk = rows_to_upsert[i:i+chunk_size]
            client.table("option_price_history").upsert(chunk).execute()
    
    new_rows_option += inserted
    return inserted


def update_contracts_table(client, contracts_df, run_id):
    rows_to_upsert = []
    for _, r in contracts_df.iterrows():
        rows_to_upsert.append({
            "ins_code": to_native(str(r["InsCode"])),
            "option_ticker": to_native(r["Option"]),
            "underlying": to_native(r["Underlying"]),
            "ua_ins_code": to_native(str(r["UAInsCode"])),
            "option_type": to_native(r["OptionType"]),
            "strike": to_native(r["Strike"]),
            "contract_size": to_native(r["ContractSize"]),
            "begin_date": r["BeginDate"].strftime("%Y-%m-%d") if pd.notna(r.get("BeginDate")) else None,
            "begin_date_jalali": gregorian_to_jalali(r["BeginDate"]) if pd.notna(r.get("BeginDate")) else None,
            "maturity": r["Maturity"].strftime("%Y-%m-%d"),
            "maturity_jalali": gregorian_to_jalali(r["Maturity"]),
            "remained_days": to_native(r.get("TTM")),
            "current_oi": to_native(r.get("OI")),
            "market": to_native(r.get("Market")),
            "last_seen_run": to_native(run_id),
        })
    
    if rows_to_upsert:
        client.table("contracts").upsert(rows_to_upsert).execute()


# ============================================================
# استراتژی‌ساز
# ============================================================
def nearest_strike_row(df, price):
    if df.empty or price is None:
        return None
    d = df.copy()
    d["diff"] = (d["strike"] - price).abs()
    return d.sort_values("diff").iloc[0]


def next_higher_strike_row(df, base_strike):
    higher = df[df["strike"] > base_strike]
    if higher.empty:
        return None
    return higher.sort_values("strike").iloc[0]


def iv_hv_verdict(iv_avg, hv):
    if iv_avg is None or hv is None or pd.isna(iv_avg) or pd.isna(hv) or hv == 0:
        return "داده کافی برای مقایسه IV/HV موجود نیست"
    ratio = iv_avg / hv
    if ratio >= 1.15:
        return f"IV نسبت به نوسان تاریخی گران‌تر است (IV/HV={ratio:.2f}) → فروش پرمیوم موجه‌تر"
    if ratio <= 0.85:
        return f"IV نسبت به نوسان تاریخی ارزان‌تر است (IV/HV={ratio:.2f}) → خرید پرمیوم موجه‌تر"
    return f"IV و HV تقریباً هم‌راستا هستند (IV/HV={ratio:.2f})"


def make_leg(action, instrument, option_type, strike, price, contract_size, row=None):
    iv = delta = gamma = theta = vega = None
    is_carried = 0
    if row is not None:
        iv = row.get("iv")
        delta = row.get("delta")
        gamma = row.get("gamma")
        theta = row.get("theta")
        vega = row.get("vega")
        is_carried = row.get("is_carried") or 0
    sign = 1 if action == "BUY" else (-1 if action == "SELL" else 0)
    d = (delta or 0) * (contract_size or 0) * sign
    g = (gamma or 0) * (contract_size or 0) * sign
    t = (theta or 0) * (contract_size or 0) * sign
    v = (vega or 0) * (contract_size or 0) * sign
    if action == "HOLD_STOCK":
        d = contract_size or 0
    return {
        "action": action, "instrument": instrument, "option_type": option_type,
        "strike": strike, "price": price, "contract_size": contract_size,
        "iv": iv, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
        "is_carried": is_carried,
        "_delta_contrib": d, "_gamma_contrib": g, "_theta_contrib": t, "_vega_contrib": v,
    }


def save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
                  strategy_type, net_cost, max_profit, max_loss,
                  breakeven_low, breakeven_high, iv_avg, hv, notes, legs):
    net_delta = sum(leg["_delta_contrib"] for leg in legs)
    net_gamma = sum(leg["_gamma_contrib"] for leg in legs)
    net_theta = sum(leg["_theta_contrib"] for leg in legs)
    net_vega = sum(leg["_vega_contrib"] for leg in legs)
    ratio = None
    if max_profit is not None and max_loss is not None and max_loss > 0:
        ratio = max_profit / max_loss
    signal = iv_hv_verdict(iv_avg, hv)
    carried_note = ""
    if any(leg.get("is_carried") for leg in legs):
        carried_note = " ⚠ حداقل یک پایه از این استراتژی امروز معامله واقعی نداشته."
    
    strategy_data = {
        "run_id": to_native(run_id),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "underlying": to_native(underlying),
        "underlying_price": to_native(underlying_price),
        "maturity": maturity.strftime("%Y-%m-%d"),
        "maturity_jalali": gregorian_to_jalali(maturity),
        "days_to_expiration": to_native(days_to_exp),
        "strategy_type": strategy_type,
        "net_cost": to_native(net_cost),
        "max_profit": to_native(max_profit),
        "max_loss": to_native(max_loss),
        "risk_reward_ratio": to_native(ratio),
        "breakeven_low": to_native(breakeven_low),
        "breakeven_high": to_native(breakeven_high),
        "iv_avg": to_native(iv_avg),
        "hv_20d": to_native(hv),
        "iv_hv_signal": signal + carried_note,
        "net_delta": to_native(net_delta),
        "net_gamma": to_native(net_gamma),
        "net_theta": to_native(net_theta),
        "net_vega": to_native(net_vega),
        "notes": notes + carried_note,
    }
    
    res = client.table("strategies").insert(strategy_data).execute()
    strategy_id = res.data[0]["strategy_id"]
    
    legs_to_insert = []
    for leg in legs:
        legs_to_insert.append({
            "strategy_id": strategy_id,
            "action": leg["action"],
            "instrument": leg["instrument"],
            "option_type": leg.get("option_type"),
            "strike": to_native(leg.get("strike")),
            "price": to_native(leg.get("price")),
            "contract_size": to_native(leg.get("contract_size")),
            "iv": to_native(leg.get("iv")),
            "delta": to_native(leg.get("delta")),
            "gamma": to_native(leg.get("gamma")),
            "theta": to_native(leg.get("theta")),
            "vega": to_native(leg.get("vega")),
        })
    
    if legs_to_insert:
        client.table("strategy_legs").insert(legs_to_insert).execute()
    
    return strategy_id


def build_strategy_set(client, run_id, underlying, maturity, group, underlying_price, hv):
    calls = group[group["option_type"] == "CALL"].copy()
    puts = group[group["option_type"] == "PUT"].copy()
    if calls.empty and puts.empty:
        return 0
    atm_call = nearest_strike_row(calls, underlying_price)
    atm_put = nearest_strike_row(puts, underlying_price)
    dte_series = group["days_to_expiration"].dropna()
    days_to_exp = int(dte_series.iloc[0]) if not dte_series.empty else None
    count = 0
    
    if atm_call is not None and pd.notna(atm_call["option_close"]):
        premium, size = atm_call["option_close"], atm_call["contract_size"] or 1
        cost = premium * size
        leg = make_leg("BUY", atm_call["option_ticker"], "CALL", atm_call["strike"], premium, size, atm_call)
        save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
            "Long Call", cost, None, cost, atm_call["strike"] + premium, None,
            atm_call.get("iv"), hv, "سود نامحدود، زیان محدود به پرمیوم", [leg])
        count += 1
    
    if atm_put is not None and pd.notna(atm_put["option_close"]):
        premium, size = atm_put["option_close"], atm_put["contract_size"] or 1
        cost = premium * size
        max_profit = (atm_put["strike"] - premium) * size
        leg = make_leg("BUY", atm_put["option_ticker"], "PUT", atm_put["strike"], premium, size, atm_put)
        save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
            "Long Put", cost, max_profit, cost, atm_put["strike"] - premium, None,
            atm_put.get("iv"), hv, "سود در افت شدید سهم", [leg])
        count += 1
    
    if atm_call is not None and pd.notna(atm_call["option_close"]):
        premium, size = atm_call["option_close"], atm_call["contract_size"] or 1
        net_cost = (underlying_price - premium) * size
        max_profit = ((atm_call["strike"] - underlying_price) + premium) * size
        max_loss = (underlying_price - premium) * size
        stock_leg = make_leg("HOLD_STOCK", underlying, None, None, underlying_price, size)
        call_leg = make_leg("SELL", atm_call["option_ticker"], "CALL", atm_call["strike"], premium, size, atm_call)
        save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
            "Covered Call", net_cost, max_profit, max_loss, underlying_price - premium, None,
            atm_call.get("iv"), hv, "مالکیت سهم + فروش Call", [stock_leg, call_leg])
        count += 1
    
    if atm_put is not None and pd.notna(atm_put["option_close"]):
        premium, size = atm_put["option_close"], atm_put["contract_size"] or 1
        cash_reserved = atm_put["strike"] * size
        max_profit = premium * size
        max_loss = (atm_put["strike"] - premium) * size
        leg = make_leg("SELL", atm_put["option_ticker"], "PUT", atm_put["strike"], premium, size, atm_put)
        save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
            "Cash-Secured Put", -max_profit, max_profit, max_loss, atm_put["strike"] - premium, None,
            atm_put.get("iv"), hv, f"نقد رزروشده: {cash_reserved:,.0f}", [leg])
        count += 1
    
    if atm_put is not None and pd.notna(atm_put["option_close"]):
        premium, size = atm_put["option_close"], atm_put["contract_size"] or 1
        net_cost = (underlying_price + premium) * size
        max_loss = ((underlying_price - atm_put["strike"]) + premium) * size
        stock_leg = make_leg("HOLD_STOCK", underlying, None, None, underlying_price, size)
        put_leg = make_leg("BUY", atm_put["option_ticker"], "PUT", atm_put["strike"], premium, size, atm_put)
        save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
            "Married Put", net_cost, None, max_loss, underlying_price + premium, None,
            atm_put.get("iv"), hv, "مالکیت سهم + خرید Put بیمه‌ای", [stock_leg, put_leg])
        count += 1
    
    if atm_call is not None:
        higher_call = next_higher_strike_row(calls, atm_call["strike"])
        if higher_call is not None and pd.notna(atm_call["option_close"]) and pd.notna(higher_call["option_close"]):
            size = atm_call["contract_size"] or 1
            premium_buy, premium_sell = atm_call["option_close"], higher_call["option_close"]
            net_debit = (premium_buy - premium_sell) * size
            width = (higher_call["strike"] - atm_call["strike"]) * size
            max_profit = width - net_debit
            max_loss = net_debit
            buy_leg = make_leg("BUY", atm_call["option_ticker"], "CALL", atm_call["strike"], premium_buy, size, atm_call)
            sell_leg = make_leg("SELL", higher_call["option_ticker"], "CALL", higher_call["strike"], premium_sell, size, higher_call)
            ivs = [x for x in [atm_call.get("iv"), higher_call.get("iv")] if x is not None and pd.notna(x)]
            avg_iv = sum(ivs) / len(ivs) if ivs else None
            save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
                "Bull Call Spread", net_debit, max_profit, max_loss,
                atm_call["strike"] + (premium_buy - premium_sell), None,
                avg_iv, hv, "خرید Call + فروش Call بالاتر", [buy_leg, sell_leg])
            count += 1
    
    if (atm_call is not None and atm_put is not None
            and atm_call["strike"] == atm_put["strike"]
            and pd.notna(atm_call["option_close"]) and pd.notna(atm_put["option_close"])):
        size = atm_call["contract_size"] or 1
        premium_call, premium_put = atm_call["option_close"], atm_put["option_close"]
        net_cost = (premium_call + premium_put) * size
        call_leg = make_leg("BUY", atm_call["option_ticker"], "CALL", atm_call["strike"], premium_call, size, atm_call)
        put_leg = make_leg("BUY", atm_put["option_ticker"], "PUT", atm_put["strike"], premium_put, size, atm_put)
        ivs = [x for x in [atm_call.get("iv"), atm_put.get("iv")] if x is not None and pd.notna(x)]
        avg_iv = sum(ivs) / len(ivs) if ivs else None
        save_strategy(client, run_id, underlying, underlying_price, maturity, days_to_exp,
            "Straddle", net_cost, None, net_cost,
            atm_call["strike"] - (premium_call + premium_put),
            atm_call["strike"] + (premium_call + premium_put),
            avg_iv, hv, "خرید Call و Put هم‌استرایک", [call_leg, put_leg])
        count += 1
    
    return count


def build_strategies_from_db(client, run_id):
    contracts_res = client.table("contracts").select("ins_code").eq("last_seen_run", int(run_id)).execute()
    if not contracts_res.data:
        return 0
    
    ins_codes = [c["ins_code"] for c in contracts_res.data]
    
    latest_records = []
    for ins_code in ins_codes:
        res = client.table("option_price_history").select("*").eq("ins_code", ins_code).order("date", desc=True).limit(1).execute()
        if res.data:
            latest_records.extend(res.data)
            
    if not latest_records:
        return 0
        
    latest = pd.DataFrame(latest_records)
    latest["maturity"] = pd.to_datetime(latest["maturity"])
    
    total = 0
    for (underlying, maturity), group in latest.groupby(["underlying", "maturity"]):
        price_series = group["underlying_close"].dropna()
        if price_series.empty:
            continue
        underlying_price = float(price_series.iloc[0])
        hv_series = group["historical_volatility"].dropna()
        hv = float(hv_series.iloc[0]) if not hv_series.empty else None
        total += build_strategy_set(client, run_id, underlying, maturity, group, underlying_price, hv)
    return total


# ============================================================
# خروجی JSON
# ============================================================
def load_existing_json(json_file):
    if not os.path.exists(json_file):
        return None
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"   هشدار: فایل JSON قبلی خراب بود ({e})")
        return None


def build_strategy_records(client, run_id):
    strategies_res = client.table("strategies").select("*").eq("run_id", run_id).order("strategy_id", desc=True).execute()
    if not strategies_res.data:
        return []
    
    records = []
    for s in strategies_res.data:
        strategy_id = s["strategy_id"]
        legs_res = client.table("strategy_legs").select("*").eq("strategy_id", strategy_id).execute()
        legs_data = legs_res.data if legs_res.data else []
        
        strategy = {
            "strategy_id": int(strategy_id),
            "run_id": int(run_id),
            "generated_at": s["generated_at"],
            "underlying": s["underlying"],
            "underlying_price": float(s["underlying_price"]) if pd.notna(s["underlying_price"]) else None,
            "maturity_jalali": s["maturity_jalali"],
            "days_to_expiration": int(s["days_to_expiration"]) if pd.notna(s["days_to_expiration"]) else None,
            "strategy_type": s["strategy_type"],
            "net_cost": float(s["net_cost"]) if pd.notna(s["net_cost"]) else None,
            "max_profit": float(s["max_profit"]) if pd.notna(s["max_profit"]) else None,
            "max_loss": float(s["max_loss"]) if pd.notna(s["max_loss"]) else None,
            "risk_reward_ratio": float(s["risk_reward_ratio"]) if pd.notna(s["risk_reward_ratio"]) else None,
            "breakeven_low": float(s["breakeven_low"]) if pd.notna(s["breakeven_low"]) else None,
            "breakeven_high": float(s["breakeven_high"]) if pd.notna(s["breakeven_high"]) else None,
            "iv_avg": float(s["iv_avg"]) if pd.notna(s["iv_avg"]) else None,
            "hv_20d": float(s["hv_20d"]) if pd.notna(s["hv_20d"]) else None,
            "iv_hv_signal": s["iv_hv_signal"],
            "net_delta": float(s["net_delta"]) if pd.notna(s["net_delta"]) else None,
            "net_gamma": float(s["net_gamma"]) if pd.notna(s["net_gamma"]) else None,
            "net_theta": float(s["net_theta"]) if pd.notna(s["net_theta"]) else None,
            "net_vega": float(s["net_vega"]) if pd.notna(s["net_vega"]) else None,
            "notes": s["notes"],
            "legs": []
        }
        for leg_row in legs_data:
            if pd.notna(leg_row.get("action")):
                strategy["legs"].append({
                    "action": leg_row["action"],
                    "instrument": leg_row["instrument"],
                    "option_type": leg_row.get("option_type"),
                    "strike": float(leg_row["strike"]) if pd.notna(leg_row["strike"]) else None,
                    "price": float(leg_row["price"]) if pd.notna(leg_row["price"]) else None,
                    "contract_size": float(leg_row["contract_size"]) if pd.notna(leg_row["contract_size"]) else None,
                    "iv": float(leg_row["iv"]) if pd.notna(leg_row["iv"]) else None,
                    "delta": float(leg_row["delta"]) if pd.notna(leg_row["delta"]) else None,
                    "gamma": float(leg_row["gamma"]) if pd.notna(leg_row["gamma"]) else None,
                    "theta": float(leg_row["theta"]) if pd.notna(leg_row["theta"]) else None,
                    "vega": float(leg_row["vega"]) if pd.notna(leg_row["vega"]) else None
                })
        records.append(strategy)
    return records


def build_options_summary(client):
    max_date_res = client.table("option_price_history").select("date").order("date", desc=True).limit(1).execute()
    if not max_date_res.data:
        return []
    max_date = max_date_res.data[0]["date"]
    
    res = client.table("option_price_history").select(
        "option_ticker, underlying, option_type, strike, date_jalali, option_close, volume, iv, delta, gamma, theta, vega, historical_volatility, days_to_expiration, status"
    ).eq("date", max_date).limit(100).execute()
    
    if not res.data:
        return []
        
    out = []
    for row in res.data:
        out.append({
            "option_ticker": row["option_ticker"],
            "underlying": row["underlying"],
            "option_type": row["option_type"],
            "strike": float(row["strike"]) if pd.notna(row["strike"]) else None,
            "date_jalali": row["date_jalali"],
            "option_close": float(row["option_close"]) if pd.notna(row["option_close"]) else None,
            "volume": float(row["volume"]) if pd.notna(row["volume"]) else None,
            "iv": float(row["iv"]) if pd.notna(row["iv"]) else None,
            "delta": float(row["delta"]) if pd.notna(row["delta"]) else None,
            "gamma": float(row["gamma"]) if pd.notna(row["gamma"]) else None,
            "theta": float(row["theta"]) if pd.notna(row["theta"]) else None,
            "vega": float(row["vega"]) if pd.notna(row["vega"]) else None,
            "historical_volatility": float(row["historical_volatility"]) if pd.notna(row["historical_volatility"]) else None,
            "days_to_expiration": int(row["days_to_expiration"]) if pd.notna(row["days_to_expiration"]) else None,
            "status": row["status"]
        })
    return out


def export_to_json(client, run_id):
    existing = load_existing_json(JSON_FILE)
    if existing is None:
        existing = {
            "metadata": {"script_name": "TSE Options Suite", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            "run_history": [],
            "strategies_recent": [],
            "options_latest_data": []
        }
    
    run_stats_res = client.table("runs").select("*").eq("run_id", run_id).execute()
    if not run_stats_res.data:
        return JSON_FILE
    run_stats = run_stats_res.data[0]
    
    existing["run_history"].append({
        "run_id": run_id,
        "run_time": run_stats.get("run_time"),
        "symbols_requested": run_stats.get("symbols_requested"),
        "contracts_active": run_stats.get("contracts_active"),
        "strategies_built": run_stats.get("strategies_built"),
        "new_rows_underlying": run_stats.get("new_rows_underlying"),
        "new_rows_option": run_stats.get("new_rows_option"),
        "carried_rows": run_stats.get("carried_rows"),
        "errors": run_stats.get("errors"),
        "elapsed_seconds": run_stats.get("elapsed_seconds"),
    })
    existing["run_history"] = existing["run_history"][-MAX_RUN_LOG_ENTRIES:]
    new_strategies = build_strategy_records(client, run_id)
    existing["strategies_recent"].extend(new_strategies)
    kept_run_ids = sorted({s["run_id"] for s in existing["strategies_recent"]}, reverse=True)[:MAX_STRATEGY_HISTORY_RUNS]
    existing["strategies_recent"] = [s for s in existing["strategies_recent"] if s["run_id"] in kept_run_ids]
    existing["options_latest_data"] = build_options_summary(client)
    existing["metadata"]["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing["metadata"]["last_run_id"] = run_id
    tmp_file = JSON_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, JSON_FILE)
    print(f"\n✅ فایل JSON به‌روزرسانی شد: {os.path.abspath(JSON_FILE)}")
    return JSON_FILE


def get_last_jalali_date_for_symbol(client, ua_code):
    res = client.table("underlying_price_history").select("date_jalali").eq("ins_code", str(ua_code)).order("date", desc=True).limit(1).execute()
    if res.data:
        return res.data[0]["date_jalali"]
    return None


def process_symbol(client, symbol, contracts_df, run_date=None):
    global errors
    symbol_contracts = contracts_df[contracts_df["Underlying"] == symbol]
    if symbol_contracts.empty:
        return {"updated": 0, "up_to_date": 0, "failed": 0, "extracted": False, "last_date_jalali": None}
    ua_code = str(symbol_contracts.iloc[0]["UAInsCode"])
    print(f"\n{symbol} ...")
    try:
        n_new = update_underlying_history(client, symbol, ua_code)
        print(f"   سهم پایه: {n_new} روز جدید")
    except Exception as e:
        errors += 1
        print(f"   خطا در سهم پایه: {e}")
        return {"updated": 0, "up_to_date": 0, "failed": len(symbol_contracts), "extracted": False, "last_date_jalali": None}
    underlying_df = load_underlying_frame(client, ua_code)
    if underlying_df.empty:
        print("   تاریخچه سهم پایه یافت نشد")
        return {"updated": 0, "up_to_date": 0, "failed": len(symbol_contracts), "extracted": False, "last_date_jalali": None}
    hv_map = compute_hv_map(underlying_df)
    updated = up_to_date = failed = 0
    for i, (_, contract) in enumerate(symbol_contracts.iterrows(), start=1):
        ticker = contract["Option"]
        try:
            n = update_option_history(client, contract, underlying_df, hv_map, run_date=run_date)
            if n > 0:
                updated += 1
                if i <= 3 or i % 10 == 0:
                    print(f"   [{i}/{len(symbol_contracts)}] {ticker}: {n} روز جدید")
            else:
                up_to_date += 1
        except Exception as e:
            failed += 1
            errors += 1
            print(f"   [{i}/{len(symbol_contracts)}] {ticker}: خطا: {e}")
    last_date_jalali = get_last_jalali_date_for_symbol(client, ua_code)
    return {
        "updated": updated, "up_to_date": up_to_date, "failed": failed,
        "extracted": True, "last_date_jalali": last_date_jalali,
    }


def main():
    global errors, requests_count, new_rows_underlying, new_rows_option, carried_rows_count
    
    start_time = time.time()
    print()
    print("=" * 75)
    print("      TSE OPTIONS SUITE - نسخه بازنویسی‌شده کامل (Supabase)")
    print("=" * 75)
    print(f"تاریخ اجرا: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"پایگاه‌داده: Supabase ({SUPABASE_URL})")
    print(f"فایل JSON: {os.path.abspath(JSON_FILE)}")
    print(f"حالت: {'REBUILD' if REBUILD else 'افزایشی'}")
    if RESET_DB:
        print("️  حالت RESET: در Supabase جداول باید دستی پاک شوند یا از REBUILD استفاده کنید.")
    print("=" * 75)
    
    if RESET_DB or REBUILD:
        reset_database()
    
    client = init_db()
    
    run_data = {"run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    res = client.table("runs").insert(run_data).execute()
    run_id = res.data[0]["run_id"]
    
    print()
    print("1) دریافت قراردادهای جاری...")
    contracts = get_contracts()
    if contracts.empty:
        print("هیچ قرارداد جاری پیدا نشد.")
        return
    print(f"   کل قراردادهای جاری: {len(contracts):,}")
    run_date = pd.Timestamp.today().normalize()
    update_contracts_table(client, contracts, run_id)
    
    print()
    print("2) به‌روزرسانی افزایشی تاریخچه...")
    total_updated = total_up_to_date = total_failed = 0
    symbols_by_last_date = {}
    symbols_not_extracted = []
    for symbol in SYMBOLS:
        res = process_symbol(client, symbol, contracts, run_date=run_date)
        total_updated += res["updated"]
        total_up_to_date += res["up_to_date"]
        total_failed += res["failed"]
        if res["extracted"] and res["last_date_jalali"]:
            symbols_by_last_date.setdefault(res["last_date_jalali"], []).append(symbol)
        else:
            symbols_not_extracted.append(symbol)
    
    print()
    print("3) ساخت استراتژی‌ها...")
    strategies_built = build_strategies_from_db(client, run_id)
    print(f"   {strategies_built} استراتژی ساخته شد")
    
    print()
    print("4) به‌روزرسانی JSON...")
    json_file = export_to_json(client, run_id)
    
    elapsed = time.time() - start_time
    
    update_data = {
        "symbols_requested": len(SYMBOLS),
        "contracts_active": len(contracts),
        "strategies_built": strategies_built,
        "new_rows_underlying": new_rows_underlying,
        "new_rows_option": new_rows_option,
        "carried_rows": carried_rows_count,
        "errors": errors,
        "elapsed_seconds": elapsed
    }
    client.table("runs").update(update_data).eq("run_id", run_id).execute()
    
    print()
    print("=" * 75)
    print("                 پایان عملیات")
    print("=" * 75)
    print(f"قراردادهای به‌روزشده: {total_updated:,} | از قبل به‌روز: {total_up_to_date:,} | ناموفق: {total_failed:,}")
    print(f"رکوردهای جدید سهم پایه: {new_rows_underlying:,}")
    print(f"رکوردهای جدید آپشن: {new_rows_option:,}")
    print(f"رکوردهای Carry-Forward: {carried_rows_count:,}")
    print(f"استراتژی‌های ساخته‌شده: {strategies_built:,}")
    print(f"خطاها: {errors:,}")
    print(f"درخواست‌های API: {requests_count:,}")
    print(f"زمان اجرا: {elapsed:.1f} ثانیه")
    print("=" * 75)
    print()
    print(f"تاریخ شمسی: {gregorian_to_jalali(pd.Timestamp.today())}")
    print()
    if symbols_by_last_date:
        for date_jalali, syms in sorted(symbols_by_last_date.items(), reverse=True):
            names = "، ".join(syms)
            print(f"نمادهای {names} - آخرین داده: {date_jalali}")
    else:
        print("هیچ نمادی با موفقیت به‌روز نشد")
    print()
    if symbols_not_extracted:
        print(f"نمادهای استخراج‌نشده: {'، '.join(symbols_not_extracted)}")
    else:
        print("همه نمادها با موفقیت به‌روز شدند")
    print()
    print("پایان")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TSE Options Suite (Supabase)")
    parser.add_argument("--rebuild", action="store_true", help="بازسازی کامل")
    parser.add_argument("--reset", action="store_true", help="حذف دیتابیس قدیمی و ساخت از صفر")
    parser.add_argument("--db", type=str, default=None, help="مسیر دیتابیس (نادیده گرفته می‌شود)")
    print()
    print("4) به‌روزرسانی JSON...")
    
    # --- خطوط جدید برای ردیابی ---
    print("DEBUG 1: قبل از فراخوانی export_to_json")
    try:
        json_file = export_to_json(client, run_id)
        print(f"DEBUG 2: export_to_json اجرا شد. مسیر فایل: {json_file}")
        
        import os
        if os.path.exists(json_file):
            print("DEBUG 3: ✅ فایل JSON با موفقیت در دیسک ساخته شد!")
        else:
            print("DEBUG 3: ❌ هشدار: تابع اجرا شد اما فایل در دیسک وجود ندارد!")
    except Exception as e:
        print(f"DEBUG 3: ❌ خطای شدید در ساخت JSON: {e}")
    # -----------------------------
    
    elapsed = time.time() - start_time
    # ... (ادامه کد به‌همان شکل قبل)
    args = parser.parse_args()
    if args.rebuild:
        REBUILD = True
    if args.reset:
        RESET_DB = True
    if args.db:
        DB_FILE = args.db
    if args.json:
        JSON_FILE = args.json
    main()
