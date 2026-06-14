from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import http.client
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CN_TZ = dt.timezone(dt.timedelta(hours=8))
MIN_COVERAGE = 0.8
MEMBER_CHANGE_LIMIT = 0.2
DEFAULT_HISTORY_DAYS = 180


class PipelineError(RuntimeError):
    pass


class FetchRequestError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(CN_TZ).replace(microsecond=0).isoformat()


def current_cn_date() -> dt.date:
    return dt.datetime.now(CN_TZ).date()


def parse_iso_date(value: str | None) -> dt.date:
    if not value:
        return current_cn_date()
    return dt.date.fromisoformat(value)


def is_non_trading_date(value: dt.date) -> bool:
    return value.weekday() >= 5


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_text(value: str, limit: int = 220) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def append_log(logs: list[str], message: str) -> None:
    logs.append(message)
    print(message)


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "none", "None", "~"}:
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def strip_yaml_comment(line: str) -> str:
    if not line.strip().startswith("#") and " #" in line:
        return line.split(" #", 1)[0]
    return line


def yaml_lines(text: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for raw in text.splitlines():
        raw = strip_yaml_comment(raw.rstrip())
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        result.append((indent, raw.strip()))
    return result


def is_key_value(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", text))


def parse_simple_yaml(text: str) -> Any:
    lines = yaml_lines(text)

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] < indent:
            return {}, index
        if lines[index][1].startswith("- "):
            return parse_list(index, indent)
        return parse_dict(index, indent)

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent != indent or not content.startswith("- "):
                break
            rest = content[2:].strip()
            index += 1
            if rest == "":
                child, index = parse_block(index, indent + 2)
                items.append(child)
                continue
            if is_key_value(rest):
                key, raw_value = rest.split(":", 1)
                item: dict[str, Any] = {}
                if raw_value.strip():
                    item[key.strip()] = parse_scalar(raw_value)
                else:
                    child, index = parse_block(index, indent + 2)
                    item[key.strip()] = child
                while index < len(lines) and lines[index][0] >= indent + 2:
                    child_indent, child_content = lines[index]
                    if child_indent != indent + 2 or child_content.startswith("- "):
                        break
                    child_map, index = parse_dict(index, indent + 2)
                    if isinstance(child_map, dict):
                        item.update(child_map)
                items.append(item)
            else:
                items.append(parse_scalar(rest))
        return items, index

    def parse_dict(index: int, indent: int) -> tuple[dict[str, Any], int]:
        data: dict[str, Any] = {}
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent != indent or content.startswith("- "):
                break
            if ":" not in content:
                raise ValueError(f"Invalid config line: {content}")
            key, raw_value = content.split(":", 1)
            key = key.strip()
            index += 1
            if raw_value.strip():
                data[key] = parse_scalar(raw_value)
            elif index < len(lines) and lines[index][0] > current_indent:
                child, index = parse_block(index, current_indent + 2)
                data[key] = child
            else:
                data[key] = None
        return data, index

    parsed, final_index = parse_block(0, 0)
    if final_index != len(lines):
        raise ValueError("Could not parse the complete config file")
    return parsed


def load_config(path: Path) -> dict[str, Any]:
    config = parse_simple_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("boards"), list):
        raise ValueError("boards.yml must contain a top-level boards list")
    return config


def normalize_stock_code(value: str) -> str:
    text = str(value).strip().upper()
    if not text:
        raise ValueError("Empty stock code")
    text = text.replace("_", ".")
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", text):
        return f"{text[2:]}.{text[:2]}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text):
        return text
    if re.fullmatch(r"\d{6}", text):
        if text.startswith(("6", "5")):
            return f"{text}.SH"
        if text.startswith(("4", "8", "9")):
            return f"{text}.BJ"
        return f"{text}.SZ"
    raise ValueError(f"Invalid stock code: {value}")


def code_to_secid(code: str) -> str:
    symbol, exchange = normalize_stock_code(code).split(".")
    market = "1" if exchange == "SH" else "0"
    return f"{market}.{symbol}"


def parse_member_item(item: Any, source: str) -> dict[str, Any]:
    if isinstance(item, str):
        return {"code": normalize_stock_code(item), "name": "", "source": source}
    if isinstance(item, dict):
        code = normalize_stock_code(str(item.get("code") or item.get("symbol") or ""))
        return {
            "code": code,
            "name": str(item.get("name") or ""),
            "source": source,
            "note": str(item.get("note") or ""),
        }
    raise ValueError(f"Invalid stock member item: {item!r}")


def enabled_boards(config: dict[str, Any]) -> list[dict[str, Any]]:
    boards = []
    for raw in config.get("boards", []):
        if not isinstance(raw, dict):
            continue
        if raw.get("enabled") is False:
            continue
        board_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or board_id).strip()
        if not board_id or not name:
            raise ValueError("Every enabled board must have id and name")
        provider = raw.get("provider_board") if isinstance(raw.get("provider_board"), dict) else {}
        boards.append(
            {
                "id": board_id,
                "name": name,
                "enabled": True,
                "priority": int(raw.get("priority") or 100),
                "note": str(raw.get("note") or ""),
                "provider_board": provider or {},
                "include": raw.get("include") or [],
                "exclude": raw.get("exclude") or [],
                "custom_members": raw.get("custom_members") or [],
            }
        )
    return sorted(boards, key=lambda item: (item["priority"], item["name"]))


@dataclass
class FetchResult:
    ok: bool
    members: list[dict[str, Any]]
    source_board_change_pct: float | None = None
    error: str | None = None


class EastmoneyClient:
    quote_fields = "f2,f3,f6,f12,f13,f14,f20,f21"
    kline_fields1 = "f1,f2,f3,f4,f5,f6"
    kline_fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    def __init__(self, timeout: int = 20, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = max(1, retries)
        self.logs: list[str] = []
        self.eastmoney_stock_history_failures = 0
        self.eastmoney_stock_history_circuit_open = False

    def request_json(self, url: str, params: dict[str, Any], source: str, operation: str, target: str) -> dict[str, Any]:
        text = self.request_text(url, params, source, operation, target)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            message = f"{source} {operation} {target} JSONDecodeError: {exc}; body={compact_text(text)}"
            append_log(self.logs, message)
            raise FetchRequestError(message) from exc

    def request_text(self, url: str, params: dict[str, Any], source: str, operation: str, target: str) -> str:
        query = urllib.parse.urlencode(params)
        last_error: Exception | None = None
        request_url = f"{url}?{query}" if query else url
        append_log(self.logs, f"FETCH start source={source} operation={operation} target={compact_text(target, 120)}")
        for attempt in range(self.retries):
            request = urllib.request.Request(
                request_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AShareBoardHeat/1.0)",
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    append_log(
                        self.logs,
                        f"FETCH ok source={source} operation={operation} target={compact_text(target, 120)} "
                        f"status={response.status} bytes={len(body)} attempt={attempt + 1}",
                    )
                    return body
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = exc
                message = (
                    f"FETCH fail source={source} operation={operation} target={compact_text(target, 120)} "
                    f"status={exc.code} error=HTTPError body={compact_text(body)} attempt={attempt + 1}"
                )
                append_log(self.logs, message)
                if attempt + 1 >= self.retries:
                    raise FetchRequestError(message) from exc
                time.sleep(0.8 * (attempt + 1))
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                last_error = exc
                message = (
                    f"FETCH fail source={source} operation={operation} target={compact_text(target, 120)} "
                    f"status=NA error={type(exc).__name__}: {compact_text(str(exc))} attempt={attempt + 1}"
                )
                append_log(self.logs, message)
                if attempt + 1 >= self.retries:
                    raise FetchRequestError(message) from exc
                time.sleep(0.8 * (attempt + 1))
        raise FetchRequestError(str(last_error) if last_error else "request failed")

    def fetch_board_members(self, provider: dict[str, Any]) -> FetchResult:
        code = str(provider.get("code") or "").strip().upper()
        if not code:
            return FetchResult(ok=True, members=[])
        try:
            payload = self.request_json(
                "https://push2.eastmoney.com/api/qt/clist/get",
                {
                    "pn": 1,
                    "pz": 500,
                    "po": 1,
                    "np": 1,
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f3",
                    "fs": f"b:{code}",
                    "fields": self.quote_fields,
                },
                "eastmoney",
                "board_members",
                code,
            )
            rows = payload.get("data", {}).get("diff") or []
            members = []
            for row in rows:
                parsed = parse_eastmoney_quote_row(row)
                if parsed:
                    members.append(
                        {
                            "code": parsed["code"],
                            "name": parsed.get("name", ""),
                            "source": "provider",
                            "market_cap": parsed.get("float_market_cap") or parsed.get("total_market_cap") or 0,
                        }
                    )
            return FetchResult(ok=True, members=members)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(ok=False, members=[], error=str(exc))

    def fetch_board_latest_change(self, provider: dict[str, Any]) -> float | None:
        code = str(provider.get("code") or "").strip().upper()
        if not code:
            return None
        try:
            payload = self.request_json(
                "https://push2.eastmoney.com/api/qt/stock/get",
                {
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2,
                    "invt": 2,
                    "secid": f"90.{code}",
                    "fields": "f3,f12,f14",
                },
                "eastmoney",
                "board_latest_change",
                code,
            )
            return to_float(payload.get("data", {}).get("f3"))
        except Exception:
            return None

    def fetch_quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        quotes: dict[str, dict[str, Any]] = {}
        for chunk in chunks(codes, 80):
            secids = ",".join(code_to_secid(code) for code in chunk)
            try:
                payload = self.request_json(
                    "https://push2.eastmoney.com/api/qt/ulist.np/get",
                    {
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                        "fltt": 2,
                        "invt": 2,
                        "secids": secids,
                        "fields": self.quote_fields,
                    },
                    "eastmoney",
                    "stock_quotes",
                    ",".join(chunk),
                )
                for row in payload.get("data", {}).get("diff") or []:
                    parsed = parse_eastmoney_quote_row(row)
                    if parsed:
                        parsed["data_source"] = "eastmoney"
                        quotes[parsed["code"]] = parsed
            except Exception as exc:  # noqa: BLE001
                append_log(self.logs, f"FALLBACK source=tencent operation=stock_quotes target={','.join(chunk)} reason={compact_text(str(exc))}")
            missing = [code for code in chunk if code not in quotes]
            if missing:
                quotes.update(self.fetch_tencent_quotes(missing))
        return quotes

    def fetch_stock_klines(self, code: str, limit: int) -> list[dict[str, Any]]:
        try:
            rows = self.fetch_tencent_stock_klines(code, limit)
            if rows:
                return rows
            append_log(self.logs, f"FALLBACK source=eastmoney operation=stock_history target={code} reason=tencent returned empty rows")
        except Exception as exc:  # noqa: BLE001
            append_log(self.logs, f"FALLBACK source=eastmoney operation=stock_history target={code} reason={compact_text(str(exc))}")

        if self.eastmoney_stock_history_circuit_open:
            append_log(self.logs, f"SKIP source=eastmoney operation=stock_history target={code} reason=circuit_open")
            return []

        try:
            payload = self.request_json(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                {
                    "secid": code_to_secid(code),
                    "fields1": self.kline_fields1,
                    "fields2": self.kline_fields2,
                    "klt": 101,
                    "fqt": 1,
                    "end": "20500101",
                    "lmt": limit,
                },
                "eastmoney",
                "stock_history",
                code,
            )
            rows = payload.get("data", {}).get("klines") or []
            parsed_rows = []
            for row in rows:
                parsed = parse_kline_row(row)
                if parsed:
                    parsed["data_source"] = "eastmoney"
                    parsed_rows.append(parsed)
            if parsed_rows:
                self.eastmoney_stock_history_failures = 0
                return parsed_rows
            self.record_eastmoney_stock_history_failure(code, "empty rows")
        except Exception as exc:  # noqa: BLE001
            self.record_eastmoney_stock_history_failure(code, str(exc))
        return []

    def record_eastmoney_stock_history_failure(self, code: str, reason: str) -> None:
        self.eastmoney_stock_history_failures += 1
        append_log(
            self.logs,
            f"FETCH fail source=eastmoney operation=stock_history target={code} "
            f"consecutive_failures={self.eastmoney_stock_history_failures} reason={compact_text(reason)}",
        )
        if self.eastmoney_stock_history_failures >= 3:
            self.eastmoney_stock_history_circuit_open = True
            append_log(self.logs, "CIRCUIT open source=eastmoney operation=stock_history threshold=3")

    def fetch_board_klines(self, provider: dict[str, Any], limit: int) -> dict[str, float]:
        code = str(provider.get("code") or "").strip().upper()
        if not code:
            return {}
        try:
            payload = self.request_json(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                {
                    "secid": f"90.{code}",
                    "fields1": self.kline_fields1,
                    "fields2": self.kline_fields2,
                    "klt": 101,
                    "fqt": 1,
                    "end": "20500101",
                    "lmt": limit,
                },
                "eastmoney",
                "board_history",
                code,
            )
            rows = payload.get("data", {}).get("klines") or []
            result: dict[str, float] = {}
            for row in rows:
                parsed = parse_kline_row(row)
                if parsed and parsed.get("change_pct") is not None:
                    result[parsed["date"]] = parsed["change_pct"]
            return result
        except Exception:
            return {}

    def fetch_tencent_quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        if not codes:
            return {}
        symbols = ",".join(to_tencent_symbol(code) for code in codes)
        try:
            raw_text = self.request_text("https://qt.gtimg.cn/q=" + urllib.parse.quote(symbols, safe=","), {}, "tencent", "stock_quotes", ",".join(codes))
        except Exception:
            return {}
        quotes: dict[str, dict[str, Any]] = {}
        pattern = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)";')
        for match in pattern.finditer(raw_text):
            parsed = parse_tencent_quote(match.group(1), match.group(2))
            if parsed:
                parsed["data_source"] = "tencent"
                quotes[parsed["code"]] = parsed
        return quotes

    def fetch_tencent_stock_klines(self, code: str, limit: int) -> list[dict[str, Any]]:
        symbol = to_tencent_symbol(code)
        payload = self.request_json(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            {"param": f"{symbol},day,,,{limit},qfq"},
            "tencent",
            "stock_history",
            code,
        )
        data = payload.get("data", {}).get(symbol, {})
        rows = data.get("qfqday") or data.get("day") or []
        parsed: list[dict[str, Any]] = []
        previous_close: float | None = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            close = to_float(row[2])
            if close is None:
                continue
            if previous_close and previous_close > 0:
                change_pct = ((close - previous_close) / previous_close) * 100
            else:
                change_pct = to_float(row[8] if len(row) > 8 else None) or 0
            parsed.append(
                {
                    "date": str(row[0]),
                    "open": to_float(row[1]),
                    "close": close,
                    "high": to_float(row[3]),
                    "low": to_float(row[4]),
                    "turnover_amount": to_float(row[5]) or 0,
                    "change_pct": change_pct,
                    "data_source": "tencent",
                }
            )
            previous_close = close
        return parsed[-limit:]


class FixtureClient:
    def __init__(self) -> None:
        self.start = dt.date(2025, 1, 2)
        self.logs: list[str] = []

    def fetch_board_members(self, provider: dict[str, Any]) -> FetchResult:
        code = str(provider.get("code") or "")
        if code == "BK1036":
            members = [
                {"code": "688981.SH", "name": "中芯国际", "source": "provider"},
                {"code": "002371.SZ", "name": "北方华创", "source": "provider"},
                {"code": "300666.SZ", "name": "江丰电子", "source": "provider"},
            ]
        elif code == "BK1137":
            members = [
                {"code": "300308.SZ", "name": "中际旭创", "source": "provider"},
                {"code": "603019.SH", "name": "中科曙光", "source": "provider"},
                {"code": "000977.SZ", "name": "浪潮信息", "source": "provider"},
            ]
        else:
            members = []
        return FetchResult(ok=True, members=members)

    def fetch_board_latest_change(self, provider: dict[str, Any]) -> float | None:
        return 1.23 if provider.get("code") else None

    def fetch_quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        return {
            code: {
                "code": code,
                "name": code,
                "price": 10 + index,
                "change_pct": (index % 5 - 2) * 0.7,
                "turnover_amount": 100_000_000 + index * 10_000_000,
                "total_market_cap": 30_000_000_000 + index * 1_000_000_000,
                "float_market_cap": 20_000_000_000 + index * 800_000_000,
            }
            for index, code in enumerate(codes)
        }

    def fetch_stock_klines(self, code: str, limit: int) -> list[dict[str, Any]]:
        seed = sum(ord(ch) for ch in code) % 13
        rows = []
        day = self.start
        while len(rows) < limit:
            if day.weekday() < 5:
                index = len(rows)
                wave = math.sin((index + seed) / 7) * 1.6
                drift = ((seed % 5) - 2) * 0.03
                change = round(wave + drift, 3)
                rows.append(
                    {
                        "date": day.isoformat(),
                        "close": round(10 + index * 0.03 + seed, 3),
                        "change_pct": change,
                        "turnover_amount": round((80_000_000 + seed * 4_000_000) * (1 + abs(change) / 10), 2),
                        "data_source": "fixture",
                    }
                )
            day += dt.timedelta(days=1)
        return rows[-limit:]

    def fetch_board_klines(self, provider: dict[str, Any], limit: int) -> dict[str, float]:
        rows = self.fetch_stock_klines(str(provider.get("code") or "BK0000"), limit)
        return {row["date"]: row["change_pct"] for row in rows}


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def parse_eastmoney_quote_row(row: dict[str, Any]) -> dict[str, Any] | None:
    code = row.get("f12")
    market = row.get("f13")
    if code is None:
        return None
    exchange = "SH" if str(market) == "1" else "BJ" if str(code).startswith(("4", "8", "9")) else "SZ"
    normalized = normalize_stock_code(f"{code}.{exchange}")
    return {
        "code": normalized,
        "name": str(row.get("f14") or ""),
        "price": to_float(row.get("f2")),
        "change_pct": to_float(row.get("f3")),
        "turnover_amount": to_float(row.get("f6")) or 0,
        "total_market_cap": to_float(row.get("f20")) or 0,
        "float_market_cap": to_float(row.get("f21")) or 0,
    }


def to_tencent_symbol(code: str) -> str:
    symbol, exchange = normalize_stock_code(code).split(".")
    prefix = "sh" if exchange == "SH" else "bj" if exchange == "BJ" else "sz"
    return f"{prefix}{symbol}"


def parse_tencent_quote(raw_symbol: str, payload: str) -> dict[str, Any] | None:
    fields = payload.split("~")
    if len(fields) < 4:
        return None
    symbol = raw_symbol[2:]
    exchange = "SH" if raw_symbol.startswith("sh") else "BJ" if raw_symbol.startswith("bj") else "SZ"
    code = normalize_stock_code(f"{symbol}.{exchange}")
    change_pct = to_float(fields[32] if len(fields) > 32 else None)
    price = to_float(fields[3] if len(fields) > 3 else None)
    turnover_amount = to_float(fields[37] if len(fields) > 37 else None)
    total_market_cap = to_float(fields[45] if len(fields) > 45 else None)
    float_market_cap = to_float(fields[46] if len(fields) > 46 else None)
    return {
        "code": code,
        "name": fields[1] if len(fields) > 1 else "",
        "price": price,
        "change_pct": change_pct,
        "turnover_amount": (turnover_amount or 0) * 10_000,
        "total_market_cap": (total_market_cap or 0) * 100_000_000,
        "float_market_cap": (float_market_cap or 0) * 100_000_000,
    }


def parse_kline_row(row: str) -> dict[str, Any] | None:
    fields = str(row).split(",")
    if len(fields) < 7:
        return None
    change_pct = to_float(fields[8] if len(fields) > 8 else None)
    if change_pct is None:
        return None
    return {
        "date": fields[0],
        "open": to_float(fields[1]),
        "close": to_float(fields[2]),
        "high": to_float(fields[3]),
        "low": to_float(fields[4]),
        "volume": to_float(fields[5]) or 0,
        "turnover_amount": to_float(fields[6]) or 0,
        "change_pct": change_pct,
    }


def quote_to_daily_row(quote: dict[str, Any]) -> dict[str, Any] | None:
    change_pct = to_float(quote.get("change_pct"))
    if change_pct is None:
        return None
    return {
        "date": dt.datetime.now(CN_TZ).date().isoformat(),
        "open": None,
        "close": to_float(quote.get("price")),
        "high": None,
        "low": None,
        "turnover_amount": to_float(quote.get("turnover_amount")) or 0,
        "change_pct": change_pct,
        "data_source": quote.get("data_source") or "latest_quote",
    }


def merge_board_members(
    board: dict[str, Any],
    provider_members: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merged: dict[str, dict[str, Any]] = {}
    for member in provider_members:
        code = normalize_stock_code(member["code"])
        merged[code] = {**member, "code": code, "source": member.get("source") or "provider"}
    for item in board.get("include", []):
        member = parse_member_item(item, "include")
        merged[member["code"]] = {**merged.get(member["code"], {}), **member}
    for item in board.get("custom_members", []):
        member = parse_member_item(item, "custom")
        merged[member["code"]] = {**merged.get(member["code"], {}), **member}
    excluded = []
    for item in board.get("exclude", []):
        member = parse_member_item(item, "exclude")
        existing = merged.pop(member["code"], None)
        excluded.append({**member, "was_present": existing is not None})
    return sorted(merged.values(), key=lambda item: item["code"]), excluded


def board_is_custom(board: dict[str, Any]) -> bool:
    provider = board.get("provider_board") or {}
    return str(provider.get("type") or "").strip().lower() == "custom"


def board_has_manual_members(board: dict[str, Any]) -> bool:
    return bool(board.get("include") or board.get("custom_members"))


def fetch_members_with_cache(
    board: dict[str, Any],
    client: Any,
    member_cache: dict[str, Any],
    run_errors: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    provider = board.get("provider_board") or {}
    provider_members: list[dict[str, Any]] = []
    provider_meta = {"source": provider.get("source") or "custom", "stale": False, "error": None}
    if board_is_custom(board):
        provider_meta["source"] = "custom"
    elif provider.get("code"):
        append_log(getattr(client, "logs", []), f"BOARD {board['name']} requesting standard members source={provider.get('source') or 'eastmoney'} code={provider.get('code')}")
        result = client.fetch_board_members(provider)
        if result.ok:
            provider_members = result.members
            member_cache[board["id"]] = {
                "updated_at": now_iso(),
                "provider_board": provider,
                "members": provider_members,
            }
        else:
            cached = member_cache.get(board["id"], {})
            provider_members = cached.get("members") or []
            provider_meta["stale"] = True
            provider_meta["error"] = result.error or "provider fetch failed"
            run_errors.append(f"{board['name']} 成分股抓取失败: {provider_meta['error']}")
            if not provider_members and not board_has_manual_members(board):
                raise PipelineError(f"{board['name']} has no provider members, no cache, and no include/custom_members")
    members, excluded = merge_board_members(board, provider_members)
    if not members:
        raise PipelineError(f"{board['name']} resolved to an empty member list")
    return members, excluded, provider_meta


def member_source_type(source: Any) -> str:
    raw = str(source or "").strip()
    return {
        "provider": "provider_board",
        "include": "include",
        "custom": "custom_members",
    }.get(raw, raw)


def stock_change_5d(rows: list[dict[str, Any]], index: int) -> float | None:
    changes = [
        to_float(row.get("change_pct"))
        for row in rows[max(0, index - 4) : index + 1]
        if to_float(row.get("change_pct")) is not None
    ]
    return rounded(compound_change([float(change) for change in changes if change is not None]))


def stock_turnover_vs_20d(rows: list[dict[str, Any]], index: int) -> float | None:
    current = to_float(rows[index].get("turnover_amount"))
    if current is None or current <= 0:
        return None
    previous = [
        to_float(row.get("turnover_amount")) or 0
        for row in rows[max(0, index - 20) : index]
        if (to_float(row.get("turnover_amount")) or 0) > 0
    ]
    if not previous:
        return None
    return rounded_ratio(current / statistics.fmean(previous))


def build_member_snapshot(
    board: dict[str, Any],
    members: list[dict[str, Any]],
    excluded_members: list[dict[str, Any]],
    invalid_members: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    rows_by_code: dict[str, list[dict[str, Any]]],
    row_index_by_code_date: dict[tuple[str, str], int],
    date: str,
) -> dict[str, Any]:
    snapshot_members = []
    for member in members:
        code = member["code"]
        rows = rows_by_code.get(code) or []
        index = row_index_by_code_date.get((code, date))
        row = rows[index] if index is not None else None
        quote = quotes.get(code, {})
        day_change = to_float(row.get("change_pct")) if row else None
        turnover_amount = to_float(row.get("turnover_amount")) if row else None
        snapshot_members.append(
            {
                "code": code,
                "name": quote.get("name") or member.get("name") or "",
                "day_change_pct": rounded(day_change),
                "change_5d_pct": stock_change_5d(rows, index) if index is not None else None,
                "turnover_amount": round(turnover_amount, 2) if turnover_amount is not None else None,
                "turnover_vs_20d": stock_turnover_vs_20d(rows, index) if index is not None else None,
                "data_source": (row or {}).get("data_source") or quote.get("data_source") or "",
                "source": member_source_type(member.get("source")),
                "member_source": member_source_type(member.get("source")),
                "quote_status": "ok" if day_change is not None else "no_data",
            }
        )
    return {
        "date": date,
        "board_id": board["id"],
        "board_name": board["name"],
        "members": snapshot_members,
        "excluded_members": excluded_members,
        "invalid_members": invalid_members,
    }


def aggregate_board_history(
    board: dict[str, Any],
    members: list[dict[str, Any]],
    excluded_members: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    kline_map: dict[str, list[dict[str, Any]]],
    source_board_changes: dict[str, float],
    history_days: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    member_count = len(members)
    by_date: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    rows_by_code: dict[str, list[dict[str, Any]]] = {}
    row_index_by_code_date: dict[tuple[str, str], int] = {}
    invalid_members = []
    for member in members:
        rows = sorted(kline_map.get(member["code"]) or [], key=lambda item: item.get("date") or "")
        rows_by_code[member["code"]] = rows
        if not rows:
            invalid_members.append({"code": member["code"], "name": member.get("name") or "", "reason": "no_quote"})
            continue
        for index, row in enumerate(rows):
            row_index_by_code_date[(member["code"], row["date"])] = index
            by_date.setdefault(row["date"], []).append((member, row))
    dates = sorted(by_date)[-history_days:]
    records: list[dict[str, Any]] = []
    snapshots_by_date: dict[str, dict[str, Any]] = {}
    for date in dates:
        rows = by_date[date]
        valid = []
        for member, row in rows:
            pct = to_float(row.get("change_pct"))
            if pct is None:
                continue
            quote = quotes.get(member["code"], {})
            cap = quote.get("float_market_cap") or quote.get("total_market_cap") or member.get("market_cap") or 0
            valid.append(
                {
                    "member": member,
                    "change_pct": pct,
                    "turnover_amount": to_float(row.get("turnover_amount")) or 0,
                    "market_cap": to_float(cap) or 0,
                }
            )
        valid_count = len(valid)
        coverage = valid_count / member_count if member_count else 0
        equal_weight_change_pct = statistics.fmean(item["change_pct"] for item in valid) if valid else None
        weighted_denominator = sum(item["market_cap"] for item in valid if item["market_cap"] > 0)
        weighted_change_pct = (
            sum(item["change_pct"] * item["market_cap"] for item in valid if item["market_cap"] > 0) / weighted_denominator
            if weighted_denominator > 0
            else equal_weight_change_pct
        )
        advance_ratio = sum(1 for item in valid if item["change_pct"] > 0) / valid_count if valid_count else None
        turnover_amount = sum(item["turnover_amount"] for item in valid)
        records.append(
            {
                "date": date,
                "board_id": board["id"],
                "board_name": board["name"],
                "priority": board["priority"],
                "note": board["note"],
                "member_count": member_count,
                "valid_quote_count": valid_count,
                "invalid_quote_count": member_count - valid_count,
                "coverage": round(coverage, 4),
                "equal_weight_change_pct": round(equal_weight_change_pct, 4) if equal_weight_change_pct is not None else None,
                "weighted_change_pct": round(weighted_change_pct, 4) if weighted_change_pct is not None else None,
                "advance_ratio": round(advance_ratio, 4) if advance_ratio is not None else None,
                "turnover_amount": round(turnover_amount, 2),
                "turnover_vs_20d": None,
                "source_board_change_pct": source_board_changes.get(date),
                "error_flags": [],
                "status": [],
                "eligible_for_signal": coverage >= MIN_COVERAGE,
            }
        )
        snapshots_by_date[date] = build_member_snapshot(
            board,
            members,
            excluded_members,
            invalid_members,
            quotes,
            rows_by_code,
            row_index_by_code_date,
            date,
        )
    return records, snapshots_by_date


def merge_history(existing: list[dict[str, Any]], incoming: list[dict[str, Any]], max_days: int) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for record in existing:
        if record.get("date") and record.get("board_id"):
            merged[(record["date"], record["board_id"])] = record
    for record in incoming:
        if record.get("date") and record.get("board_id"):
            merged[(record["date"], record["board_id"])] = record
    dates = sorted({date for date, _board_id in merged})[-max_days:]
    keep_dates = set(dates)
    return sorted(
        [record for (date, _board_id), record in merged.items() if date in keep_dates],
        key=lambda item: (item["date"], item.get("priority", 100), item["board_name"]),
    )


def compound_change(changes: list[float]) -> float | None:
    if not changes:
        return None
    value = 1.0
    for change in changes:
        value *= 1 + change / 100
    return (value - 1) * 100


def range_position(value: float, values: list[float]) -> float | None:
    if not values:
        return None
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return 0.5
    return (value - low) / (high - low)


def enrich_history(records: list[dict[str, Any]], previous_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_latest_by_board: dict[str, dict[str, Any]] = {}
    for record in previous_history:
        board_id = record.get("board_id")
        if board_id and (
            board_id not in previous_latest_by_board or record.get("date", "") > previous_latest_by_board[board_id].get("date", "")
        ):
            previous_latest_by_board[board_id] = record

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["board_id"], []).append(record)

    enriched: list[dict[str, Any]] = []
    for board_id, board_records in grouped.items():
        board_records = sorted(board_records, key=lambda item: item["date"])
        latest_board_date = board_records[-1]["date"] if board_records else None
        synthetic_index = 100.0
        changes: list[float] = []
        index_values: list[float] = []
        turnovers: list[float] = []
        for index, record in enumerate(board_records):
            change = record.get("equal_weight_change_pct")
            if isinstance(change, (int, float)):
                synthetic_index *= 1 + float(change) / 100
                changes.append(float(change))
            else:
                changes.append(0.0)
            index_values.append(synthetic_index)
            prior_turnovers = turnovers[-20:]
            turnover_amount = to_float(record.get("turnover_amount")) or 0
            turnover_avg_20 = statistics.fmean(prior_turnovers) if prior_turnovers else None
            turnover_vs_20d = turnover_amount / turnover_avg_20 if turnover_avg_20 and turnover_avg_20 > 0 else None
            turnovers.append(turnover_amount)

            record["synthetic_index"] = round(synthetic_index, 4)
            record["change_5d_pct"] = rounded(compound_change(changes[max(0, index - 4) : index + 1]))
            record["change_20d_pct"] = rounded(compound_change(changes[max(0, index - 19) : index + 1]))
            record["change_60d_pct"] = rounded(compound_change(changes[max(0, index - 59) : index + 1]))
            record["position_20d"] = rounded_ratio(range_position(synthetic_index, index_values[max(0, index - 19) : index + 1]))
            record["position_60d"] = rounded_ratio(range_position(synthetic_index, index_values[max(0, index - 59) : index + 1]))
            record["turnover_vs_20d"] = rounded_ratio(turnover_vs_20d)
            record["consecutive_up_days"] = consecutive_days(changes[: index + 1], direction=1)
            record["consecutive_down_days"] = consecutive_days(changes[: index + 1], direction=-1)

            flags = list(record.get("error_flags") or [])
            if record.get("coverage", 0) < MIN_COVERAGE:
                flags.append("low_coverage")
            previous_latest = previous_latest_by_board.get(board_id)
            if previous_latest and record.get("date") == latest_board_date and previous_latest.get("date") != record.get("date"):
                previous_count = to_float(previous_latest.get("member_count"))
                current_count = to_float(record.get("member_count"))
                if previous_count and current_count and abs(current_count - previous_count) / previous_count > MEMBER_CHANGE_LIMIT:
                    flags.append("member_count_changed_over_20pct")
            record["error_flags"] = sorted(set(flags))
            record["eligible_for_signal"] = record.get("coverage", 0) >= MIN_COVERAGE and "member_count_changed_over_20pct" not in flags
            record["status"] = classify_status(record)
            enriched.append(record)
    return sorted(enriched, key=lambda item: (item["date"], item.get("priority", 100), item["board_name"]))


def rounded(value: float | None) -> float | None:
    return round(value, 4) if value is not None and math.isfinite(value) else None


def rounded_ratio(value: float | None) -> float | None:
    return round(value, 4) if value is not None and math.isfinite(value) else None


def consecutive_days(changes: list[float], direction: int) -> int:
    count = 0
    for change in reversed(changes):
        if direction > 0 and change > 0:
            count += 1
        elif direction < 0 and change < 0:
            count += 1
        else:
            break
    return count


def classify_status(record: dict[str, Any]) -> list[str]:
    if not record.get("eligible_for_signal"):
        return []
    statuses: list[str] = []
    change = record.get("equal_weight_change_pct") or 0
    change_5d = record.get("change_5d_pct") or 0
    position_20d = record.get("position_20d")
    position_60d = record.get("position_60d")
    advance_ratio = record.get("advance_ratio") or 0
    turnover_vs_20d = record.get("turnover_vs_20d") or 0
    up_days = record.get("consecutive_up_days") or 0
    down_days = record.get("consecutive_down_days") or 0

    if change_5d >= 3 and (position_20d or 0) >= 0.7 and up_days >= 2 and advance_ratio >= 0.55:
        statuses.append("强势延续")
    if (change_5d >= 8 or (position_60d is not None and position_60d >= 0.9)) and up_days >= 3 and turnover_vs_20d >= 1.35:
        statuses.append("过热警戒")
    if down_days >= 2 or (change_5d <= -3 and (position_20d is not None and position_20d <= 0.45)):
        statuses.append("调整中")
    if position_60d is not None and position_60d <= 0.25 and (change > 0 or change_5d > -2):
        statuses.append("低位观察")
    if position_60d is not None and position_60d <= 0.35 and change >= 0.5 and advance_ratio >= 0.55 and turnover_vs_20d >= 1.05:
        statuses.append("高低切换候选")
    return statuses


def latest_records(history: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    dates = sorted({record["date"] for record in history})
    if not dates:
        return None, []
    latest_date = dates[-1]
    return latest_date, sorted(
        [record for record in history if record["date"] == latest_date],
        key=lambda item: (item.get("priority", 100), item["board_name"]),
    )


def build_rankings(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def compact(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "board_id": record["board_id"],
            "board_name": record["board_name"],
            "equal_weight_change_pct": record.get("equal_weight_change_pct"),
            "change_5d_pct": record.get("change_5d_pct"),
            "position_60d": record.get("position_60d"),
            "turnover_vs_20d": record.get("turnover_vs_20d"),
            "status": record.get("status") or [],
        }

    return {
        "strong": [compact(item) for item in sorted(records, key=lambda r: (r.get("change_5d_pct") or -999), reverse=True) if "强势延续" in item.get("status", [])],
        "overheat": [compact(item) for item in sorted(records, key=lambda r: (r.get("turnover_vs_20d") or 0), reverse=True) if "过热警戒" in item.get("status", [])],
        "low": [compact(item) for item in sorted(records, key=lambda r: (r.get("position_60d") if r.get("position_60d") is not None else 9)) if "低位观察" in item.get("status", [])],
        "rotation": [compact(item) for item in sorted(records, key=lambda r: (r.get("equal_weight_change_pct") or -999), reverse=True) if "高低切换候选" in item.get("status", [])],
    }


def build_latest_payload(
    history: list[dict[str, Any]],
    run_time: str,
    source: dict[str, Any],
    run_errors: list[str],
    update_failed: bool,
    previous_latest: dict[str, Any] | None,
    fetch_logs: list[str] | None = None,
    status_override: str | None = None,
    message_override: str | None = None,
) -> dict[str, Any]:
    data_date, records = latest_records(history)
    status = "ok"
    if update_failed:
        status = "failed"
    elif run_errors:
        status = "partial"
    if status_override:
        status = status_override
    if update_failed and previous_latest and previous_latest.get("data_time"):
        data_date = previous_latest.get("data_time")
    coverage_values = [record.get("coverage", 0) for record in records]
    if message_override:
        message = message_override
    elif status == "ok":
        message = "数据已更新"
    elif status == "partial":
        message = "数据已部分更新，存在数据源异常"
    elif status == "non_trading_day":
        message = "今日非交易日，当前展示上一交易日数据"
    elif status == "stale_ok":
        message = "今日行情尚未更新，当前展示上一交易日数据"
    elif data_date:
        message = "数据源异常，当前展示上一交易日数据"
    else:
        message = "数据源异常，暂无有效数据"
    return {
        "data_time": data_date,
        "run_time": run_time,
        "source": source,
        "status": status,
        "message": message,
        "coverage": {
            "overall": rounded_ratio(statistics.fmean(coverage_values)) if coverage_values else None,
            "by_board": {record["board_id"]: record.get("coverage") for record in records},
        },
        "error_flags": sorted(set(run_errors + [flag for record in records for flag in (record.get("error_flags") or [])])),
        "fetch_logs": (fetch_logs or [])[-300:],
        "boards": records,
        "rankings": build_rankings(records),
    }


def write_history_csv(path: Path, records: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    fields = [
        "date",
        "board_id",
        "board_name",
        "member_count",
        "valid_quote_count",
        "coverage",
        "equal_weight_change_pct",
        "weighted_change_pct",
        "advance_ratio",
        "turnover_amount",
        "turnover_vs_20d",
        "source_board_change_pct",
        "change_5d_pct",
        "change_20d_pct",
        "change_60d_pct",
        "position_20d",
        "position_60d",
        "consecutive_up_days",
        "consecutive_down_days",
        "eligible_for_signal",
        "status",
        "error_flags",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {field: record.get(field) for field in fields}
            row["status"] = "|".join(record.get("status") or [])
            row["error_flags"] = "|".join(record.get("error_flags") or [])
            writer.writerow(row)


def pct_text(value: Any, digits: int = 2) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:+.{digits}f}%"


def ratio_text(value: Any, digits: int = 0) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    return f"{parsed * 100:.{digits}f}%"


def multiple_text(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:.2f}x"


def money_text(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "-"
    if abs(parsed) >= 100_000_000:
        return f"{parsed / 100_000_000:.1f}亿"
    if abs(parsed) >= 10_000:
        return f"{parsed / 10_000:.1f}万"
    return f"{parsed:.0f}"


def sparkline(records: list[dict[str, Any]], board_id: str) -> str:
    values = [
        record.get("synthetic_index")
        for record in records
        if record.get("board_id") == board_id and isinstance(record.get("synthetic_index"), (int, float))
    ][-60:]
    if len(values) < 2:
        return '<span class="muted">-</span>'
    low = min(values)
    high = max(values)
    width = 140
    height = 42
    points = []
    for index, value in enumerate(values):
        x = index / (len(values) - 1) * width
        y = height - ((value - low) / (high - low or 1)) * height
        points.append(f"{x:.1f},{y:.1f}")
    last = values[-1]
    first = values[0]
    stroke = "#b42318" if last >= first else "#027a48"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="trend">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="2.2" points="{" ".join(points)}" />'
        "</svg>"
    )


def status_badges(statuses: list[str], flags: list[str]) -> str:
    if not statuses and flags:
        return '<span class="badge warn">质量跳过</span>'
    if not statuses:
        return '<span class="badge neutral">观察</span>'
    return "".join(f'<span class="badge">{html.escape(status)}</span>' for status in statuses)


MEMBER_DETAIL_SCRIPT = r"""
<script>
(() => {
  const DATA_TIME = __DATA_TIME__;
  const state = { payload: null, loading: null };
  const asNumber = (value) => (value === null || value === undefined || value === "" ? NaN : Number(value));
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[char]));
  const pct = (value) => {
    const number = asNumber(value);
    return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${number.toFixed(2)}%` : "-";
  };
  const pctClass = (value) => {
    const number = asNumber(value);
    if (!Number.isFinite(number) || Math.abs(number) < 0.0001) return "";
    return number > 0 ? "rise" : "fall";
  };
  const money = (value) => {
    const number = asNumber(value);
    if (!Number.isFinite(number)) return "-";
    if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(1)}亿`;
    if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
    return number.toFixed(0);
  };
  const multiple = (value) => {
    const number = asNumber(value);
    return Number.isFinite(number) ? `${number.toFixed(2)}x` : "-";
  };
  const tileClass = (member) => {
    const change = asNumber(member.day_change_pct);
    if (!Number.isFinite(change) || member.quote_status !== "ok") return "no-data";
    if (change > 0) return "rise";
    if (change < 0) return "fall";
    return "flat";
  };
  const memberTitle = (member) => [
    `${member.name || member.code} ${member.code}`,
    `今日 ${pct(member.day_change_pct)}`,
    `近5日 ${pct(member.change_5d_pct)}`,
    `成交额 ${money(member.turnover_amount)}`,
    `量能 ${multiple(member.turnover_vs_20d)}`,
    `行情源 ${member.data_source || "-"}`,
    `成员来源 ${member.member_source || member.source || "-"}`
  ].join("\n");

  async function loadMembers() {
    if (!DATA_TIME) throw new Error("暂无数据日期");
    if (state.payload) return state.payload;
    if (!state.loading) {
      state.loading = fetch(`data/members/${encodeURIComponent(DATA_TIME)}.json`, { cache: "no-store" }).then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      });
    }
    state.payload = await state.loading;
    return state.payload;
  }

  function inspectorHtml(member) {
    if (!member) return "点击热力图方块查看股票详情。";
    return `<strong>${escapeHtml(member.name || member.code)}</strong> ${escapeHtml(member.code)}
      <span class="${pctClass(member.day_change_pct)}">今日 ${pct(member.day_change_pct)}</span>
      <span class="${pctClass(member.change_5d_pct)}">近5日 ${pct(member.change_5d_pct)}</span>
      <span>成交额 ${money(member.turnover_amount)}</span>
      <span>量能 ${multiple(member.turnover_vs_20d)}</span>
      <span>行情源 ${escapeHtml(member.data_source || "-")}</span>
      <span>成员来源 ${escapeHtml(member.member_source || member.source || "-")}</span>`;
  }

  function renderBoard(board) {
    const members = Array.isArray(board.members) ? board.members : [];
    if (!members.length) {
      return `<p class="muted">${escapeHtml(board.board_name || "该板块")} 暂无可展示的成分股详情。</p>`;
    }
    const maxTurnover = Math.max(0, ...members.map((member) => asNumber(member.turnover_amount)).filter(Number.isFinite));
    const tiles = members.map((member, index) => {
      const amount = asNumber(member.turnover_amount);
      const scale = maxTurnover > 0 && Number.isFinite(amount) && amount > 0 ? 0.85 + Math.sqrt(amount / maxTurnover) * 0.65 : 1;
      const label = (member.name || member.code || "").slice(0, 4);
      return `<button class="heat-tile ${tileClass(member)}" type="button" data-member-index="${index}" style="--tile-scale:${scale.toFixed(2)}" title="${escapeHtml(memberTitle(member))}">
        <span>${escapeHtml(label)}</span><small>${pct(member.day_change_pct)}</small>
      </button>`;
    }).join("");
    const rows = members.map((member) => `<tr>
      <td>${escapeHtml(member.code)}</td>
      <td>${escapeHtml(member.name || "")}</td>
      <td class="num ${pctClass(member.day_change_pct)}">${pct(member.day_change_pct)}</td>
      <td class="num ${pctClass(member.change_5d_pct)}">${pct(member.change_5d_pct)}</td>
      <td class="num">${money(member.turnover_amount)}</td>
      <td class="num">${multiple(member.turnover_vs_20d)}</td>
      <td>${escapeHtml(member.data_source || "-")}</td>
      <td>${escapeHtml(member.member_source || member.source || "-")}</td>
    </tr>`).join("");
    const invalidCount = Array.isArray(board.invalid_members) ? board.invalid_members.length : 0;
    return `<div class="board-detail-header">
        <h3>${escapeHtml(board.board_name || "")} 成分股</h3>
        <span class="pill">成员 ${members.length} · 无报价 ${invalidCount}</span>
      </div>
      <div class="member-heatmap" role="list" aria-label="${escapeHtml(board.board_name || "")} 成分股热力图">${tiles}</div>
      <div class="member-inspector">${inspectorHtml(null)}</div>
      <div class="member-table-wrap">
        <table class="member-table">
          <thead><tr><th>代码</th><th>名称</th><th class="num">今日</th><th class="num">近5日</th><th class="num">成交额</th><th class="num">量能</th><th>行情源</th><th>成员来源</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function bindTiles(root, board) {
    const members = Array.isArray(board.members) ? board.members : [];
    const inspector = root.querySelector(".member-inspector");
    root.querySelectorAll(".heat-tile").forEach((button) => {
      button.addEventListener("click", () => {
        const member = members[Number(button.dataset.memberIndex)];
        if (inspector) inspector.innerHTML = inspectorHtml(member);
      });
    });
  }

  async function openBoard(button, row) {
    const boardId = button.dataset.boardId;
    const target = row.querySelector(".board-detail");
    if (!target) return;
    row.hidden = false;
    button.setAttribute("aria-expanded", "true");
    button.querySelector(".toggle-mark").textContent = "收起";
    if (target.dataset.loaded === "true") return;
    target.innerHTML = '<p class="muted">正在读取成分股详情...</p>';
    try {
      const payload = await loadMembers();
      const board = (payload.boards || []).find((item) => String(item.board_id) === String(boardId));
      if (!board) {
        target.innerHTML = '<p class="muted">未找到该板块的成分股快照。</p>';
        return;
      }
      target.innerHTML = renderBoard(board);
      target.dataset.loaded = "true";
      bindTiles(target, board);
    } catch (error) {
      target.innerHTML = `<p class="muted">无法读取成分股详情：${escapeHtml(error.message || error)}</p>`;
    }
  }

  document.querySelectorAll(".board-toggle").forEach((button) => {
    button.addEventListener("click", () => {
      const row = Array.from(document.querySelectorAll(".board-detail-row")).find((item) => item.dataset.boardId === button.dataset.boardId);
      if (!row) return;
      if (!row.hidden) {
        row.hidden = true;
        button.setAttribute("aria-expanded", "false");
        button.querySelector(".toggle-mark").textContent = "展开";
        return;
      }
      openBoard(button, row);
    });
  });
})();
</script>
"""


def render_index_html(latest: dict[str, Any], history: list[dict[str, Any]]) -> str:
    boards = latest.get("boards") or []
    status_value = latest.get("status")
    status_class = "bad" if status_value == "failed" else "warn" if status_value in {"partial", "non_trading_day", "stale_ok"} else "ok"
    rows = []
    for record in boards:
        board_id = str(record["board_id"])
        safe_board_id = html.escape(board_id, quote=True)
        safe_board_name = html.escape(record["board_name"])
        rows.append(
            f'<tr class="board-row" data-board-id="{safe_board_id}">'
            f'<td><button class="board-toggle" type="button" data-board-id="{safe_board_id}" aria-expanded="false" aria-controls="detail-{safe_board_id}"><strong>{safe_board_name}</strong><span class="toggle-mark">展开</span></button><small>{html.escape(record.get("note") or "")}</small></td>'
            f"<td class=\"num {change_class(record.get('equal_weight_change_pct'))}\">{pct_text(record.get('equal_weight_change_pct'))}</td>"
            f"<td class=\"num\">{pct_text(record.get('change_5d_pct'))}</td>"
            f"<td class=\"num\">{ratio_text(record.get('advance_ratio'))}</td>"
            f"<td class=\"num\">{multiple_text(record.get('turnover_vs_20d'))}</td>"
            f"<td class=\"num\">{ratio_text(record.get('coverage'))}</td>"
            f"<td>{status_badges(record.get('status') or [], record.get('error_flags') or [])}</td>"
            f"<td>{sparkline(history, board_id)}</td>"
            "</tr>"
            f'<tr class="board-detail-row" data-board-id="{safe_board_id}" id="detail-{safe_board_id}" hidden><td class="board-detail-cell" colspan="8"><div class="board-detail" data-detail-board-id="{safe_board_id}"><p class="muted">正在读取成分股详情...</p></div></td></tr>'
        )
    ranking_html = render_rankings(latest.get("rankings") or {})
    archive_links = render_archive_links(history)
    data_time_json = json.dumps(str(latest.get("data_time") or ""), ensure_ascii=False)
    member_detail_script = MEMBER_DETAIL_SCRIPT.replace("__DATA_TIME__", data_time_json)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A股重点板块热度记录器</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7f9; --text:#1f2933; --muted:#637083; --line:#d9dee8; --rise:#b42318; --fall:#027a48; --accent:#255e7e; --warn:#a15c07; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif; color:var(--text); background:var(--bg); }}
    header {{ padding:18px 16px 12px; background:#ffffff; border-bottom:1px solid var(--line); }}
    main {{ max-width:1180px; margin:0 auto; padding:14px 12px 38px; }}
    h1 {{ margin:0 0 8px; font-size:24px; letter-spacing:0; }}
    h2 {{ margin:24px 0 10px; font-size:18px; }}
    p {{ margin:0; color:var(--muted); line-height:1.55; }}
    a {{ color:var(--accent); text-decoration:none; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px; }}
    .pill {{ display:inline-flex; align-items:center; min-height:28px; padding:4px 9px; border:1px solid var(--line); border-radius:6px; background:#fff; font-size:13px; }}
    .pill.ok {{ border-color:#92c8ad; color:#075b39; }}
    .pill.warn {{ border-color:#e2bb74; color:var(--warn); }}
    .pill.bad {{ border-color:#e7a19c; color:#9b1c13; }}
    .notice {{ margin-top:12px; padding:10px 12px; border-left:4px solid var(--warn); background:#fff8e8; color:#5f3b07; }}
    .notice.ok {{ border-left-color:#2e7d55; background:#edf8f1; color:#075b39; }}
    .notice.bad {{ border-left-color:#b42318; background:#fff1f0; color:#9b1c13; }}
    .summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:14px; }}
    .metric {{ padding:12px; border:1px solid var(--line); border-radius:8px; background:#fff; }}
    .metric strong {{ display:block; font-size:20px; margin-top:4px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:middle; font-size:14px; }}
    th {{ text-align:left; color:#4b596b; background:#f0f3f7; font-weight:650; }}
    td small {{ display:block; margin-top:3px; color:var(--muted); line-height:1.35; }}
    .muted {{ color:var(--muted); }}
    .num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
    .rise {{ color:var(--rise); }}
    .fall {{ color:var(--fall); }}
    .board-toggle {{ display:inline-flex; align-items:center; gap:7px; max-width:100%; padding:0; border:0; background:transparent; color:var(--accent); font:inherit; text-align:left; cursor:pointer; }}
    .board-toggle strong {{ font-weight:700; }}
    .board-toggle:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; border-radius:4px; }}
    .toggle-mark {{ flex:0 0 auto; padding:2px 6px; border:1px solid var(--line); border-radius:5px; color:var(--muted); font-size:12px; background:#fff; }}
    .board-detail-cell {{ padding:0; background:#fbfcfe; }}
    .board-detail {{ padding:12px; white-space:normal; }}
    .board-detail-header {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:space-between; margin-bottom:10px; }}
    .board-detail-header h3 {{ margin:0; font-size:15px; }}
    .member-heatmap {{ display:flex; flex-wrap:wrap; gap:5px; align-items:flex-start; margin:10px 0 12px; }}
    .heat-tile {{ --tile-size:34px; width:calc(var(--tile-size) * var(--tile-scale, 1)); height:calc(var(--tile-size) * var(--tile-scale, 1)); min-width:30px; max-width:58px; min-height:30px; max-height:58px; padding:3px; border:1px solid rgba(0,0,0,.12); border-radius:5px; color:#fff; background:#8a94a6; overflow:hidden; cursor:pointer; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:1px; line-height:1.05; font-size:10px; }}
    .heat-tile span,.heat-tile small {{ display:block; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:inherit; }}
    .heat-tile.rise {{ background:#b42318; }}
    .heat-tile.fall {{ background:#027a48; }}
    .heat-tile.flat {{ background:#667085; }}
    .heat-tile.no-data {{ background:#d0d5dd; color:#344054; }}
    .heat-tile:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
    .member-inspector {{ min-height:38px; padding:9px 10px; border:1px solid var(--line); border-radius:7px; background:#fff; color:#344054; }}
    .member-table-wrap {{ overflow-x:auto; margin-top:10px; }}
    .member-table {{ min-width:760px; }}
    .member-table th,.member-table td {{ font-size:13px; padding:8px; }}
    .badge {{ display:inline-flex; margin:2px 3px 2px 0; padding:3px 7px; border-radius:5px; background:#e8f1f5; color:#16445d; font-size:12px; white-space:nowrap; }}
    .badge.warn {{ background:#fff0d3; color:#8a4b04; }}
    .badge.neutral {{ background:#eef0f3; color:#596579; }}
    .spark {{ width:140px; height:42px; display:block; }}
    .rankings {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
    .rank {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:12px; min-height:112px; }}
    .rank h3 {{ margin:0 0 8px; font-size:15px; }}
    .rank ol {{ margin:0; padding-left:22px; }}
    .rank li {{ margin:5px 0; color:#344054; }}
    .archive {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .footer {{ margin-top:26px; color:var(--muted); font-size:13px; }}
    @media (max-width: 780px) {{
      h1 {{ font-size:21px; }}
      main {{ padding-left:8px; padding-right:8px; }}
      .summary,.rankings {{ grid-template-columns:1fr 1fr; }}
      table {{ display:block; overflow-x:auto; white-space:nowrap; }}
      .board-detail {{ min-width:min(760px, 92vw); }}
      .member-heatmap {{ max-height:260px; overflow:auto; padding-right:2px; }}
      .heat-tile {{ --tile-size:32px; }}
      th,td {{ padding:9px 8px; }}
    }}
    @media (max-width: 520px) {{
      .summary,.rankings {{ grid-template-columns:1fr; }}
      .pill {{ font-size:12px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>A股重点板块热度记录器</h1>
    <p>仅用于板块热度记录、复盘和观察，不提供买入或卖出建议。</p>
    <div class="meta">
      <span class="pill {status_class}">数据状态：{html.escape(str(latest.get("status") or "-"))}</span>
      <span class="pill">数据日期：{html.escape(str(latest.get("data_time") or "-"))}</span>
      <span class="pill">运行时间：{html.escape(str(latest.get("run_time") or "-"))}</span>
      <a class="pill" href="data/latest.json">latest.json</a>
      <a class="pill" href="data/history.csv">history.csv</a>
    </div>
    {render_notice(latest)}
  </header>
  <main>
    {render_summary(boards)}
    {ranking_html}
    <h2>关注板块表</h2>
    <table>
      <thead><tr><th>板块</th><th class="num">等权涨跌</th><th class="num">近5日</th><th class="num">上涨占比</th><th class="num">量能</th><th class="num">覆盖率</th><th>状态</th><th>近60日趋势</th></tr></thead>
      <tbody>{''.join(rows) if rows else '<tr><td colspan="8">暂无有效数据</td></tr>'}</tbody>
    </table>
    <h2>历史归档</h2>
    <div class="archive">{archive_links}</div>
    <p class="footer">网页为静态文件，由 GitHub Actions 在收盘后生成；打开页面时不会实时请求行情接口。</p>
  </main>
  {member_detail_script}
</body>
</html>"""


def strip_html_like_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value))
    return compact_text(text, 120)


def user_facing_error_messages(latest: dict[str, Any]) -> list[str]:
    raw_errors = [str(error) for error in (latest.get("error_flags") or [])]
    visible_boards = {str(board.get("board_name") or "") for board in (latest.get("boards") or [])}
    messages: list[str] = []

    def add(message: str) -> None:
        if message not in messages:
            messages.append(message)

    for error in raw_errors:
        board_name = ""
        if " 成分股抓取失败" in error:
            board_name = error.split(" 成分股抓取失败", 1)[0].strip()
        elif " failed:" in error:
            board_name = error.split(" failed:", 1)[0].strip()

        if "成分股抓取失败" in error or "has no provider members" in error:
            if board_name and board_name in visible_boards:
                add(f"{board_name}：标准板块成分股接口暂时失败，已使用手动配置股票继续计算。")
            elif board_name:
                add(f"{board_name}：标准板块成分股接口暂时失败，已跳过该板块。")
            else:
                add("部分标准板块成分股接口暂时失败，已跳过受影响板块。")
        elif error == "provider_members_stale":
            add("部分板块使用了缓存或手动配置的成分股。")
        elif error == "low_coverage":
            add("部分板块有效报价覆盖率不足，已跳过状态判断。")
        elif "used latest quote fallback" in error:
            add("部分股票历史行情暂不可用，已用最新报价生成最小可用数据。")
        elif "stock quotes failed" in error or "quote history failed" in error:
            if board_name:
                add(f"{board_name}：部分个股行情暂时失败，已尽量使用备用数据源。")
            else:
                add("部分个股行情暂时失败，已尽量使用备用数据源。")

    return [strip_html_like_text(message) for message in messages[:6]]


def render_notice(latest: dict[str, Any]) -> str:
    status = latest.get("status")
    message = html.escape(str(latest.get("message") or ""))
    if status == "ok":
        return f'<div class="notice ok">{message or "数据已更新"}</div>'
    if status in {"non_trading_day", "stale_ok"}:
        return f'<div class="notice">{message or "今日非交易日，当前展示上一交易日数据"}</div>'
    if status == "failed":
        return f'<div class="notice bad">{message or "数据源异常，当前展示上一交易日数据"}</div>'
    messages = user_facing_error_messages(latest)
    if messages:
        escaped = "；".join(html.escape(message) for message in messages)
        return f'<div class="notice">数据源异常：{escaped}</div>'
    return ""


def render_summary(boards: list[dict[str, Any]]) -> str:
    eligible = [board for board in boards if board.get("eligible_for_signal")]
    avg_change = statistics.fmean(board.get("equal_weight_change_pct") or 0 for board in eligible) if eligible else None
    low_coverage = sum(1 for board in boards if board.get("coverage", 0) < MIN_COVERAGE)
    hot = sum(1 for board in boards if "过热警戒" in (board.get("status") or []))
    low = sum(1 for board in boards if "低位观察" in (board.get("status") or []))
    return (
        '<section class="summary">'
        f'<div class="metric">可判断板块<strong>{len(eligible)}/{len(boards)}</strong></div>'
        f'<div class="metric">平均等权涨跌<strong class="{change_class(avg_change)}">{pct_text(avg_change)}</strong></div>'
        f"<div class=\"metric\">过热警戒<strong>{hot}</strong></div>"
        f"<div class=\"metric\">低覆盖跳过<strong>{low_coverage}</strong></div>"
        "</section>"
    )


def render_rankings(rankings: dict[str, list[dict[str, Any]]]) -> str:
    sections = [
        ("strong", "强势榜"),
        ("overheat", "过热榜"),
        ("low", "低位榜"),
        ("rotation", "高低切换候选"),
    ]
    html_parts = ['<h2>今日摘要</h2><section class="rankings">']
    for key, title in sections:
        items = rankings.get(key) or []
        if items:
            body = "<ol>" + "".join(
                f"<li>{html.escape(item['board_name'])} <span class=\"{change_class(item.get('equal_weight_change_pct'))}\">{pct_text(item.get('equal_weight_change_pct'))}</span></li>"
                for item in items[:5]
            ) + "</ol>"
        else:
            body = '<p class="muted">暂无</p>'
        html_parts.append(f'<div class="rank"><h3>{title}</h3>{body}</div>')
    html_parts.append("</section>")
    return "".join(html_parts)


def render_archive_links(history: list[dict[str, Any]]) -> str:
    dates = sorted({record["date"] for record in history}, reverse=True)[:30]
    if not dates:
        return '<span class="muted">暂无归档</span>'
    return "".join(f'<a class="pill" href="archive/{date}.html">{date}</a>' for date in dates)


def change_class(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None or abs(parsed) < 0.0001:
        return ""
    return "rise" if parsed > 0 else "fall"


def render_archive_page(date: str, records: list[dict[str, Any]]) -> str:
    rows = []
    for record in sorted(records, key=lambda item: (item.get("priority", 100), item["board_name"])):
        rows.append(
            "<tr>"
            f"<td>{html.escape(record['board_name'])}</td>"
            f"<td class=\"num {change_class(record.get('equal_weight_change_pct'))}\">{pct_text(record.get('equal_weight_change_pct'))}</td>"
            f"<td class=\"num\">{pct_text(record.get('weighted_change_pct'))}</td>"
            f"<td class=\"num\">{ratio_text(record.get('advance_ratio'))}</td>"
            f"<td class=\"num\">{ratio_text(record.get('coverage'))}</td>"
            f"<td>{', '.join(html.escape(status) for status in (record.get('status') or [])) or '-'}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{date} 板块归档</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;margin:0;background:#f6f7f9;color:#1f2933}}main{{max-width:980px;margin:0 auto;padding:18px 10px}}a{{color:#255e7e;text-decoration:none}}table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d9dee8}}th,td{{padding:9px;border-bottom:1px solid #d9dee8;font-size:14px}}th{{text-align:left;background:#f0f3f7}}.num{{text-align:right;font-variant-numeric:tabular-nums}}.rise{{color:#b42318}}.fall{{color:#027a48}}</style>
</head><body><main><p><a href="../index.html">返回首页</a></p><h1>{date} 板块归档</h1><table><thead><tr><th>板块</th><th class="num">等权涨跌</th><th class="num">市值加权</th><th class="num">上涨占比</th><th class="num">覆盖率</th><th>状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table></main></body></html>"""


def render_site(output_dir: Path, latest: dict[str, Any], history: list[dict[str, Any]]) -> None:
    ensure_dir(output_dir)
    (output_dir / "index.html").write_text(render_index_html(latest, history), encoding="utf-8")
    archive_dir = output_dir / "archive"
    ensure_dir(archive_dir)
    for date in sorted({record["date"] for record in history}):
        date_records = [record for record in history if record["date"] == date]
        (archive_dir / f"{date}.html").write_text(render_archive_page(date, date_records), encoding="utf-8")


def latest_record_date(records: list[dict[str, Any]]) -> str | None:
    dates = sorted({str(record.get("date")) for record in records if record.get("date")})
    return dates[-1] if dates else None


def build_pipeline(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    output_dir = Path(args.output)
    data_dir = output_dir / "data"
    members_dir = data_dir / "members"
    ensure_dir(data_dir)
    ensure_dir(members_dir)
    previous_history_payload = read_json(data_dir / "history.json", {"records": []})
    previous_history = previous_history_payload.get("records") or []
    previous_latest = read_json(data_dir / "latest.json", {})
    member_cache = read_json(data_dir / "member_cache.json", {})
    client: Any = FixtureClient() if args.fixture else EastmoneyClient(timeout=args.timeout, retries=args.retries)
    run_time = now_iso()
    as_of_date = parse_iso_date(getattr(args, "as_of_date", None))
    as_of_date_text = as_of_date.isoformat()
    run_errors: list[str] = []
    incoming_records: list[dict[str, Any]] = []
    member_snapshots_by_date: dict[str, dict[str, Any]] = {}
    source = {
        "provider": "fixture" if args.fixture else "eastmoney",
        "fallback_provider": None if args.fixture else "tencent",
        "mode": "github_actions_static",
        "as_of_date": as_of_date_text,
    }

    if not args.fixture and is_non_trading_date(as_of_date):
        history = previous_history
        latest = build_latest_payload(
            history,
            run_time,
            source,
            [],
            False,
            previous_latest,
            getattr(client, "logs", []),
            status_override="non_trading_day",
            message_override="今日非交易日，当前展示上一交易日数据",
        )
        write_json(data_dir / "history.json", previous_history_payload if previous_history_payload.get("records") is not None else {"generated_at": run_time, "records": history})
        write_json(data_dir / "latest.json", latest)
        write_json(data_dir / "member_cache.json", member_cache)
        write_history_csv(data_dir / "history.csv", history)
        render_site(output_dir, latest, history)
        return 0

    for board in enabled_boards(config):
        try:
            members, excluded, provider_meta = fetch_members_with_cache(board, client, member_cache, run_errors)
            codes = [member["code"] for member in members]
            append_log(getattr(client, "logs", []), f"BOARD {board['name']} requesting stock quotes count={len(codes)}")
            try:
                quotes = client.fetch_quotes(codes)
            except Exception as exc:  # noqa: BLE001
                quotes = {}
                run_errors.append(f"{board['name']} stock quotes failed: {type(exc).__name__}: {compact_text(str(exc))}")
            for member in members:
                quote = quotes.get(member["code"])
                if quote and quote.get("name") and not member.get("name"):
                    member["name"] = quote["name"]
            kline_map = {}
            for code in codes:
                try:
                    append_log(getattr(client, "logs", []), f"BOARD {board['name']} requesting stock history code={code}")
                    kline_map[code] = client.fetch_stock_klines(code, args.history_days + 40)
                    time.sleep(args.request_pause)
                except Exception as exc:  # noqa: BLE001
                    kline_map[code] = []
                    run_errors.append(f"{board['name']} {code} quote history failed: {type(exc).__name__}: {compact_text(str(exc))}")
                if not kline_map[code]:
                    quote_row = quote_to_daily_row(quotes.get(code, {}))
                    if quote_row:
                        kline_map[code] = [quote_row]
                        run_errors.append(f"{board['name']} {code} used latest quote fallback as one-day history")
            if board_is_custom(board):
                source_changes = {}
                latest_change = None
            else:
                source_changes = client.fetch_board_klines(board.get("provider_board") or {}, args.history_days + 40)
                latest_change = client.fetch_board_latest_change(board.get("provider_board") or {})
            if latest_change is not None and source_changes:
                source_changes[max(source_changes)] = latest_change
            records, snapshots = aggregate_board_history(
                board,
                members,
                excluded,
                quotes,
                kline_map,
                source_changes,
                args.history_days,
            )
            for record in records:
                if provider_meta.get("stale"):
                    record["error_flags"].append("provider_members_stale")
            if records:
                incoming_records.extend(records)
                for date, snapshot in snapshots.items():
                    member_snapshots_by_date.setdefault(date, {"date": date, "boards": []})["boards"].append(snapshot)
            else:
                run_errors.append(f"{board['name']} produced no valid quote/history records")
        except Exception as exc:  # noqa: BLE001
            run_errors.append(f"{board['name']} failed: {type(exc).__name__}: {compact_text(str(exc))}")

    update_failed = not incoming_records
    incoming_latest_date = latest_record_date(incoming_records)
    stale_ok = bool(incoming_latest_date and incoming_latest_date < as_of_date_text and not args.fixture)
    source["latest_fetched_date"] = incoming_latest_date

    if stale_ok and previous_history:
        history = previous_history
    elif update_failed:
        history = previous_history
    else:
        merged = merge_history(previous_history, incoming_records, max(args.history_days, 120))
        history = enrich_history(merged, previous_history)

    status_override = "stale_ok" if stale_ok else None
    message_override = "今日行情尚未更新，当前展示上一交易日数据" if stale_ok else None
    latest = build_latest_payload(
        history,
        run_time,
        source,
        run_errors,
        update_failed and not stale_ok,
        previous_latest,
        getattr(client, "logs", []),
        status_override=status_override,
        message_override=message_override,
    )
    if stale_ok and previous_history_payload.get("records") is not None:
        write_json(data_dir / "history.json", previous_history_payload)
    else:
        write_json(data_dir / "history.json", {"generated_at": run_time, "records": history})
    write_json(data_dir / "latest.json", latest)
    write_json(data_dir / "member_cache.json", member_cache)
    write_history_csv(data_dir / "history.csv", history)

    should_write_member_snapshots = not (stale_ok and previous_history)
    if should_write_member_snapshots:
        for date, snapshot in member_snapshots_by_date.items():
            write_json(members_dir / f"{date}.json", snapshot)
    if latest.get("data_time") and latest["data_time"] not in member_snapshots_by_date:
        existing_snapshot = members_dir / f"{latest['data_time']}.json"
        if not existing_snapshot.exists():
            write_json(existing_snapshot, {"date": latest["data_time"], "boards": []})

    render_site(output_dir, latest, history)
    return 0


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build static A-share board heat dashboard")
    parser.add_argument("--config", default="boards.yml")
    parser.add_argument("--output", default="docs")
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--request-pause", type=float, default=0.05)
    parser.add_argument("--as-of-date", default=None, help="Override current China date as YYYY-MM-DD for tests")
    parser.add_argument("--fixture", action="store_true", help="Use deterministic sample data for local tests")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    try:
        return build_pipeline(args)
    except Exception as exc:  # noqa: BLE001
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
