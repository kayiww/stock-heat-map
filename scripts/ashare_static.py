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


def now_iso() -> str:
    return dt.datetime.now(CN_TZ).replace(microsecond=0).isoformat()


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

    def request_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            request = urllib.request.Request(
                f"{url}?{query}",
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AShareBoardHeat/1.0)",
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8", errors="replace"))
            except (OSError, TimeoutError, json.JSONDecodeError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt + 1 >= self.retries:
                    break
                time.sleep(0.8 * (attempt + 1))
        raise last_error or RuntimeError("request failed")

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
            )
            return to_float(payload.get("data", {}).get("f3"))
        except Exception:
            return None

    def fetch_quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        quotes: dict[str, dict[str, Any]] = {}
        for chunk in chunks(codes, 80):
            secids = ",".join(code_to_secid(code) for code in chunk)
            payload = self.request_json(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                {
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2,
                    "invt": 2,
                    "secids": secids,
                    "fields": self.quote_fields,
                },
            )
            for row in payload.get("data", {}).get("diff") or []:
                parsed = parse_eastmoney_quote_row(row)
                if parsed:
                    quotes[parsed["code"]] = parsed
        return quotes

    def fetch_stock_klines(self, code: str, limit: int) -> list[dict[str, Any]]:
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
        )
        rows = payload.get("data", {}).get("klines") or []
        parsed_rows = []
        for row in rows:
            parsed = parse_kline_row(row)
            if parsed:
                parsed_rows.append(parsed)
        return parsed_rows

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


class FixtureClient:
    def __init__(self) -> None:
        self.start = dt.date(2025, 1, 2)

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


def fetch_members_with_cache(
    board: dict[str, Any],
    client: Any,
    member_cache: dict[str, Any],
    run_errors: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    provider = board.get("provider_board") or {}
    provider_members: list[dict[str, Any]] = []
    provider_meta = {"source": provider.get("source") or "custom", "stale": False, "error": None}
    if provider.get("code"):
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
            run_errors.append(f"{board['name']} standard members stale: {provider_meta['error']}")
            if not provider_members:
                raise PipelineError(f"{board['name']} has no provider members and no cache")
    members, excluded = merge_board_members(board, provider_members)
    if not members:
        raise PipelineError(f"{board['name']} resolved to an empty member list")
    return members, excluded, provider_meta


def aggregate_board_history(
    board: dict[str, Any],
    members: list[dict[str, Any]],
    excluded_members: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    kline_map: dict[str, list[dict[str, Any]]],
    source_board_changes: dict[str, float],
    history_days: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    member_count = len(members)
    by_date: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    invalid_members = []
    for member in members:
        rows = kline_map.get(member["code"]) or []
        if not rows:
            invalid_members.append({"code": member["code"], "name": member.get("name") or "", "reason": "no_quote"})
            continue
        for row in rows:
            by_date.setdefault(row["date"], []).append((member, row))
    dates = sorted(by_date)[-history_days:]
    records: list[dict[str, Any]] = []
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
    member_snapshot = {
        "board_id": board["id"],
        "board_name": board["name"],
        "members": [
            {
                "code": member["code"],
                "name": quotes.get(member["code"], {}).get("name") or member.get("name") or "",
                "source": member.get("source") or "",
            }
            for member in members
        ],
        "excluded_members": excluded_members,
        "invalid_members": invalid_members,
    }
    return records, member_snapshot


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
) -> dict[str, Any]:
    data_date, records = latest_records(history)
    status = "ok"
    if update_failed:
        status = "failed"
    elif run_errors:
        status = "partial"
    if update_failed and previous_latest and previous_latest.get("data_time"):
        data_date = previous_latest.get("data_time")
    coverage_values = [record.get("coverage", 0) for record in records]
    return {
        "data_time": data_date,
        "run_time": run_time,
        "source": source,
        "status": status,
        "message": "今日更新失败，当前展示上一交易日数据" if update_failed else "数据已更新",
        "coverage": {
            "overall": rounded_ratio(statistics.fmean(coverage_values)) if coverage_values else None,
            "by_board": {record["board_id"]: record.get("coverage") for record in records},
        },
        "error_flags": sorted(set(run_errors + [flag for record in records for flag in (record.get("error_flags") or [])])),
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


def render_index_html(latest: dict[str, Any], history: list[dict[str, Any]]) -> str:
    boards = latest.get("boards") or []
    status_class = "bad" if latest.get("status") == "failed" else "warn" if latest.get("status") == "partial" else "ok"
    rows = []
    for record in boards:
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(record['board_name'])}</strong><small>{html.escape(record.get('note') or '')}</small></td>"
            f"<td class=\"num {change_class(record.get('equal_weight_change_pct'))}\">{pct_text(record.get('equal_weight_change_pct'))}</td>"
            f"<td class=\"num\">{pct_text(record.get('change_5d_pct'))}</td>"
            f"<td class=\"num\">{ratio_text(record.get('advance_ratio'))}</td>"
            f"<td class=\"num\">{multiple_text(record.get('turnover_vs_20d'))}</td>"
            f"<td class=\"num\">{ratio_text(record.get('coverage'))}</td>"
            f"<td>{status_badges(record.get('status') or [], record.get('error_flags') or [])}</td>"
            f"<td>{sparkline(history, record['board_id'])}</td>"
            "</tr>"
        )
    ranking_html = render_rankings(latest.get("rankings") or {})
    archive_links = render_archive_links(history)
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
    .summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:14px; }}
    .metric {{ padding:12px; border:1px solid var(--line); border-radius:8px; background:#fff; }}
    .metric strong {{ display:block; font-size:20px; margin-top:4px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:middle; font-size:14px; }}
    th {{ text-align:left; color:#4b596b; background:#f0f3f7; font-weight:650; }}
    td small {{ display:block; margin-top:3px; color:var(--muted); line-height:1.35; }}
    .num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
    .rise {{ color:var(--rise); }}
    .fall {{ color:var(--fall); }}
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
</body>
</html>"""


def render_notice(latest: dict[str, Any]) -> str:
    if latest.get("status") == "failed":
        return '<div class="notice">今日更新失败，当前展示上一交易日数据。</div>'
    errors = latest.get("error_flags") or []
    if errors:
        escaped = "；".join(html.escape(str(error)) for error in errors[:5])
        return f'<div class="notice">存在数据质量提示：{escaped}</div>'
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
    run_errors: list[str] = []
    incoming_records: list[dict[str, Any]] = []
    member_snapshots_by_date: dict[str, dict[str, Any]] = {}
    source = {"provider": "fixture" if args.fixture else "eastmoney", "mode": "github_actions_static"}

    for board in enabled_boards(config):
        try:
            members, excluded, provider_meta = fetch_members_with_cache(board, client, member_cache, run_errors)
            codes = [member["code"] for member in members]
            quotes = client.fetch_quotes(codes)
            for member in members:
                quote = quotes.get(member["code"])
                if quote and quote.get("name") and not member.get("name"):
                    member["name"] = quote["name"]
            kline_map = {}
            for code in codes:
                try:
                    kline_map[code] = client.fetch_stock_klines(code, args.history_days + 40)
                    time.sleep(args.request_pause)
                except Exception as exc:  # noqa: BLE001
                    kline_map[code] = []
                    run_errors.append(f"{board['name']} {code} quote history failed: {exc}")
            source_changes = client.fetch_board_klines(board.get("provider_board") or {}, args.history_days + 40)
            latest_change = client.fetch_board_latest_change(board.get("provider_board") or {})
            if latest_change is not None and source_changes:
                source_changes[max(source_changes)] = latest_change
            records, snapshot = aggregate_board_history(
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
            incoming_records.extend(records)
            for record in records:
                member_snapshots_by_date.setdefault(record["date"], {"date": record["date"], "boards": []})["boards"].append(snapshot)
        except Exception as exc:  # noqa: BLE001
            run_errors.append(f"{board['name']} failed: {exc}")

    update_failed = not incoming_records
    if update_failed:
        history = previous_history
    else:
        merged = merge_history(previous_history, incoming_records, max(args.history_days, 120))
        history = enrich_history(merged, previous_history)

    latest = build_latest_payload(history, run_time, source, run_errors, update_failed, previous_latest)
    write_json(data_dir / "history.json", {"generated_at": run_time, "records": history})
    write_json(data_dir / "latest.json", latest)
    write_json(data_dir / "member_cache.json", member_cache)
    write_history_csv(data_dir / "history.csv", history)

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
