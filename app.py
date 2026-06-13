from __future__ import annotations

import html
import json
import math
import os
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import urllib3


APP_DIR = Path(__file__).resolve().parent
TAIPEI = ZoneInfo("Asia/Taipei")

TWSE_MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_WARRANTS = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
TWSE_SYMBOLS = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
TPEX_WARRANTS = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap37_O"
YUANTA_WARRANT_DATA = "https://www.warrantwin.com.tw/eyuanta/ws/GetWarData.ashx"
YUANTA_QUOTE = "https://www.warrantwin.com.tw/eyuanta/ws/Quote.ashx"
KGI_SERVICE = "https://warrant.kgi.com/EDWebService/WSInterfaceSwap.asmx/GetService"

HEADERS = {"User-Agent": "Mozilla/5.0 warrant-watch streamlit app"}
APP_VERSION = "W1.0.4a"
BASIC_DATA_TTL_SECONDS = 60 * 60 * 12
CALCULATION_STATE_VERSION = "clear-calculation-inputs-v2"
CALCULATION_FIELDS = ("testSpot", "targetPrice", "simulatedPrice", "impliedSpot")
SUPABASE_TABLE_DEFAULT = "warrant_watch_lists"
SUPABASE_PROFILE_DEFAULT = "default"
SUPABASE_HEADERS_BASE = {"User-Agent": "warrant-watch-streamlit/1.0"}
VENDOR_SSL_VERIFY = False
VOLATILITY_ALERT_POINTS = 1.0

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class WarrantError(Exception):
    pass


def to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def roc_date_to_iso(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) != 7:
        return ""
    year = int(text[:3]) + 1911
    return f"{year}-{text[3:5]}-{text[5:7]}"


def compact_date_to_iso(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) != 8:
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def iso_to_compact(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def today_iso() -> str:
    return datetime.now(TAIPEI).date().isoformat()


def today_compact() -> str:
    return iso_to_compact(today_iso())


def deployed_commit() -> str:
    for key in ("STREAMLIT_GIT_COMMIT", "GIT_COMMIT", "COMMIT_SHA", "SOURCE_VERSION"):
        value = os.environ.get(key)
        if value:
            return value[:7]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def app_version_text() -> str:
    commit = deployed_commit()
    return f"版本 {APP_VERSION}" + (f" · {commit}" if commit else "")


def secret_value(env_key: str, nested_key: str | None = None, default: str = "") -> str:
    value = os.environ.get(env_key)
    if value:
        return value
    try:
        if env_key in st.secrets:
            return str(st.secrets[env_key])
        supabase = st.secrets.get("supabase", {})
        if nested_key and nested_key in supabase:
            return str(supabase[nested_key])
    except Exception:
        return default
    return default


def supabase_config() -> dict[str, str] | None:
    url = secret_value("SUPABASE_URL", "url").rstrip("/")
    key = (
        secret_value("SUPABASE_SERVICE_KEY", "service_key")
        or secret_value("SUPABASE_KEY", "key")
        or secret_value("SUPABASE_ANON_KEY", "anon_key")
    )
    if not url or not key:
        return None
    table = secret_value("SUPABASE_TABLE", "table", SUPABASE_TABLE_DEFAULT) or SUPABASE_TABLE_DEFAULT
    profile_id = secret_value("SUPABASE_PROFILE_ID", "profile_id", SUPABASE_PROFILE_DEFAULT) or SUPABASE_PROFILE_DEFAULT
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise WarrantError("Supabase table name 只能包含英文、數字與底線")
    return {"url": url, "key": key, "table": table, "profile_id": profile_id}


def storage_label() -> str:
    return "Supabase" if supabase_config() else "Supabase 未設定"


def supabase_headers(config: dict[str, str], *, prefer: str = "") -> dict[str, str]:
    headers = {
        **SUPABASE_HEADERS_BASE,
        "apikey": config["key"],
        "Authorization": f"Bearer {config['key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_endpoint(config: dict[str, str]) -> str:
    return f"{config['url']}/rest/v1/{config['table']}"


def response_json(response: requests.Response, source: str) -> Any:
    text = response.text.strip()
    if not text:
        raise WarrantError(f"{source} 回傳空資料")
    try:
        return response.json()
    except ValueError as error:
        preview = text[:120].replace("\n", " ")
        raise WarrantError(f"{source} 回傳格式不是 JSON：{preview}") from error


def read_supabase_items() -> list[dict[str, Any]]:
    config = supabase_config()
    if not config:
        raise WarrantError("Supabase 尚未設定，請先設定 SUPABASE_URL / SUPABASE_KEY")
    response = requests.get(
        supabase_endpoint(config),
        headers=supabase_headers(config),
        params={
            "profile_id": f"eq.{config['profile_id']}",
            "select": "items",
            "limit": "1",
        },
        timeout=12,
    )
    response.raise_for_status()
    rows = response_json(response, "Supabase 清單")
    if not rows:
        return []
    items = rows[0].get("items") or []
    return items if isinstance(items, list) else []


def write_supabase_items(items: list[dict[str, Any]]) -> bool:
    config = supabase_config()
    if not config:
        raise WarrantError("Supabase 尚未設定，無法儲存清單")
    payload = {
        "profile_id": config["profile_id"],
        "items": [item_without_calculations(item) for item in items],
        "updated_at": datetime.now(TAIPEI).isoformat(),
    }
    response = requests.post(
        supabase_endpoint(config),
        headers=supabase_headers(config, prefer="resolution=merge-duplicates,return=minimal"),
        params={"on_conflict": "profile_id"},
        json=payload,
        timeout=12,
    )
    response.raise_for_status()
    return True


def split_book(value: Any) -> list[float]:
    numbers: list[float] = []
    for item in str(value or "").split("_"):
        parsed = to_number(item)
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    verify: bool = True,
) -> Any:
    response = requests.request(method, url, headers=HEADERS, data=data, timeout=15, verify=verify)
    response.raise_for_status()
    return response_json(response, url)


def normalize_quote(raw: dict[str, Any], requested_market: str, requested_code: str) -> dict[str, Any] | None:
    items = raw.get("msgArray") or []
    item = items[0] if items else None
    if not item or (not item.get("c") and not item.get("n")):
        return None

    asks = split_book(item.get("a"))
    bids = split_book(item.get("b"))
    last = to_number(item.get("z"))
    recent = to_number(item.get("pz"))
    previous_close = to_number(item.get("y"))
    open_price = to_number(item.get("o"))
    high = to_number(item.get("h"))
    low = to_number(item.get("l"))
    best_ask = asks[0] if asks else None
    best_bid = bids[0] if bids else None
    mid = (best_ask + best_bid) / 2 if best_ask is not None and best_bid is not None else None
    price = first_number(last, recent, mid, best_bid, best_ask, previous_close)

    query_time = raw.get("queryTime") or {}
    return {
        "market": item.get("ex") or requested_market,
        "code": item.get("c") or requested_code,
        "name": item.get("n") or "",
        "fullName": item.get("nf") or "",
        "relatedCode": item.get("rch") or "",
        "relatedName": item.get("rn") or "",
        "date": item.get("d") or query_time.get("sysDate") or "",
        "time": item.get("t") or query_time.get("sysTime") or "",
        "last": last,
        "recent": recent,
        "price": price,
        "previousClose": previous_close,
        "open": open_price,
        "high": high,
        "low": low,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "mid": mid,
        "bidSize": [part for part in str(item.get("g") or "").split("_") if part],
        "askSize": [part for part in str(item.get("f") or "").split("_") if part],
        "rawStatus": raw.get("rtmessage") or "",
    }


def first_number(*values: Any) -> float | None:
    for value in values:
        parsed = to_number(value)
        if parsed is not None:
            return parsed
    return None


@st.cache_data(ttl=BASIC_DATA_TTL_SECONDS, show_spinner=False)
def fetch_twse_warrants() -> list[dict[str, Any]]:
    return fetch_json(TWSE_WARRANTS)


@st.cache_data(ttl=BASIC_DATA_TTL_SECONDS, show_spinner=False)
def fetch_tpex_warrants() -> list[dict[str, Any]]:
    return fetch_json(TPEX_WARRANTS)


@st.cache_data(ttl=BASIC_DATA_TTL_SECONDS, show_spinner=False)
def fetch_twse_symbols() -> list[dict[str, Any]]:
    return fetch_json(TWSE_SYMBOLS)


@st.cache_data(ttl=15, show_spinner=False)
def fetch_yuanta_warrant(code: str) -> dict[str, Any] | None:
    columns = [
        "FLD_WAR_ID",
        "FLD_WAR_NM",
        "FLD_WAR_TYPE",
        "FLD_UND_ID",
        "FLD_UND_NM",
        "FLD_OBJ_TXN_PRICE",
        "FLD_WAR_TXN_PRICE",
        "FLD_WAR_BUY_PRICE",
        "FLD_WAR_SELL_PRICE",
        "FLD_ISSUE_AGT_ID",
        "FLD_YUANTA_IV",
        "FLD_DUR_END",
        "FLD_N_STRIKE_PRC",
        "FLD_N_UND_CONVER",
        "FLD_IV_BUY_PRICE",
        "FLD_IV_SELL_PRICE",
        "FLD_HISTORY_VOLATILITY_3M",
        "FLD_RISK_RATE_FREE",
        "FLD_IV_CLOSE_PRICE",
        "FLD_OBJ_BUY_PRICE",
        "FLD_OBJ_2BUY_PRICE",
        "FLD_OBJ_SELL_PRICE",
        "FLD_OBJ_2SELL_PRICE",
    ]
    payload = {
        "format": "JSON",
        "factor": {
            "columns": columns,
            "condition": [
                {"field": "FLD_WAR_ID", "values": [code]},
                {"field": "FLD_WAR_TYPE", "values": ["1", "2"]},
            ],
            "orderby": {"field": "FLD_WAR_TXN_VOLUME", "sort": "DESC", "agtfirst": "980"},
        },
        "pagination": {"row": "1", "page": "1", "count": "1"},
    }
    headers = {
        **HEADERS,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    response = requests.post(
        YUANTA_WARRANT_DATA,
        headers=headers,
        data={"data": json.dumps(payload, ensure_ascii=False)},
        timeout=15,
        verify=VENDOR_SSL_VERIFY,
    )
    response.raise_for_status()
    data = response_json(response, "元大權證資料")
    result = data.get("result") or []
    return result[0] if result else None


@st.cache_data(ttl=15, show_spinner=False)
def fetch_yuanta_quote_calc(params: dict[str, str]) -> dict[str, Any] | None:
    endpoint = f"{YUANTA_QUOTE}?{urlencode(params)}"
    data = fetch_json(endpoint, verify=VENDOR_SSL_VERIFY)
    calc = data.get("calc") if isinstance(data, dict) else None
    return calc if isinstance(calc, dict) else None


def fetch_yuanta_quote_price(params: dict[str, str]) -> float | None:
    calc = fetch_yuanta_quote_calc(params)
    return to_number((calc or {}).get("PriceTheory"))


@st.cache_data(ttl=15, show_spinner=False)
def fetch_kgi_service(service_id: str, params: dict[str, Any]) -> Any:
    payload = dict(params)
    payload["LocationPathName"] = "/EDWebSite/Views/WarrantCalculator/WarrantCalculatorIframe.aspx"
    response = requests.post(
        KGI_SERVICE,
        headers={
            **HEADERS,
            "Origin": "https://warrant.kgi.com",
            "Referer": "https://warrant.kgi.com/EDWebSite/Views/WarrantCalculator/WarrantCalculatorIframe.aspx",
        },
        data={
            "serviceId": service_id,
            "parametersOfJson": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
        timeout=15,
        verify=VENDOR_SSL_VERIFY,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    namespace = {"kgi": "http://tempuri.org/"}
    result = root.find("kgi:Result", namespace)
    if result is not None and str(result.text).lower() != "true":
        return None
    value = root.find("kgi:ValueOfJson", namespace)
    if value is None or not value.text:
        return None
    return json.loads(value.text)


@st.cache_data(ttl=BASIC_DATA_TTL_SECONDS, show_spinner=False)
def fetch_kgi_warrant_list() -> list[dict[str, Any]]:
    result = fetch_kgi_service("S0600013_NormalWarrantList", {})
    return result if isinstance(result, list) else []


def find_kgi_warrant_entry(code: str) -> dict[str, Any] | None:
    normalized = str(code or "").strip().upper()
    for item in fetch_kgi_warrant_list():
        text = str(item.get("TEXT") or "").upper()
        if text.startswith(f"{normalized} "):
            return item
    return None


def fetch_kgi_warrant(code: str) -> dict[str, Any] | None:
    entry = find_kgi_warrant_entry(code)
    insnbr = int(to_number((entry or {}).get("INSTR_INSNBR")) or 0)
    if not insnbr:
        return None
    result = fetch_kgi_service("S0600013_GetWarrant", {"INSTR_INSNBR": insnbr})
    if isinstance(result, list) and result:
        return result[0]
    return None


def fetch_kgi_underlying_by_warrant(warrant: dict[str, Any] | None) -> dict[str, Any] | None:
    insnbr = int(to_number((warrant or {}).get("INSTR_INSNBR")) or 0)
    if not insnbr:
        return None
    result = fetch_kgi_service("S0600017_GetUnderlyingByWarrant", {"INSTR_INSNBR": insnbr})
    if isinstance(result, list) and result:
        return result[0]
    return None


def choose_kgi_underlying_price(warrant: dict[str, Any] | None, underlying: dict[str, Any] | None) -> float | None:
    if not warrant or not underlying:
        return None
    if warrant.get("INSWRT_STOCKTYPE") == "DI":
        return first_number(underlying.get("DEAL"), 1)
    if warrant.get("INSWRT_CP") == "認售":
        return first_number(underlying.get("ASK1"), underlying.get("DEAL"))
    return first_number(underlying.get("BID1"), underlying.get("DEAL"))


def build_kgi_calc_params_from_warrant(warrant: dict[str, Any], spot: Any) -> dict[str, Any] | None:
    insnbr = int(to_number(warrant.get("INSTR_INSNBR")) or 0)
    vol = to_number(warrant.get("MTM_BID_VOL"))
    spot_value = to_number(spot)
    if not insnbr or vol is None or spot_value is None:
        return None
    return {
        "INSTR_INSNBR": insnbr,
        "PROCESS_DATE": int(today_compact()),
        "VOL": vol,
        "UNDERLYING_PRICE": spot_value,
    }


def build_kgi_calc_params_from_item(item: dict[str, Any], spot: Any) -> dict[str, Any] | None:
    spot_value = to_number(spot)
    if spot_value is None:
        return None
    params = dict(item.get("kgiCalcParams") or {})
    if not params:
        warrant = fetch_kgi_warrant(str(item.get("code") or ""))
        params = build_kgi_calc_params_from_warrant(warrant or {}, spot_value) or {}
    params["UNDERLYING_PRICE"] = spot_value
    if not params.get("INSTR_INSNBR") or params.get("VOL") is None or not params.get("PROCESS_DATE"):
        return None
    return params


def fetch_kgi_theoretical_price(params: dict[str, Any] | None) -> float | None:
    if not params:
        return None
    result = fetch_kgi_service("S0600018_GetTheoreticalPrice", params)
    return to_number(result)


def fetch_kgi_price_for_item(item: dict[str, Any], spot: Any) -> float | None:
    params = build_kgi_calc_params_from_item(item, spot)
    return fetch_kgi_theoretical_price(params)


def choose_volatility(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"value": None, "source": ""}
    yuanta_iv = to_number(row.get("FLD_YUANTA_IV"))
    if row.get("FLD_ISSUE_AGT_ID") == "980" and yuanta_iv and yuanta_iv > 0:
        return {"value": yuanta_iv, "source": "元大造市委買波動率"}

    candidates = [
        ("買價隱波", row.get("FLD_IV_BUY_PRICE")),
        ("賣價隱波", row.get("FLD_IV_SELL_PRICE")),
        ("收盤隱波", row.get("FLD_IV_CLOSE_PRICE")),
        ("三個月歷史波動率", row.get("FLD_HISTORY_VOLATILITY_3M")),
    ]
    for source, raw_value in candidates:
        value = to_number(raw_value)
        if value and value > 0:
            return {"value": value, "source": source}
    return {"value": None, "source": ""}


def yuanta_type_is_put(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return str(row.get("FLD_WAR_TYPE") or "") in {"認售", "2", "PUT", "put"}


def choose_underlying_price(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    if yuanta_type_is_put(row):
        primary = row.get("FLD_OBJ_SELL_PRICE")
        secondary = row.get("FLD_OBJ_2SELL_PRICE")
    else:
        primary = row.get("FLD_OBJ_BUY_PRICE")
        secondary = row.get("FLD_OBJ_2BUY_PRICE")
    return first_number(primary, secondary, row.get("FLD_OBJ_TXN_PRICE"))


def find_underlying(name_or_code: str) -> dict[str, Any] | None:
    if not name_or_code:
        return None
    symbols = fetch_twse_symbols()
    key = str(name_or_code).strip()
    compact_key = key.replace(" ", "")
    for item in symbols:
        if item.get("Code") == key:
            return item
    for item in symbols:
        if item.get("Name") == key:
            return item
    for item in symbols:
        if str(item.get("Name") or "").replace(" ", "") == compact_key:
            return item
    return None


def find_warrant_info(code: str) -> dict[str, Any] | None:
    code = code.strip().upper()
    row = next((item for item in fetch_twse_warrants() if str(item.get("權證代號") or "").upper() == code), None)
    market = "tse"

    if not row:
        row = next((item for item in fetch_tpex_warrants() if str(item.get("權證代號") or "").upper() == code), None)
        market = "otc" if row else "tse"

    if not row:
        return None

    underlying_label = row.get("標的證券/指數") or ""
    underlying = find_underlying(underlying_label) if market == "tse" else None
    shares_per_thousand = to_number(row.get("最新標的履約配發數量(每仟單位權證)"))
    ratio = shares_per_thousand / 1000 if shares_per_thousand is not None else None

    return {
        "code": row.get("權證代號") or code,
        "name": row.get("權證簡稱") or "",
        "warrantType": "put" if row.get("權證類型") == "認售" else "call",
        "category": row.get("類別") or "",
        "quoteStyle": row.get("流動量提供者報價方式") or "",
        "settlement": row.get("結算方式(詳附註編號說明)") or "",
        "underlyingName": underlying_label,
        "underlyingCode": (underlying or {}).get("Code") or "",
        "underlyingMarket": "tse" if underlying else market,
        "warrantMarket": market,
        "strike": to_number(row.get("最新履約價格(元)/履約指數")),
        "ratio": ratio,
        "sharesPerThousand": shares_per_thousand,
        "startDate": roc_date_to_iso(row.get("履約開始日")),
        "lastTradingDate": roc_date_to_iso(row.get("最後交易日")),
        "exerciseEndDate": roc_date_to_iso(row.get("履約截止日")),
        "sourceDate": roc_date_to_iso(row.get("出表日期")),
    }


def info_from_existing_item(code: str, existing: dict[str, Any] | None) -> dict[str, Any] | None:
    if not existing:
        return None

    strike = to_number(existing.get("strike"))
    ratio = to_number(existing.get("ratio"))
    expiry = str(existing.get("expiry") or "").strip()
    if strike is None or ratio is None or not expiry:
        return None

    quote = existing.get("quote") or {}
    underlying_quote = existing.get("underlyingQuote") or {}
    underlying_code = (
        existing.get("underlyingCode")
        or quote.get("relatedCode")
        or underlying_quote.get("code")
        or ""
    )
    underlying_name = (
        existing.get("underlyingName")
        or quote.get("relatedName")
        or underlying_quote.get("name")
        or ""
    )
    warrant_market = quote.get("market") or "tse"
    underlying_market = underlying_quote.get("market") or quote.get("market") or warrant_market

    return {
        "code": existing.get("code") or code,
        "name": existing.get("name") or "",
        "warrantType": existing.get("type") or "call",
        "category": "",
        "quoteStyle": "",
        "settlement": "",
        "underlyingName": underlying_name,
        "underlyingCode": underlying_code,
        "underlyingMarket": underlying_market,
        "warrantMarket": warrant_market,
        "strike": strike,
        "ratio": ratio,
        "sharesPerThousand": ratio * 1000,
        "startDate": "",
        "lastTradingDate": expiry,
        "exerciseEndDate": expiry,
        "sourceDate": "",
    }


def info_from_quote(code: str, quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": quote.get("code") or code,
        "name": quote.get("name") or quote.get("fullName") or "",
        "warrantType": "call",
        "category": "",
        "quoteStyle": "",
        "settlement": "",
        "underlyingName": quote.get("relatedName") or "",
        "underlyingCode": quote.get("relatedCode") or "",
        "underlyingMarket": quote.get("market") or "tse",
        "warrantMarket": quote.get("market") or "tse",
        "strike": None,
        "ratio": None,
        "sharesPerThousand": None,
        "startDate": "",
        "lastTradingDate": "",
        "exerciseEndDate": "",
        "sourceDate": "",
    }


def apply_yuanta_overrides(info: dict[str, Any], yuanta: dict[str, Any] | None) -> None:
    if not yuanta:
        return
    info["name"] = yuanta.get("FLD_WAR_NM") or info.get("name") or ""
    if yuanta_type_is_put(yuanta):
        info["warrantType"] = "put"
    elif str(yuanta.get("FLD_WAR_TYPE") or "") in {"認購", "1", "CALL", "call"}:
        info["warrantType"] = "call"
    info["underlyingCode"] = yuanta.get("FLD_UND_ID") or info.get("underlyingCode") or ""
    info["underlyingName"] = yuanta.get("FLD_UND_NM") or info.get("underlyingName") or ""
    info["strike"] = first_number(yuanta.get("FLD_N_STRIKE_PRC"), info.get("strike"))
    info["ratio"] = first_number(yuanta.get("FLD_N_UND_CONVER"), info.get("ratio"))
    info["exerciseEndDate"] = compact_date_to_iso(yuanta.get("FLD_DUR_END")) or info.get("exerciseEndDate") or ""


def apply_kgi_overrides(info: dict[str, Any], kgi: dict[str, Any] | None) -> None:
    if not kgi:
        return
    info["name"] = kgi.get("INSTR_NAME") or info.get("name") or ""
    info["warrantType"] = "put" if kgi.get("INSWRT_CP") == "認售" else "call"
    info["underlyingCode"] = kgi.get("UND_INSTR_STKID") or info.get("underlyingCode") or ""
    info["underlyingName"] = kgi.get("UND_INSTR_NAME") or info.get("underlyingName") or ""
    info["strike"] = first_number(kgi.get("INSWRT_STRIKE"), info.get("strike"))
    info["ratio"] = first_number(kgi.get("INSWRT_EXECRATE"), info.get("ratio"))
    info["exerciseEndDate"] = compact_date_to_iso(kgi.get("INSWRT_EXPIRED_DATE")) or info.get("exerciseEndDate") or ""


@st.cache_data(ttl=5, show_spinner=False)
def fetch_quote(market: str, code: str) -> dict[str, Any] | None:
    params = {
        "ex_ch": f"{market}_{code}.tw",
        "json": "1",
        "delay": "0",
        "_": str(int(time.time() * 1000)),
    }
    endpoint = f"{TWSE_MIS}?{urlencode(params)}"
    return normalize_quote(fetch_json(endpoint), market, code)


def fetch_quote_with_fallback(code: str, preferred_market: str) -> dict[str, Any] | None:
    markets = ["tse", "otc"] if preferred_market == "tse" else ["otc", "tse"]
    for market in markets:
        try:
            quote = fetch_quote(market, code)
        except Exception:
            quote = None
        if quote:
            return quote
    return None


def build_yuanta_calc_params(info: dict[str, Any], yuanta: dict[str, Any] | None, spot: Any) -> dict[str, str] | None:
    volatility = choose_volatility(yuanta)
    spot_value = to_number(spot)
    vol_value = to_number(volatility.get("value"))
    if not yuanta or not spot_value or not vol_value:
        return None

    expiry = (
        compact_date_to_iso(yuanta.get("FLD_DUR_END"))
        or info.get("exerciseEndDate")
        or info.get("lastTradingDate")
        or ""
    )
    return {
        "type": "calc",
        "symbol": str(info.get("code") or ""),
        "war_type": "2" if info.get("warrantType") == "put" else "1",
        "conver_rate": str(first_number(yuanta.get("FLD_N_UND_CONVER"), info.get("ratio")) or ""),
        "udly_price": str(spot_value),
        "bid_price": str(first_number(yuanta.get("FLD_WAR_BUY_PRICE")) or ""),
        "ask_price": str(first_number(yuanta.get("FLD_WAR_SELL_PRICE")) or ""),
        "strike_price": str(first_number(yuanta.get("FLD_N_STRIKE_PRC"), info.get("strike")) or ""),
        "hist_vol": str((first_number(yuanta.get("FLD_HISTORY_VOLATILITY_3M"), vol_value) or vol_value) / 100),
        "date_s": iso_to_compact(today_iso()),
        "date_e": iso_to_compact(expiry),
        "ir": str((first_number(yuanta.get("FLD_RISK_RATE_FREE"), 1.5) or 1.5) / 100),
        "iv": str(vol_value / 100),
    }


def build_yuanta_calc_params_from_item(item: dict[str, Any], spot: Any) -> dict[str, str] | None:
    spot_value = to_number(spot)
    if spot_value is None:
        return None

    params = dict(item.get("yuantaCalcParams") or {})
    if not params:
        quote = item.get("quote") or {}
        params = {
            "type": "calc",
            "symbol": str(item.get("code") or ""),
            "war_type": "2" if item.get("type") == "put" else "1",
            "conver_rate": str(to_number(item.get("ratio")) or ""),
            "bid_price": str(to_number(quote.get("bestBid")) or ""),
            "ask_price": str(to_number(quote.get("bestAsk")) or ""),
            "strike_price": str(to_number(item.get("strike")) or ""),
            "hist_vol": str(to_number(item.get("historyVolatility")) or to_number(item.get("volatility")) or ""),
            "date_s": iso_to_compact(today_iso()),
            "date_e": iso_to_compact(item.get("expiry")),
            "ir": str(to_number(item.get("riskFreeRate")) or 0.015),
            "iv": str(to_number(item.get("volatility")) or ""),
        }
    params["udly_price"] = str(spot_value)
    if not params.get("symbol") or not params.get("conver_rate") or not params.get("strike_price") or not params.get("date_e"):
        return None
    return params


def fetch_yuanta_fair_price(info: dict[str, Any], yuanta: dict[str, Any] | None, spot: Any) -> float | None:
    params = build_yuanta_calc_params(info, yuanta, spot)
    if not params:
        return None
    return fetch_yuanta_quote_price(params)


def fetch_yuanta_price_for_item(item: dict[str, Any], spot: Any) -> float | None:
    params = build_yuanta_calc_params_from_item(item, spot)
    if not params:
        return None
    return fetch_yuanta_quote_price(params)


def warrant_issuer(item: dict[str, Any]) -> str:
    text = " ".join(
        str(value or "")
        for value in (
            item.get("issuer"),
            item.get("name"),
            (item.get("quote") or {}).get("name"),
            (item.get("quote") or {}).get("fullName"),
        )
    )
    if "元大" in text:
        return "yuanta"
    if "凱基" in text:
        return "kgi"
    return ""


def issuer_label(issuer: str) -> str:
    if issuer == "yuanta":
        return "元大"
    if issuer == "kgi":
        return "凱基"
    return "未知券商"


def quote_reference(quote: dict[str, Any] | None) -> float | None:
    if not quote:
        return None
    return first_number(
        quote.get("mid"),
        quote.get("recent"),
        quote.get("last"),
        quote.get("bestBid"),
        quote.get("bestAsk"),
        quote.get("price"),
        quote.get("previousClose"),
    )


def quote_from_yuanta(row: dict[str, Any] | None, code: str) -> dict[str, Any] | None:
    if not row:
        return None
    bid = first_number(row.get("FLD_WAR_BUY_PRICE"))
    ask = first_number(row.get("FLD_WAR_SELL_PRICE"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = first_number(row.get("FLD_WAR_TXN_PRICE"), mid, bid, ask)
    return {
        "market": "tse",
        "code": row.get("FLD_WAR_ID") or code,
        "name": row.get("FLD_WAR_NM") or "",
        "fullName": row.get("FLD_WAR_NM") or "",
        "relatedCode": row.get("FLD_UND_ID") or "",
        "relatedName": row.get("FLD_UND_NM") or "",
        "date": today_compact(),
        "time": "",
        "last": to_number(row.get("FLD_WAR_TXN_PRICE")),
        "recent": to_number(row.get("FLD_WAR_TXN_PRICE")),
        "price": price,
        "previousClose": None,
        "open": None,
        "high": None,
        "low": None,
        "bestBid": bid,
        "bestAsk": ask,
        "mid": mid,
        "bidSize": [],
        "askSize": [],
        "rawStatus": "yuanta",
    }


def underlying_quote_from_yuanta(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    bid = first_number(row.get("FLD_OBJ_BUY_PRICE"), row.get("FLD_OBJ_2BUY_PRICE"))
    ask = first_number(row.get("FLD_OBJ_SELL_PRICE"), row.get("FLD_OBJ_2SELL_PRICE"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = first_number(row.get("FLD_OBJ_TXN_PRICE"), mid, bid, ask)
    return {
        "market": "tse",
        "code": row.get("FLD_UND_ID") or "",
        "name": row.get("FLD_UND_NM") or "",
        "fullName": row.get("FLD_UND_NM") or "",
        "relatedCode": "",
        "relatedName": "",
        "date": today_compact(),
        "time": "",
        "last": to_number(row.get("FLD_OBJ_TXN_PRICE")),
        "recent": to_number(row.get("FLD_OBJ_TXN_PRICE")),
        "price": price,
        "previousClose": None,
        "open": None,
        "high": None,
        "low": None,
        "bestBid": bid,
        "bestAsk": ask,
        "mid": mid,
        "bidSize": [],
        "askSize": [],
        "rawStatus": "yuanta",
    }


def quote_from_kgi(row: dict[str, Any] | None, code: str) -> dict[str, Any] | None:
    if not row:
        return None
    bid = first_number(row.get("BID1"), row.get("BID"))
    ask = first_number(row.get("ASK1"), row.get("ASK"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = first_number(row.get("DEAL"), row.get("CLOSE"), mid, bid, ask)
    return {
        "market": "tse",
        "code": row.get("INSTR_STKID") or row.get("INSTR_ID") or code,
        "name": row.get("INSTR_NAME") or "",
        "fullName": row.get("INSTR_NAME") or "",
        "relatedCode": row.get("UND_INSTR_STKID") or "",
        "relatedName": row.get("UND_INSTR_NAME") or "",
        "date": today_compact(),
        "time": "",
        "last": first_number(row.get("DEAL"), row.get("CLOSE")),
        "recent": first_number(row.get("DEAL"), row.get("CLOSE")),
        "price": price,
        "previousClose": None,
        "open": None,
        "high": None,
        "low": None,
        "bestBid": bid,
        "bestAsk": ask,
        "mid": mid,
        "bidSize": [],
        "askSize": [],
        "rawStatus": "kgi",
    }


def underlying_quote_from_kgi(
    warrant: dict[str, Any] | None,
    underlying: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not warrant and not underlying:
        return None
    source = underlying or {}
    bid = first_number(source.get("BID1"), source.get("BID"))
    ask = first_number(source.get("ASK1"), source.get("ASK"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = first_number(source.get("DEAL"), source.get("CLOSE"), mid, bid, ask)
    return {
        "market": "tse",
        "code": (warrant or {}).get("UND_INSTR_STKID") or source.get("INSTR_STKID") or "",
        "name": (warrant or {}).get("UND_INSTR_NAME") or source.get("INSTR_NAME") or "",
        "fullName": (warrant or {}).get("UND_INSTR_NAME") or source.get("INSTR_NAME") or "",
        "relatedCode": "",
        "relatedName": "",
        "date": today_compact(),
        "time": "",
        "last": first_number(source.get("DEAL"), source.get("CLOSE")),
        "recent": first_number(source.get("DEAL"), source.get("CLOSE")),
        "price": price,
        "previousClose": None,
        "open": None,
        "high": None,
        "low": None,
        "bestBid": bid,
        "bestAsk": ask,
        "mid": mid,
        "bidSize": [],
        "askSize": [],
        "rawStatus": "kgi",
    }


def implied_spot_from_price(item: dict[str, Any], target_price: Any) -> float | None:
    target = to_number(target_price)
    strike = to_number(item.get("strike"))
    spot = first_number(item.get("spot"), strike)
    if target is None or target < 0 or not strike or not spot:
        return None

    is_put = item.get("type") == "put"
    low = 0.01
    high = max(strike, spot, 1) * 2

    def price_at(value: float) -> float:
        return fair_price_for_spot(item, value) or 0

    if is_put:
        while price_at(low) < target and low > 0.000001:
            low /= 2
        while price_at(high) > target and high < strike * 20:
            high *= 2
        if price_at(low) < target or price_at(high) > target:
            return None
        for _ in range(18):
            mid = (low + high) / 2
            if price_at(mid) > target:
                low = mid
            else:
                high = mid
        return (low + high) / 2

    while price_at(high) < target and high < strike * 20:
        high *= 2
    if price_at(low) > target or price_at(high) < target:
        return None
    for _ in range(18):
        mid = (low + high) / 2
        if price_at(mid) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def load_warrant(code: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = str(code or "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]+", normalized):
        raise WarrantError("請輸入有效權證代號")
    if re.fullmatch(r"\d{4}", normalized):
        raise WarrantError("請輸入權證代號，不是股票代號；例如 068562")

    info = info_from_existing_item(normalized, existing) or {
        "code": normalized,
        "name": "",
        "warrantType": "call",
        "underlyingName": "",
        "underlyingCode": "",
        "underlyingMarket": "tse",
        "warrantMarket": "tse",
        "strike": None,
        "ratio": None,
        "lastTradingDate": "",
        "exerciseEndDate": "",
    }
    yuanta: dict[str, Any] | None = None
    kgi_warrant: dict[str, Any] | None = None
    kgi_underlying: dict[str, Any] | None = None
    yuanta_error = ""
    kgi_error = ""
    quote = None

    issuer = warrant_issuer({"issuer": (existing or {}).get("issuer"), "name": info.get("name")})
    if issuer == "kgi" or not issuer:
        try:
            kgi_warrant = fetch_kgi_warrant(normalized)
            if kgi_warrant:
                issuer = "kgi"
                apply_kgi_overrides(info, kgi_warrant)
                kgi_underlying = fetch_kgi_underlying_by_warrant(kgi_warrant)
            elif issuer == "kgi":
                kgi_error = "凱基資料讀不到這檔權證"
        except Exception as error:
            kgi_error = str(error)
            kgi_warrant = None
            kgi_underlying = None

    if issuer == "yuanta" or not issuer:
        try:
            yuanta = fetch_yuanta_warrant(normalized)
            if yuanta:
                issuer = "yuanta"
                apply_yuanta_overrides(info, yuanta)
            elif issuer == "yuanta":
                yuanta_error = "元大資料讀不到這檔權證"
        except Exception as error:
            yuanta_error = str(error)
            yuanta = None

    quote = (
        fetch_quote_with_fallback(normalized, info.get("warrantMarket") or "tse")
        or quote_from_kgi(kgi_warrant, normalized)
        or quote_from_yuanta(yuanta, normalized)
    )
    if not quote:
        details = "；".join(message for message in (yuanta_error, kgi_error) if message)
        raise WarrantError(f"查無權證即時報價{f'：{details}' if details else ''}")

    info = info_from_quote(normalized, quote) | info
    info["warrantMarket"] = quote.get("market") or info.get("warrantMarket") or "tse"
    info["underlyingCode"] = info.get("underlyingCode") or quote.get("relatedCode") or ""
    info["underlyingName"] = info.get("underlyingName") or quote.get("relatedName") or ""

    if issuer == "yuanta":
        if not yuanta:
            yuanta_error = yuanta_error or "元大資料讀不到這檔權證"
    elif issuer == "kgi":
        if not kgi_warrant:
            kgi_error = kgi_error or "凱基資料讀不到這檔權證"

    underlying_quote = underlying_quote_from_kgi(kgi_warrant, kgi_underlying) or underlying_quote_from_yuanta(yuanta)
    if info.get("underlyingCode"):
        underlying_quote = fetch_quote_with_fallback(
            info["underlyingCode"],
            info.get("underlyingMarket") or info.get("warrantMarket") or "tse",
        ) or underlying_quote
    if not underlying_quote:
        raise WarrantError("查無標的即時報價")

    info["underlyingMarket"] = underlying_quote.get("market") or info.get("underlyingMarket")
    info["underlyingName"] = info.get("underlyingName") or underlying_quote.get("name") or ""

    yuanta_volatility = choose_volatility(yuanta)
    if issuer == "kgi":
        volatility = {
            "value": first_number((kgi_warrant or {}).get("MTM_BID_VOL")),
            "source": "凱基委買波動率" if first_number((kgi_warrant or {}).get("MTM_BID_VOL")) else "",
        }
        underlying_price = first_number(
            choose_kgi_underlying_price(kgi_warrant, kgi_underlying),
            quote_reference(underlying_quote),
        )
        risk_free_rate = 1.5
        history_volatility = to_number((kgi_warrant or {}).get("THREE_MONTH_HISTORY_VOLAILITY"))
    else:
        volatility = yuanta_volatility
        underlying_price = first_number(choose_underlying_price(yuanta), quote_reference(underlying_quote))
        risk_free_rate = first_number((yuanta or {}).get("FLD_RISK_RATE_FREE"), 1.5)
        history_volatility = to_number((yuanta or {}).get("FLD_HISTORY_VOLATILITY_3M"))
    pricing = {
        "evaluationDate": today_iso(),
        "underlyingPrice": underlying_price,
        "volatility": volatility.get("value"),
        "volatilitySource": volatility.get("source"),
        "riskFreeRate": risk_free_rate,
        "historyVolatility": history_volatility,
        "expiryDate": info.get("exerciseEndDate") or info.get("lastTradingDate") or "",
    }
    yuanta_calc_params = build_yuanta_calc_params(info, yuanta, underlying_price) if issuer == "yuanta" else None
    fair_from_yuanta = None
    if issuer == "yuanta":
        try:
            fair_from_yuanta = fetch_yuanta_quote_price(yuanta_calc_params) if yuanta_calc_params else None
        except Exception as error:
            yuanta_error = str(error)
    kgi_calc_params: dict[str, Any] = {}
    fair_from_kgi = None
    if issuer == "kgi":
        try:
            kgi_calc_params = build_kgi_calc_params_from_warrant(kgi_warrant or {}, underlying_price) or {}
            fair_from_kgi = fetch_kgi_theoretical_price(kgi_calc_params)
        except Exception as error:
            kgi_error = str(error)
            fair_from_kgi = None

    market_reference = quote_reference(quote)
    spot = first_number(pricing["underlyingPrice"], quote_reference(underlying_quote))
    item = {
        "id": (existing or {}).get("id") or str(uuid.uuid4()),
        "code": info.get("code") or normalized,
        "name": info.get("name") or "",
        "issuer": issuer,
        "type": info.get("warrantType") or "call",
        "expiry": pricing["expiryDate"],
        "strike": info.get("strike"),
        "ratio": info.get("ratio"),
        "underlyingCode": info.get("underlyingCode") or "",
        "underlyingName": info.get("underlyingName") or "",
        "quote": quote,
        "underlyingQuote": underlying_quote,
        "spot": spot,
        "marketReference": market_reference,
        "volatility": (first_number(pricing["volatility"], 45) or 45) / 100,
        "volatilitySource": pricing.get("volatilitySource") or "波動率",
        "historyVolatility": (pricing.get("historyVolatility") / 100) if pricing.get("historyVolatility") else None,
        "riskFreeRate": (first_number(pricing["riskFreeRate"], 1.5) or 1.5) / 100,
        "evaluationDate": pricing.get("evaluationDate") or "",
        "yuantaCalcParams": yuanta_calc_params or {},
        "kgiCalcParams": kgi_calc_params,
        "testSpot": "",
        "targetPrice": "",
        "updatedAt": int(time.time() * 1000),
        "error": "",
    }
    if issuer == "yuanta":
        item["fairPrice"] = fair_from_yuanta
    elif issuer == "kgi":
        item["fairPrice"] = fair_from_kgi
    else:
        item["fairPrice"] = None

    if issuer == "yuanta" and fair_from_yuanta is not None:
        item["fairPriceSource"] = "元大合理價"
    elif issuer == "kgi" and fair_from_kgi is not None:
        item["fairPriceSource"] = "凱基理論價"
    elif issuer == "yuanta":
        item["fairPriceSource"] = "元大合理價抓取失敗"
        item["error"] = yuanta_error or "元大合理價抓取失敗"
    elif issuer == "kgi":
        item["fairPriceSource"] = "凱基理論價抓取失敗"
        item["error"] = kgi_error or "凱基理論價抓取失敗"
    else:
        item["fairPriceSource"] = "不支援券商"
        item["error"] = "目前只支援元大與凱基權證理論價"
    item["simulatedPrice"] = None
    item["impliedSpot"] = None
    return apply_volatility_tracking(item, existing)


def fair_price_for_spot(item: dict[str, Any], spot: Any) -> float | None:
    spot_value = to_number(spot)
    if spot_value is None:
        return None
    issuer = warrant_issuer(item)
    if issuer == "yuanta":
        return fetch_yuanta_price_for_item(item, spot_value)
    if issuer == "kgi":
        return fetch_kgi_price_for_item(item, spot_value)
    return None


def read_cloud_items() -> list[dict[str, Any]]:
    try:
        items = read_supabase_items()
    except Exception as error:
        st.error(f"Supabase 清單讀取失敗：{error}")
        return []
    valid_items: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("code"):
            item.setdefault("id", str(uuid.uuid4()))
            item.setdefault("error", "")
            item.setdefault("volatilityAlerted", False)
            item.setdefault("previousVolatility", None)
            item.setdefault("volatilityChangePoints", None)
            valid_items.append(recalculate_derived_prices(item))
    return valid_items


def clear_item_calculations(item: dict[str, Any]) -> dict[str, Any]:
    for field in CALCULATION_FIELDS:
        item[field] = "" if field in {"testSpot", "targetPrice"} else None
    return item


def item_without_calculations(item: dict[str, Any]) -> dict[str, Any]:
    payload = clear_item_calculations(dict(item))
    payload.pop("pricingVolatility", None)
    return payload


def write_cloud_items(items: list[dict[str, Any]]) -> None:
    write_supabase_items(items)


def recalculate_derived_prices(item: dict[str, Any]) -> dict[str, Any]:
    return clear_item_calculations(item)


def normalize_saved_item(item: dict[str, Any]) -> dict[str, Any] | None:
    code = str(item.get("code") or "").strip().upper()
    if not code:
        return None
    normalized = dict(item)
    normalized["code"] = code
    normalized.setdefault("id", str(uuid.uuid4()))
    normalized.setdefault("error", "")
    normalized.setdefault("type", "call")
    normalized.setdefault("quote", {})
    normalized.setdefault("underlyingQuote", {})
    normalized.setdefault("volatilityAlerted", False)
    normalized.setdefault("previousVolatility", None)
    normalized.setdefault("volatilityChangePoints", None)
    normalized.setdefault("testSpot", normalized.get("spot"))
    normalized.setdefault("targetPrice", normalized.get("marketReference"))
    normalized.setdefault("updatedAt", int(time.time() * 1000))
    return recalculate_derived_prices(normalized)


def parse_import_items(raw_text: str) -> list[dict[str, Any]]:
    text = raw_text.strip()
    if not text:
        raise ValueError("請貼上舊版 localStorage 內容")

    payload = json.loads(text)
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        if isinstance(payload.get("warrant-watch-items-v3"), str):
            payload = json.loads(payload["warrant-watch-items-v3"])
        elif isinstance(payload.get("items"), list):
            payload = payload["items"]
    if not isinstance(payload, list):
        raise ValueError("匯入內容不是權證清單格式")

    imported: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            normalized = normalize_saved_item(item)
            if normalized:
                imported.append(normalized)
    if not imported:
        raise ValueError("沒有讀到任何權證代號")
    return imported


def import_items(raw_text: str, *, replace: bool) -> int:
    imported = parse_import_items(raw_text)
    if replace:
        st.session_state["items"] = imported
    else:
        by_code = {item.get("code"): index for index, item in enumerate(st.session_state["items"])}
        for item in imported:
            found_index = by_code.get(item["code"])
            if found_index is None:
                by_code[item["code"]] = len(st.session_state["items"])
                st.session_state["items"].append(item)
            else:
                st.session_state["items"][found_index] = item
    persist_current_items()
    return len(imported)


def format_number(value: Any, digits: int = 2) -> str:
    parsed = to_number(value)
    if parsed is None:
        return "--"
    return f"{parsed:,.{digits}f}"


def format_input_number(value: Any, digits: int = 2) -> str:
    parsed = to_number(value)
    if parsed is None:
        return ""
    return f"{parsed:.{digits}f}"


def format_calc_number(value: Any, digits: int = 2) -> str:
    parsed = to_number(value)
    if parsed is None:
        return ""
    return f"{parsed:,.{digits}f}"


def numbers_equal(left: Any, right: Any, *, tolerance: float = 1e-9) -> bool:
    left_number = to_number(left)
    right_number = to_number(right)
    if left_number is None or right_number is None:
        return left_number is right_number
    return abs(left_number - right_number) <= tolerance


def type_text(value: str) -> str:
    return "認售" if value == "put" else "認購"


def format_percent_points(value: Any, digits: int = 2) -> str:
    parsed = to_number(value)
    if parsed is None:
        return "--"
    return f"{parsed * 100:,.{digits}f}%"


def format_volatility_change(value: Any) -> str:
    parsed = to_number(value)
    if parsed is None:
        return "--"
    sign = "+" if parsed > 0 else ""
    return f"{sign}{parsed:.2f}pt"


def apply_volatility_tracking(item: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    existing = existing or {}
    current_volatility = to_number(item.get("volatility"))
    previous_volatility = to_number(existing.get("volatility"))
    previous_alerted = bool(existing.get("volatilityAlerted"))
    now = datetime.now(TAIPEI).isoformat()

    item["volatilityAlerted"] = previous_alerted
    item["volatilityAlertThreshold"] = VOLATILITY_ALERT_POINTS
    item["previousVolatility"] = previous_volatility
    item["volatilityChangePoints"] = None
    item["volatilityDirection"] = existing.get("volatilityDirection") or ""
    item["volatilityFirstAlertAt"] = existing.get("volatilityFirstAlertAt") or ""
    item["volatilityLastCheckAt"] = now

    if current_volatility is None or previous_volatility is None:
        return item

    change_points = (current_volatility - previous_volatility) * 100
    item["volatilityChangePoints"] = change_points
    if change_points > 0:
        item["volatilityDirection"] = "up"
    elif change_points < 0:
        item["volatilityDirection"] = "down"
    else:
        item["volatilityDirection"] = "flat"

    if abs(change_points) >= VOLATILITY_ALERT_POINTS:
        item["volatilityAlerted"] = True
        item["volatilityLastAlertAt"] = now
        if not item["volatilityFirstAlertAt"]:
            item["volatilityFirstAlertAt"] = now
    else:
        item["volatilityLastAlertAt"] = existing.get("volatilityLastAlertAt") or ""
    return item


def volatility_tracking_text(item: dict[str, Any]) -> str:
    previous = to_number(item.get("previousVolatility"))
    change = to_number(item.get("volatilityChangePoints"))
    if previous is None or change is None:
        return "尚無前次資料"
    return f"前次 {format_percent_points(previous)} / 變化 {format_volatility_change(change)}"


def time_ago(timestamp: Any) -> str:
    parsed = to_number(timestamp)
    if parsed is None:
        return "未更新"
    seconds = max(0, int((time.time() * 1000 - parsed) / 1000))
    if seconds < 60:
        return f"{seconds} 秒前"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分前"
    return f"{minutes // 60} 小時前"


def latest_update_text(items: list[dict[str, Any]]) -> str:
    latest = max((to_number(item.get("updatedAt")) or 0 for item in items), default=0)
    return time_ago(latest) if latest else "尚未更新"


def safe_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", value)


def warrant_title_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    code = str(item.get("code") or "").strip()
    return f"{name} {code}".strip() if name else code


def warrant_title_html(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    code = str(item.get("code") or "").strip()
    title = warrant_title_text(item)
    if name and code:
        body = (
            f'<span class="warrant-name">{html.escape(name)}</span>'
            f'<span class="warrant-code">{html.escape(code)}</span>'
        )
    else:
        body = f'<span class="warrant-name">{html.escape(title)}</span>'
    return f'<div class="warrant-title" title="{html.escape(title)}">{body}</div>'


def metric_html(label: str, value: Any, *, accent: bool = False) -> str:
    cls = "metric-value accent" if accent else "metric-value"
    return (
        '<div class="metric-box">'
        f'<span class="metric-label">{html.escape(label)}</span>'
        f'<strong class="{cls}">{html.escape(format_number(value))}</strong>'
        "</div>"
    )


def calc_result_html(label: str, value: Any) -> str:
    return (
        '<div class="calc-result">'
        f'<span class="calc-result-label">{html.escape(label)}</span>'
        f'<strong class="calc-result-value">{html.escape(format_calc_number(value))}</strong>'
        "</div>"
    )


def error_note_html(message: Any) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    return f'<div class="card-error-note">{html.escape(text)}</div>'


def detail_line(label: str, value: str) -> None:
    st.markdown(
        f'<div class="detail-line"><span>{html.escape(label)}</span><strong>{html.escape(value or "--")}</strong></div>',
        unsafe_allow_html=True,
    )


def detail_row_html(label: str, value: str) -> str:
    return (
        '<div class="detail-line">'
        f'<span>{html.escape(label)}</span>'
        f'<strong>{html.escape(value or "--")}</strong>'
        "</div>"
    )


def detail_html(item: dict[str, Any]) -> str:
    quote = item.get("quote") or {}
    underlying_quote = item.get("underlyingQuote") or {}
    star_class = "native-detail-popover volatility-lit" if item.get("volatilityAlerted") else "native-detail-popover"
    star_text = "★" if item.get("volatilityAlerted") else "☆"
    star_title = "隱波已變動，點擊查看追蹤" if item.get("volatilityAlerted") else "權證細節"
    rows = [
        ("類型", type_text(item.get("type") or "call")),
        ("標的", f"{item.get('underlyingCode') or ''} {item.get('underlyingName') or ''}".strip()),
        ("履約價", format_number(item.get("strike"))),
        ("換股比例", format_number(item.get("ratio"), 4)),
        ("到期日", item.get("expiry") or "--"),
        ("評價日", item.get("evaluationDate") or "--"),
        ("合理價來源", item.get("fairPriceSource") or "--"),
        ("波動率", f"{item.get('volatilitySource') or '波動率'} {format_number((to_number(item.get('volatility')) or 0) * 100)}%"),
        ("隱波追蹤", volatility_tracking_text(item)),
        ("利率", f"{format_number((to_number(item.get('riskFreeRate')) or 0) * 100)}%"),
        ("委買/委賣", f"{format_number(quote.get('bestBid'))} / {format_number(quote.get('bestAsk'))}"),
        ("標的市場", f"{underlying_quote.get('market') or '--'}"),
    ]
    body = "".join(detail_row_html(label, value) for label, value in rows)
    return (
        f'<details class="{star_class}">'
        f'<summary title="{html.escape(star_title)}">{star_text}</summary>'
        f'<div class="native-detail-body">{body}</div>'
        "</details>"
    )


def persist_current_items() -> None:
    write_cloud_items(st.session_state["items"])


def clear_realtime_caches() -> None:
    for cached_fetch in (fetch_quote, fetch_yuanta_warrant, fetch_yuanta_quote_price, fetch_kgi_service):
        try:
            cached_fetch.clear()
        except Exception:
            pass


def clear_calculation_inputs() -> None:
    prefixes = ("spot_text_", "target_text_", "mobile_spot_text_", "mobile_target_text_")
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefixes):
            del st.session_state[key]


def reset_calculation_state_once() -> None:
    if st.session_state.get("_calculation_state_version") == CALCULATION_STATE_VERSION:
        return
    clear_calculation_inputs()
    for item in st.session_state.get("items", []):
        clear_item_calculations(item)
    st.session_state["_calculation_state_version"] = CALCULATION_STATE_VERSION


def sync_session_version() -> None:
    if st.session_state.get("_loaded_app_version") == APP_VERSION:
        return
    clear_calculation_inputs()
    st.session_state["_loaded_app_version"] = APP_VERSION


def add_or_update_warrant(code: str) -> None:
    normalized = str(code or "").strip().upper()
    if not normalized:
        st.warning("請先輸入權證代號")
        return
    existing_index = next((i for i, item in enumerate(st.session_state["items"]) if item.get("code") == normalized), -1)
    existing = st.session_state["items"][existing_index] if existing_index >= 0 else None
    with st.spinner("正在抓取權證資料..."):
        item = load_warrant(normalized, existing)
    if existing_index >= 0:
        st.session_state["items"][existing_index] = item
    else:
        st.session_state["items"].append(item)
    clear_calculation_inputs()
    persist_current_items()
    st.toast(f"{item['code']} 已儲存")


def refresh_all_prices() -> None:
    if not st.session_state["items"]:
        return
    clear_realtime_caches()
    refreshed: list[dict[str, Any] | None] = [None] * len(st.session_state["items"])
    failed = 0
    progress = st.progress(0, text="更新價格中...")
    max_workers = min(6, max(1, len(st.session_state["items"])))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(load_warrant, item.get("code") or "", dict(item)): index
            for index, item in enumerate(st.session_state["items"])
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            item = st.session_state["items"][index]
            try:
                refreshed[index] = future.result()
            except Exception as error:
                failed += 1
                item["error"] = str(error)
                refreshed[index] = item
            progress.progress(completed / len(st.session_state["items"]), text="更新價格中...")
    progress.empty()
    st.session_state["items"] = [item for item in refreshed if item is not None]
    clear_calculation_inputs()
    persist_current_items()
    st.toast(f"已更新，{failed} 檔暫時抓不到" if failed else "價格已更新")


def move_item(index: int, direction: int) -> None:
    next_index = index + direction
    if next_index < 0 or next_index >= len(st.session_state["items"]):
        return
    item = st.session_state["items"].pop(index)
    st.session_state["items"].insert(next_index, item)
    persist_current_items()
    st.rerun()


def delete_item(index: int) -> None:
    removed = st.session_state["items"].pop(index)
    persist_current_items()
    st.toast(f"{removed.get('code')} 已刪除")
    st.rerun()


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
          --bg: #202124;
          --sidebar: #202124;
          --surface: #292a2d;
          --surface-soft: #303134;
          --surface-input: #202124;
          --ink: #e8eaed;
          --muted: #bdc1c6;
          --faint: #9aa0a6;
          --line: #3c4043;
          --line-strong: #5f6368;
          --accent: #8ab4f8;
          --accent-strong: #aecbfa;
          --green: #81c995;
          --danger: #f28b82;
          --blue-soft: #1f2d3d;
          --blue-line: #3f6ea5;
          --orange-soft: #33281b;
          --orange-line: #b26c1c;
        }
        .stApp { background: var(--bg); color: var(--ink); }
        header[data-testid="stHeader"] {
          background: transparent;
        }
        div[data-testid="stToolbar"] {
          display: none;
        }
        .main .block-container {
          max-width: none;
          padding: 0.72rem 1.05rem 1.35rem;
        }
        section[data-testid="stSidebar"] {
          background: var(--sidebar);
          border-right: 1px solid var(--line);
        }
        section[data-testid="stSidebar"],
        section[data-testid="stSidebar"] * {
          color: var(--ink);
        }
        section[data-testid="stSidebar"] h1 {
          font-size: 1.42rem;
          line-height: 1.1;
          margin-bottom: 0.2rem;
        }
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] [data-testid="stMetricLabel"] {
          color: var(--muted);
        }
        div[data-testid="stTextInput"] input,
        div[data-testid="stNumberInput"] input {
          background: var(--surface-input) !important;
          color: var(--ink) !important;
          border: 1px solid var(--line) !important;
          border-radius: 8px !important;
        }
        .stButton > button,
        button[data-testid="stBaseButton-secondary"],
        button[data-testid="stBaseButton-secondaryFormSubmit"],
        button[data-testid="stPopoverButton"] {
          background: var(--surface-soft);
          color: var(--ink);
          border: 1px solid var(--line);
          min-height: 2rem;
          border-radius: 8px;
          font-weight: 800;
        }
        button[data-testid="stBaseButton-secondaryFormSubmit"] {
          background: #3c78d8;
          color: #ffffff;
          border-color: #4c8bf5;
        }
        .stButton > button:hover,
        button[data-testid="stBaseButton-secondary"]:hover,
        button[data-testid="stBaseButton-secondaryFormSubmit"]:hover,
        button[data-testid="stPopoverButton"]:hover {
          border-color: #9fb0a9;
        }
        .stButton > button:disabled,
        button[data-testid="stBaseButton-secondary"]:disabled,
        button[data-testid="stBaseButton-secondaryFormSubmit"]:disabled {
          background: #252628;
          color: #74777c;
          border-color: var(--line);
        }
        button[data-testid="stPopoverButton"] {
          width: 2rem;
          min-width: 2rem;
          height: 2rem;
          padding: 0 !important;
          color: var(--accent-strong);
          line-height: 1;
        }
        button[data-testid="stPopoverButton"] > div {
          justify-content: center;
          gap: 0;
        }
        button[data-testid="stPopoverButton"] p {
          margin: 0;
          font-size: 1rem;
          line-height: 1;
        }
        button[data-testid="stPopoverButton"] span[data-testid="stIconMaterial"] {
          display: none;
        }
        div[data-testid="stPopoverBody"] {
          background: var(--surface) !important;
          color: var(--ink) !important;
          border: 1px solid var(--line) !important;
          border-radius: 8px !important;
          box-shadow: 0 16px 36px rgba(18, 31, 27, 0.14) !important;
        }
        div[data-testid="stPopoverBody"] * {
          color: var(--ink) !important;
        }
        div[data-testid="stPopoverBody"] .detail-line span {
          color: var(--muted) !important;
        }
        div[data-testid="stVerticalBlock"] { gap: 0.45rem; }
        div[data-testid="stHorizontalBlock"] { gap: 0.5rem; }
        div[class*="st-key-card_"] {
          background: var(--surface);
          border-radius: 8px;
          margin-bottom: 0.68rem;
        }
        div[class*="st-key-card_"] > div {
          border-color: var(--line) !important;
          background: var(--surface) !important;
        }
        div[class*="st-key-mobile_controls"] {
          display: none;
        }
        div[class*="st-key-mobile_watchlist"] {
          display: none;
        }
        div[class*="st-key-desktop_watchlist"] {
          display: block;
        }
        div[class*="st-key-calc_forward_"],
        div[class*="st-key-calc_reverse_"] {
          border-radius: 8px;
          padding: 0.46rem 0.5rem 0.42rem;
          border: 1px solid;
          min-height: 4.65rem;
        }
        div[class*="st-key-calc_forward_"] {
          background: var(--blue-soft);
          border-color: var(--blue-line);
        }
        div[class*="st-key-calc_reverse_"] {
          background: var(--orange-soft);
          border-color: var(--orange-line);
        }
        div[class*="st-key-calc_forward_"] label,
        div[class*="st-key-calc_reverse_"] label {
          height: 1rem;
          min-height: 1rem;
          padding: 0 !important;
          margin: 0 0 0.26rem !important;
          display: flex;
          align-items: center;
        }
        div[class*="st-key-calc_forward_"] label p,
        div[class*="st-key-calc_reverse_"] label p {
          font-size: 0.76rem;
          line-height: 1rem;
          font-weight: 800;
          color: var(--muted) !important;
        }
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextInput"] input {
          min-height: 2.05rem;
          height: 2.05rem;
          padding-top: 0.15rem;
          padding-bottom: 0.15rem;
        }
        .warrant-title {
          display: flex;
          align-items: baseline;
          gap: 0.2rem;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          font-size: 0.91rem;
          line-height: 1.25;
          font-weight: 850;
        }
        .warrant-name {
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .warrant-code {
          flex: 0 0 auto;
          color: var(--faint);
          font-size: 0.82em;
          font-weight: 800;
        }
        .card-header-grid {
          display: grid;
          grid-template-columns: minmax(150px, 1fr) 58px 54px 90px;
          align-items: start;
          gap: 4px 9px;
          min-width: 0;
          min-height: 2.55rem;
          margin-bottom: 0.46rem;
          padding-top: 0.05rem;
        }
        .card-title-cell {
          display: flex;
          align-items: center;
          gap: 5px;
          min-width: 0;
        }
        .native-detail-popover {
          position: relative;
          flex: 0 0 auto;
        }
        .native-detail-popover summary {
          display: grid;
          width: 1.55rem;
          height: 1.55rem;
          place-items: center;
          border: 1px solid var(--line);
          border-radius: 999px;
          background: var(--surface-soft);
          color: var(--accent-strong);
          cursor: pointer;
          font-size: 1rem;
          line-height: 1;
          list-style: none;
        }
        .native-detail-popover summary::-webkit-details-marker {
          display: none;
        }
        .native-detail-popover[open] summary {
          border-color: #9ecfbd;
          background: #26354c;
        }
        .native-detail-popover.volatility-lit summary {
          color: #fdd663;
          border-color: #fbbc04;
          background: rgba(251, 188, 4, 0.14);
          box-shadow: 0 0 0 1px rgba(251, 188, 4, 0.16), 0 0 12px rgba(251, 188, 4, 0.16);
        }
        .native-detail-body {
          position: absolute;
          z-index: 50;
          top: 1.85rem;
          left: 0;
          width: max-content;
          min-width: 270px;
          max-width: 330px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--surface);
          padding: 0.45rem 0.6rem;
          box-shadow: 0 16px 36px rgba(18, 31, 27, 0.14);
        }
        .metric-box {
          min-width: 0;
          text-align: left;
        }
        .card-header-grid .metric-box {
          padding-top: 0.03rem;
        }
        .metric-label {
          display: block;
          color: var(--muted);
          font-size: 0.7rem;
          line-height: 1.02;
          font-weight: 850;
          white-space: nowrap;
        }
        .metric-value {
          display: block;
          color: var(--ink);
          font-size: 0.96rem;
          line-height: 1.08;
          margin-top: 0.12rem;
          white-space: nowrap;
          font-weight: 850;
        }
        .metric-value.accent { color: var(--green); }
        .calc-output {
          min-width: 0;
        }
        .calc-result {
          display: grid;
          grid-template-rows: 1rem 2.05rem;
          gap: 0.26rem;
          min-width: 0;
          text-align: right;
        }
        .calc-result-label {
          color: var(--muted);
          font-size: 0.76rem;
          line-height: 1rem;
          font-weight: 850;
          white-space: nowrap;
        }
        .calc-result-value {
          display: flex;
          align-items: center;
          justify-content: flex-end;
          min-width: 0;
          min-height: 2.05rem;
          color: var(--green);
          font-size: 1rem;
          line-height: 1;
          font-weight: 850;
          white-space: nowrap;
        }
        .card-actions {
          display: grid;
          align-content: center;
          justify-items: center;
          gap: 0.36rem;
          padding-top: 0.12rem;
        }
        div[class*="st-key-card_action_"],
        div[class*="st-key-delete_"] {
          display: flex;
          align-items: center;
          justify-content: center;
          height: 1.45rem;
          margin: 0 !important;
        }
        div[class*="st-key-card_action_"] button,
        div[class*="st-key-delete_"] button {
          width: 1.45rem;
          min-width: 1.45rem;
          min-height: 1.45rem;
          height: 1.45rem;
          padding: 0;
          border-radius: 6px;
          font-size: 0.7rem;
          line-height: 1;
          display: inline-flex;
          align-items: center;
          justify-content: center;
        }
        div[class*="st-key-delete_"] button {
          color: var(--danger);
        }
        .sidebar-update-time {
          color: var(--faint);
          font-size: 0.76rem;
          line-height: 1.2;
          margin-top: 0.2rem;
          margin-bottom: 0.35rem;
          text-align: center;
        }
        .sidebar-status {
          display: flex;
          align-items: center;
          justify-content: flex-start;
          gap: 0.22rem;
          min-height: 2rem;
          min-width: 0;
        }
        .sidebar-status span {
          color: var(--muted);
          font-size: 0.82rem;
          line-height: 1;
          font-weight: 850;
          white-space: nowrap;
        }
        .sidebar-status strong {
          color: var(--ink);
          font-size: 0.82rem;
          line-height: 1;
          font-weight: 850;
          white-space: nowrap;
        }
        .card-error-note {
          width: 36%;
          min-width: 14rem;
          max-width: 22rem;
          margin: 0.48rem 0 0.46rem;
          border-radius: 8px;
          background: rgba(128, 126, 58, 0.48);
          color: var(--ink);
          padding: 0.3rem 0.46rem;
          font-size: 0.68rem;
          line-height: 1.25;
          font-weight: 750;
          overflow-wrap: anywhere;
        }
        .app-version,
        .mobile-version {
          color: var(--faint);
          font-size: 0.68rem;
          line-height: 1.15;
          letter-spacing: 0;
        }
        .app-version {
          margin: -0.1rem 0 0.58rem;
        }
        .mobile-version {
          margin-top: 0.26rem;
          text-align: right;
        }
        .detail-line {
          display: grid;
          grid-template-columns: 5.5rem minmax(0, 1fr);
          gap: 0.5rem;
          border-bottom: 1px solid var(--line);
          padding: 0.18rem 0;
          font-size: 0.82rem;
        }
        .detail-line span {
          color: var(--muted);
          font-weight: 750;
        }
        .detail-line strong {
          color: var(--ink);
          font-weight: 760;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .small-note {
          color: var(--muted);
          font-size: 0.8rem;
          line-height: 1.2;
        }
        .mobile-status {
          display: flex;
          align-items: baseline;
          gap: 0.42rem;
          min-width: 0;
        }
        .mobile-status span {
          color: var(--muted);
          font-size: 0.74rem;
          line-height: 1.05;
          white-space: nowrap;
        }
        .mobile-status strong {
          color: var(--ink);
          font-size: 1.15rem;
          line-height: 1;
          font-weight: 850;
        }
        .mobile-status small {
          color: var(--faint);
          font-size: 0.66rem;
          line-height: 1;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .mobile-app-title {
          color: var(--ink);
          font-size: 1.34rem;
          line-height: 1.05;
          font-weight: 850;
          margin: 0 0 0.5rem 0.12rem;
          white-space: nowrap;
        }
        .mobile-card-header {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 0.32rem 0.45rem;
          margin-bottom: 0.48rem;
        }
        .mobile-title {
          display: flex;
          align-items: center;
          min-width: 0;
          gap: 0.34rem;
          grid-column: 1 / -1;
        }
        .mobile-title .warrant-title {
          font-size: 0.98rem;
          line-height: 1.15;
        }
        .mobile-metrics {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 0.54rem;
          grid-column: 1 / -1;
          padding-right: 0;
        }
        .mobile-calc-output {
          min-width: 0;
        }
        .mobile-calc-output .calc-result-value {
          justify-content: flex-end;
        }
        .empty-state {
          border: 1px dashed var(--line);
          border-radius: 8px;
          background: var(--surface);
          padding: 1.8rem;
          color: var(--muted);
          text-align: center;
        }
        @media (max-width: 900px) {
          div[class*="st-key-desktop_watchlist"] {
            display: none !important;
          }
          div[class*="st-key-mobile_watchlist"] {
            display: block !important;
          }
          header[data-testid="stHeader"] {
            display: none;
          }
          div[data-testid="collapsedControl"] {
            display: none !important;
          }
          .main .block-container {
            padding: 0.5rem 0.45rem 1rem;
          }
          div[class*="st-key-mobile_controls"] {
            display: block;
            margin: 0 0 0.48rem;
            padding: 0.54rem 0.58rem 0.48rem;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--surface);
            position: relative;
          }
          div[class*="st-key-mobile_controls"]::before {
            content: "Warrant Watch!";
            position: absolute;
            left: 0.12rem;
            top: -2.05rem;
            color: var(--ink);
            font-size: 1.34rem;
            line-height: 1.05;
            font-weight: 850;
            white-space: nowrap;
          }
          div[class*="st-key-mobile_controls"] div[data-testid="stVerticalBlock"] {
            gap: 0.2rem;
          }
          div[class*="st-key-mobile_controls"] div[data-testid="stHorizontalBlock"] {
            display: grid !important;
            grid-template-columns: 4.6rem minmax(0, 1fr) 7.2rem !important;
            gap: 0.5rem !important;
            align-items: center !important;
          }
          div[class*="st-key-mobile_controls"] div[data-testid="stHorizontalBlock"] > div {
            width: auto !important;
            min-width: 0 !important;
            flex: none !important;
          }
          div[class*="st-key-mobile_controls"] .stButton > button {
            min-height: 1.9rem;
            height: 1.9rem;
            padding: 0 0.62rem;
            font-size: 0.82rem;
          }
          div[class*="st-key-mobile_controls"] button[data-testid="stPopoverButton"] {
            width: 100%;
            min-width: 0;
            height: 1.9rem;
            min-height: 1.9rem;
            padding: 0 0.52rem !important;
            color: var(--ink);
          }
          div[class*="st-key-mobile_controls"] button[data-testid="stPopoverButton"] p {
            font-size: 0.82rem;
            line-height: 1;
          }
          div[class*="st-key-card_"] {
            margin-bottom: 0.54rem;
          }
          div[class*="st-key-mobile_card_"] {
            margin-bottom: 0.62rem;
            border-radius: 8px;
            background: var(--surface);
          }
          div[class*="st-key-mobile_card_"] > div {
            border: 1px solid var(--line) !important;
            background: var(--surface) !important;
            border-radius: 8px !important;
            padding: 0.58rem 0.56rem 0.62rem !important;
          }
          div[class*="st-key-mobile_card_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] {
            display: grid !important;
            grid-template-columns: minmax(0, 1fr) 1.38rem !important;
            gap: 0.42rem !important;
            align-items: stretch !important;
          }
          div[class*="st-key-mobile_card_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            width: auto !important;
            min-width: 0 !important;
            flex: none !important;
          }
          .native-detail-popover summary {
            width: 1.38rem;
            height: 1.38rem;
            font-size: 0.9rem;
          }
          .metric-label {
            font-size: 0.66rem;
          }
          .metric-value {
            font-size: 0.9rem;
          }
          div[class*="st-key-mobile_calc_row_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] {
            display: grid !important;
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            gap: 0.44rem !important;
            align-items: stretch !important;
          }
          div[class*="st-key-mobile_calc_row_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            width: auto !important;
            min-width: 0 !important;
            flex: none !important;
          }
          div[class*="st-key-mobile_calc_forward_"],
          div[class*="st-key-mobile_calc_reverse_"] {
            border-radius: 8px;
            border: 1px solid;
            min-height: 3.35rem;
            padding: 0.38rem 0.38rem 0.34rem;
          }
          div[class*="st-key-mobile_calc_forward_"] {
            background: var(--blue-soft);
            border-color: var(--blue-line);
          }
          div[class*="st-key-mobile_calc_reverse_"] {
            background: var(--orange-soft);
            border-color: var(--orange-line);
          }
          div[class*="st-key-mobile_calc_forward_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"],
          div[class*="st-key-mobile_calc_reverse_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] {
            display: grid !important;
            grid-template-columns: minmax(4.2rem, 0.95fr) minmax(4.05rem, 1.05fr) !important;
            gap: 0.3rem !important;
            align-items: start !important;
          }
          div[class*="st-key-mobile_calc_forward_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
          div[class*="st-key-mobile_calc_reverse_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            width: auto !important;
            min-width: 0 !important;
            flex: none !important;
          }
          div[class*="st-key-mobile_calc_forward_"] label,
          div[class*="st-key-mobile_calc_reverse_"] label {
            height: 0.88rem;
            min-height: 0.88rem;
            margin-bottom: 0.22rem !important;
            padding: 0 !important;
          }
          div[class*="st-key-mobile_calc_forward_"] label p,
          div[class*="st-key-mobile_calc_reverse_"] label p {
            font-size: 0.76rem;
            line-height: 0.88rem;
            font-weight: 850;
            color: var(--muted) !important;
          }
          div[class*="st-key-mobile_calc_forward_"] input,
          div[class*="st-key-mobile_calc_reverse_"] input {
            min-height: 1.74rem;
            height: 1.74rem;
            padding-left: 0.46rem;
            padding-right: 0.36rem;
            font-size: 0.84rem;
          }
          .calc-result {
            grid-template-rows: 0.88rem 1.74rem;
            gap: 0.08rem;
            align-content: start;
            height: 100%;
          }
          .calc-result-value {
            min-height: 1.74rem;
            font-size: 0.84rem;
          }
          div[class*="st-key-mobile_actions_"] {
            height: 100%;
          }
          div[class*="st-key-mobile_actions_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"] {
            display: grid !important;
            align-content: center !important;
            justify-items: center !important;
            gap: 0.34rem !important;
            min-height: 7.1rem;
          }
          div[class*="st-key-mobile_action_"],
          div[class*="st-key-mobile_delete_"] {
            height: 1.32rem;
            margin: 0 !important;
          }
          div[class*="st-key-mobile_action_"] button,
          div[class*="st-key-mobile_delete_"] button {
            width: 1.32rem;
            min-width: 1.32rem;
            min-height: 1.32rem;
            height: 1.32rem;
            font-size: 0.62rem;
            padding: 0;
            border-radius: 6px;
          }
          div[class*="st-key-mobile_delete_"] button {
            color: var(--danger);
          }
          .card-error-note {
            width: 100%;
            min-width: 0;
            max-width: none;
            margin: 0.42rem 0 0.08rem;
            font-size: 0.66rem;
            padding: 0.3rem 0.4rem;
          }
        }
        @media (min-width: 901px) {
          div[class*="st-key-mobile_controls"] {
            display: none !important;
          }
        }
        @media (max-width: 380px) {
          .metric-label {
            font-size: 0.62rem;
          }
          .metric-value {
            font-size: 0.82rem;
          }
          .mobile-title .warrant-title {
            font-size: 0.9rem;
          }
          div[class*="st-key-mobile_calc_row_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] {
            gap: 0.36rem !important;
          }
          div[class*="st-key-mobile_calc_forward_"],
          div[class*="st-key-mobile_calc_reverse_"] {
            padding: 0.34rem 0.32rem 0.3rem;
          }
          div[class*="st-key-mobile_calc_forward_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"],
          div[class*="st-key-mobile_calc_reverse_"] > div[data-testid="stLayoutWrapper"] > div[data-testid="stHorizontalBlock"] {
            grid-template-columns: minmax(3.75rem, 0.9fr) minmax(3.35rem, 1.1fr) !important;
            gap: 0.2rem !important;
          }
          div[class*="st-key-mobile_calc_forward_"] input,
          div[class*="st-key-mobile_calc_reverse_"] input {
            font-size: 0.78rem;
            padding-left: 0.34rem;
            padding-right: 0.22rem;
          }
          .calc-result-label {
            font-size: 0.66rem;
          }
          .calc-result-value {
            font-size: 0.76rem;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_warrant_card(item: dict[str, Any], index: int) -> None:
    card_id = safe_key(item.get("id") or item.get("code") or str(index))
    with st.container(border=True, key=f"card_{card_id}"):
        card_cols = st.columns([1.0, 0.08], gap="small")

        changed = False
        with card_cols[1]:
            st.markdown('<div class="card-actions">', unsafe_allow_html=True)
            if st.button("▲", key=f"card_action_up_{card_id}", disabled=index == 0, help="上移"):
                move_item(index, -1)
            if st.button("▼", key=f"card_action_down_{card_id}", disabled=index == len(st.session_state["items"]) - 1, help="下移"):
                move_item(index, 1)
            if st.button("×", key=f"delete_{card_id}", help="刪除這檔權證"):
                delete_item(index)
            st.markdown("</div>", unsafe_allow_html=True)

        with card_cols[0]:
            st.markdown(
                '<div class="card-header-grid">'
                '<div class="card-title-cell">'
                f"{warrant_title_html(item)}"
                f"{detail_html(item)}"
                "</div>"
                + metric_html("合理價", item.get("fairPrice"), accent=True)
                + metric_html("報價", item.get("marketReference"))
                + metric_html("現貨股價", item.get("spot"))
                + "</div>",
                unsafe_allow_html=True,
            )

            calc_cols = st.columns(2, gap="small")
            with calc_cols[0]:
                with st.container(key=f"calc_forward_{card_id}"):
                    inner = st.columns([0.95, 1.05], gap="small")
                    spot_key = f"spot_text_{card_id}"
                    if spot_key not in st.session_state:
                        st.session_state[spot_key] = format_input_number(item.get("testSpot"))
                    with inner[0]:
                        test_spot_raw = st.text_input("股價", key=spot_key)
                    test_spot = to_number(test_spot_raw)
                    if test_spot is None:
                        simulated = None
                        if to_number(item.get("testSpot")) is not None or to_number(item.get("simulatedPrice")) is not None:
                            item["testSpot"] = ""
                            item["simulatedPrice"] = None
                            changed = True
                    else:
                        if numbers_equal(test_spot, item.get("testSpot")) and to_number(item.get("simulatedPrice")) is not None:
                            simulated = item.get("simulatedPrice")
                        elif numbers_equal(test_spot, item.get("spot")):
                            simulated = item.get("fairPrice")
                            changed = True
                        else:
                            simulated = fair_price_for_spot(item, test_spot)
                            changed = True
                        if changed:
                            item["testSpot"] = test_spot
                            item["simulatedPrice"] = simulated
                    with inner[1]:
                        st.markdown(
                            '<div class="calc-output">' + calc_result_html("權證價格", simulated) + "</div>",
                            unsafe_allow_html=True,
                        )

            with calc_cols[1]:
                with st.container(key=f"calc_reverse_{card_id}"):
                    inner = st.columns([0.95, 1.05], gap="small")
                    target_key = f"target_text_{card_id}"
                    if target_key not in st.session_state:
                        st.session_state[target_key] = format_input_number(item.get("targetPrice"))
                    with inner[0]:
                        target_price_raw = st.text_input("權證價格", key=target_key)
                    target_price = to_number(target_price_raw)
                    if target_price is None:
                        implied = None
                        if to_number(item.get("targetPrice")) is not None or to_number(item.get("impliedSpot")) is not None:
                            item["targetPrice"] = ""
                            item["impliedSpot"] = None
                            changed = True
                    else:
                        if numbers_equal(target_price, item.get("targetPrice")) and to_number(item.get("impliedSpot")) is not None:
                            implied = item.get("impliedSpot")
                        else:
                            implied = implied_spot_from_price(item, target_price)
                            item["impliedSpot"] = implied
                            item["targetPrice"] = target_price
                            changed = True
                    with inner[1]:
                        st.markdown(
                            '<div class="calc-output">' + calc_result_html("股價", implied) + "</div>",
                            unsafe_allow_html=True,
                        )

            if item.get("error"):
                st.markdown(error_note_html(item["error"]), unsafe_allow_html=True)


def render_mobile_warrant_card(item: dict[str, Any], index: int) -> None:
    card_id = safe_key(item.get("id") or item.get("code") or str(index))
    changed = False
    with st.container(border=False, key=f"mobile_card_{card_id}"):
        card_cols = st.columns([1.0, 0.08], gap="small")

        with card_cols[1]:
            with st.container(key=f"mobile_actions_{card_id}"):
                if st.button("▲", key=f"mobile_action_up_{card_id}", disabled=index == 0, help="上移"):
                    move_item(index, -1)
                if st.button("▼", key=f"mobile_action_down_{card_id}", disabled=index == len(st.session_state["items"]) - 1, help="下移"):
                    move_item(index, 1)
                if st.button("×", key=f"mobile_delete_{card_id}", help="刪除這檔權證"):
                    delete_item(index)

        with card_cols[0]:
            st.markdown(
                '<div class="mobile-card-header">'
                '<div class="mobile-title">'
                f"{warrant_title_html(item)}"
                f"{detail_html(item)}"
                "</div>"
                '<div class="mobile-metrics">'
                + metric_html("合理價", item.get("fairPrice"), accent=True)
                + metric_html("報價", item.get("marketReference"))
                + metric_html("現貨股價", item.get("spot"))
                + "</div>"
                "</div>",
                unsafe_allow_html=True,
            )

            with st.container(key=f"mobile_calc_row_{card_id}"):
                calc_cols = st.columns(2, gap="small")
                with calc_cols[0]:
                    with st.container(key=f"mobile_calc_forward_{card_id}"):
                        inner = st.columns([0.95, 1.05], gap="small")
                        spot_key = f"mobile_spot_text_{card_id}"
                        if spot_key not in st.session_state:
                            st.session_state[spot_key] = format_input_number(item.get("testSpot"))
                        with inner[0]:
                            test_spot_raw = st.text_input("股價", key=spot_key)
                        test_spot = to_number(test_spot_raw)
                        if test_spot is None:
                            simulated = None
                            if to_number(item.get("testSpot")) is not None or to_number(item.get("simulatedPrice")) is not None:
                                item["testSpot"] = ""
                                item["simulatedPrice"] = None
                                changed = True
                        else:
                            if numbers_equal(test_spot, item.get("testSpot")) and to_number(item.get("simulatedPrice")) is not None:
                                simulated = item.get("simulatedPrice")
                            elif numbers_equal(test_spot, item.get("spot")):
                                simulated = item.get("fairPrice")
                                changed = True
                            else:
                                simulated = fair_price_for_spot(item, test_spot)
                                changed = True
                            if changed:
                                item["testSpot"] = test_spot
                                item["simulatedPrice"] = simulated
                        with inner[1]:
                            st.markdown(
                                '<div class="mobile-calc-output">' + calc_result_html("權證價格", simulated) + "</div>",
                                unsafe_allow_html=True,
                            )

                with calc_cols[1]:
                    with st.container(key=f"mobile_calc_reverse_{card_id}"):
                        inner = st.columns([0.95, 1.05], gap="small")
                        target_key = f"mobile_target_text_{card_id}"
                        if target_key not in st.session_state:
                            st.session_state[target_key] = format_input_number(item.get("targetPrice"))
                        with inner[0]:
                            target_price_raw = st.text_input("權證價格", key=target_key)
                        target_price = to_number(target_price_raw)
                        if target_price is None:
                            implied = None
                            if to_number(item.get("targetPrice")) is not None or to_number(item.get("impliedSpot")) is not None:
                                item["targetPrice"] = ""
                                item["impliedSpot"] = None
                                changed = True
                        else:
                            if numbers_equal(target_price, item.get("targetPrice")) and to_number(item.get("impliedSpot")) is not None:
                                implied = item.get("impliedSpot")
                            else:
                                implied = implied_spot_from_price(item, target_price)
                                item["impliedSpot"] = implied
                                item["targetPrice"] = target_price
                                changed = True
                        with inner[1]:
                            st.markdown(
                                '<div class="mobile-calc-output">' + calc_result_html("股價", implied) + "</div>",
                                unsafe_allow_html=True,
                            )

            if item.get("error"):
                st.markdown(error_note_html(item["error"]), unsafe_allow_html=True)


def render_details(item: dict[str, Any]) -> None:
    quote = item.get("quote") or {}
    underlying_quote = item.get("underlyingQuote") or {}
    detail_line("類型", type_text(item.get("type") or "call"))
    detail_line("標的", f"{item.get('underlyingCode') or ''} {item.get('underlyingName') or ''}".strip())
    detail_line("履約價", format_number(item.get("strike")))
    detail_line("換股比例", format_number(item.get("ratio"), 4))
    detail_line("到期日", item.get("expiry") or "--")
    detail_line("評價日", item.get("evaluationDate") or "--")
    detail_line("合理價來源", item.get("fairPriceSource") or "--")
    detail_line("波動率", f"{item.get('volatilitySource') or '波動率'} {format_number((to_number(item.get('volatility')) or 0) * 100)}%")
    detail_line("利率", f"{format_number((to_number(item.get('riskFreeRate')) or 0) * 100)}%")
    detail_line("委買/委賣", f"{format_number(quote.get('bestBid'))} / {format_number(quote.get('bestAsk'))}")
    detail_line("標的市場", f"{underlying_quote.get('market') or '--'}")


def render_desktop_watchlist() -> None:
    for row_start in range(0, len(st.session_state["items"]), 2):
        cols = st.columns(2, gap="small")
        for offset, col in enumerate(cols):
            item_index = row_start + offset
            if item_index >= len(st.session_state["items"]):
                continue
            with col:
                render_warrant_card(st.session_state["items"][item_index], item_index)


def render_mobile_watchlist() -> None:
    for item_index, item in enumerate(st.session_state["items"]):
        render_mobile_warrant_card(item, item_index)


def render_main() -> None:
    if not st.session_state["items"]:
        st.markdown(
            '<div class="empty-state"><strong>還沒有儲存任何權證</strong><br>從左側輸入代號後，系統會自動抓資料。</div>',
            unsafe_allow_html=True,
        )
        return

    with st.container(key="desktop_watchlist"):
        render_desktop_watchlist()
    with st.container(key="mobile_watchlist"):
        render_mobile_watchlist()


def render_mobile_controls() -> None:
    with st.container(key="mobile_controls"):
        control_cols = st.columns([0.26, 0.39, 0.35], gap="small")
        with control_cols[0]:
            with st.popover("新增", use_container_width=True):
                with st.form("add_warrant_form_mobile", clear_on_submit=True):
                    code = st.text_input("權證代號", placeholder="例如 030012", key="mobile_warrant_code").strip().upper()
                    submitted = st.form_submit_button("新增並抓資料", use_container_width=True)
                if submitted:
                    try:
                        add_or_update_warrant(code)
                    except Exception as error:
                        st.error(str(error))
        with control_cols[1]:
            st.markdown(
                '<div class="mobile-status">'
                '<span>已儲存</span>'
                f'<strong>{len(st.session_state["items"])}</strong>'
                f'<small>{html.escape(latest_update_text(st.session_state["items"]))}</small>'
                "</div>",
                unsafe_allow_html=True,
            )
        with control_cols[2]:
            if st.button("更新價格", use_container_width=True, disabled=not st.session_state["items"], key="mobile_refresh_prices"):
                try:
                    refresh_all_prices()
                except Exception as error:
                    st.error(str(error))
        st.markdown(
            f'<div class="mobile-version">{html.escape(app_version_text())} · 儲存 {html.escape(storage_label())}</div>',
            unsafe_allow_html=True,
        )


def render_sidebar() -> None:
    with st.sidebar:
        st.title("Warrant Watch!")
        st.caption(f"評價日期: {today_compact()}")
        st.markdown(
            f'<div class="app-version">{html.escape(app_version_text())}</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"儲存: {storage_label()}")

        with st.form("add_warrant_form", clear_on_submit=True):
            code = st.text_input("權證代號", placeholder="例如 030012").strip().upper()
            submitted = st.form_submit_button("新增並抓資料", use_container_width=True)
        if submitted:
            try:
                add_or_update_warrant(code)
            except Exception as error:
                st.error(str(error))

        status_cols = st.columns([0.42, 0.58], gap="small")
        with status_cols[0]:
            st.markdown(
                '<div class="sidebar-status">'
                f'<span>已儲存 <strong>{len(st.session_state["items"])}</strong> 檔</span>'
                "</div>",
                unsafe_allow_html=True,
            )
        with status_cols[1]:
            if st.button("更新價格", use_container_width=True, disabled=not st.session_state["items"]):
                try:
                    refresh_all_prices()
                except Exception as error:
                    st.error(str(error))
        st.markdown(
            f'<div class="sidebar-update-time">最近更新 {html.escape(latest_update_text(st.session_state["items"]))}</div>',
            unsafe_allow_html=True,
        )


def main() -> None:
    st.set_page_config(page_title="權證合理價", layout="wide", initial_sidebar_state="expanded")
    inject_css()
    if "items" not in st.session_state:
        st.session_state["items"] = read_cloud_items()
    sync_session_version()
    reset_calculation_state_once()
    render_sidebar()
    render_mobile_controls()
    render_main()


if __name__ == "__main__":
    main()
