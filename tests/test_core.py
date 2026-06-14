from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.ashare_static import (
    EastmoneyClient,
    FetchResult,
    FetchRequestError,
    aggregate_board_history,
    FixtureClient,
    build_latest_payload,
    build_pipeline,
    classify_status,
    enabled_boards,
    fetch_members_with_cache,
    load_config,
    merge_board_members,
    normalize_stock_code,
    parse_simple_yaml,
    user_facing_error_messages,
)


class CoreTests(unittest.TestCase):
    def test_parse_board_config_fields(self) -> None:
        config = load_config(Path("boards.yml"))
        boards = enabled_boards(config)
        self.assertGreaterEqual(len(boards), 1)
        first = boards[0]
        self.assertIn("provider_board", first)
        self.assertIn("include", first)
        self.assertIn("exclude", first)
        self.assertIn("custom_members", first)
        self.assertIn("priority", first)
        self.assertIn("note", first)

    def test_simple_yaml_nested_lists(self) -> None:
        parsed = parse_simple_yaml(
            """
boards:
  - id: demo
    enabled: true
    provider_board:
      code: BK0001
    include:
      - 600000.SH
      - code: 000001.SZ
        name: 平安银行
"""
        )
        self.assertEqual(parsed["boards"][0]["provider_board"]["code"], "BK0001")
        self.assertEqual(parsed["boards"][0]["include"][1]["name"], "平安银行")

    def test_member_merge_include_exclude_custom(self) -> None:
        board = {
            "include": ["000001.SZ"],
            "exclude": ["600000.SH"],
            "custom_members": [{"code": "300750.SZ", "name": "宁德时代"}],
        }
        members, excluded = merge_board_members(
            board,
            [
                {"code": "600000.SH", "name": "浦发银行", "source": "provider"},
                {"code": "600519.SH", "name": "贵州茅台", "source": "provider"},
            ],
        )
        codes = [member["code"] for member in members]
        self.assertNotIn("600000.SH", codes)
        self.assertIn("000001.SZ", codes)
        self.assertIn("300750.SZ", codes)
        self.assertTrue(excluded[0]["was_present"])

    def test_provider_failure_uses_manual_members(self) -> None:
        class FailingProviderClient:
            logs: list[str] = []

            def fetch_board_members(self, provider: dict[str, object]) -> FetchResult:
                return FetchResult(ok=False, members=[], error="HTTPError status=502 body=Bad Gateway")

        board = {
            "id": "manual_fallback",
            "name": "Manual Fallback",
            "provider_board": {"source": "eastmoney", "code": "BK0001", "type": "concept"},
            "include": ["000001.SZ"],
            "exclude": [],
            "custom_members": ["300750.SZ"],
        }
        errors: list[str] = []
        members, _excluded, meta = fetch_members_with_cache(board, FailingProviderClient(), {}, errors)
        self.assertTrue(meta["stale"])
        self.assertEqual([member["code"] for member in members], ["000001.SZ", "300750.SZ"])
        self.assertIn("HTTPError status=502", errors[0])

    def test_custom_board_does_not_request_provider_members(self) -> None:
        class NoProviderClient:
            logs: list[str] = []

            def fetch_board_members(self, provider: dict[str, object]) -> FetchResult:
                raise AssertionError("custom board should not request provider members")

        board = {
            "id": "custom_only",
            "name": "Custom Only",
            "provider_board": {"source": "eastmoney", "code": "BK0001", "type": "custom"},
            "include": [],
            "exclude": [],
            "custom_members": ["600519.SH", "300750.SZ"],
        }
        members, _excluded, meta = fetch_members_with_cache(board, NoProviderClient(), {}, [])
        self.assertEqual(meta["source"], "custom")
        self.assertEqual([member["code"] for member in members], ["300750.SZ", "600519.SH"])

    def test_user_notice_hides_html_error_body(self) -> None:
        latest = {
            "error_flags": [
                "半导体 成分股抓取失败: HTTPError status=502 body=<html><head><title>502 Bad Gateway</title></head></html>",
                "半导体 failed: PipelineError: 半导体 has no provider members, no cache, and no include/custom_members",
            ],
            "boards": [{"board_name": "AI算力"}],
        }
        messages = user_facing_error_messages(latest)
        joined = " ".join(messages)
        self.assertIn("半导体：标准板块成分股接口暂时失败，已跳过该板块。", joined)
        self.assertNotIn("<html>", joined)
        self.assertNotIn("Bad Gateway", joined)

    def test_stock_history_prefers_tencent_and_circuits_eastmoney(self) -> None:
        client = EastmoneyClient(retries=1)
        calls = {"eastmoney": 0}

        def tencent_ok(code: str, limit: int) -> list[dict[str, object]]:
            return [{"date": "2026-06-10", "change_pct": 1.0, "turnover_amount": 1000}]

        def eastmoney_fail(*args: object, **kwargs: object) -> dict[str, object]:
            calls["eastmoney"] += 1
            raise FetchRequestError("HTTPError status=502 body=<html>bad gateway</html>")

        client.fetch_tencent_stock_klines = tencent_ok  # type: ignore[method-assign]
        client.request_json = eastmoney_fail  # type: ignore[method-assign]
        rows = client.fetch_stock_klines("000001.SZ", 30)
        self.assertEqual(rows[0]["date"], "2026-06-10")
        self.assertEqual(calls["eastmoney"], 0)

        def tencent_empty(code: str, limit: int) -> list[dict[str, object]]:
            return []

        client.fetch_tencent_stock_klines = tencent_empty  # type: ignore[method-assign]
        for _ in range(3):
            self.assertEqual(client.fetch_stock_klines("000001.SZ", 30), [])
        self.assertTrue(client.eastmoney_stock_history_circuit_open)
        self.assertEqual(calls["eastmoney"], 3)
        self.assertEqual(client.fetch_stock_klines("000001.SZ", 30), [])
        self.assertEqual(calls["eastmoney"], 3)

    def test_status_skips_low_coverage(self) -> None:
        record = {
            "eligible_for_signal": False,
            "coverage": 0.5,
            "equal_weight_change_pct": 3,
            "change_5d_pct": 8,
            "position_60d": 0.95,
            "turnover_vs_20d": 2,
            "consecutive_up_days": 5,
            "advance_ratio": 0.9,
        }
        self.assertEqual(classify_status(record), [])

    def test_member_snapshot_contains_stock_detail_fields(self) -> None:
        board = {"id": "demo", "name": "Demo", "priority": 1, "note": ""}
        members = [
            {"code": "000001.SZ", "name": "Ping An", "source": "include"},
            {"code": "600000.SH", "name": "SPD Bank", "source": "provider"},
        ]
        kline_map = {
            "000001.SZ": [
                {"date": "2026-06-08", "change_pct": 1.0, "turnover_amount": 100, "data_source": "tencent"},
                {"date": "2026-06-09", "change_pct": -0.5, "turnover_amount": 120, "data_source": "tencent"},
                {"date": "2026-06-10", "change_pct": 2.0, "turnover_amount": 160, "data_source": "tencent"},
                {"date": "2026-06-11", "change_pct": 0.3, "turnover_amount": 140, "data_source": "tencent"},
                {"date": "2026-06-12", "change_pct": 1.2, "turnover_amount": 220, "data_source": "tencent"},
            ],
            "600000.SH": [],
        }
        records, snapshots = aggregate_board_history(board, members, [], {}, kline_map, {}, 30)
        self.assertTrue(records)
        snapshot = snapshots["2026-06-12"]
        self.assertEqual(snapshot["date"], "2026-06-12")
        self.assertEqual(len(snapshot["members"]), 2)
        active = snapshot["members"][0]
        inactive = snapshot["members"][1]
        self.assertEqual(active["source"], "include")
        self.assertEqual(active["data_source"], "tencent")
        self.assertIsNotNone(active["day_change_pct"])
        self.assertIsNotNone(active["change_5d_pct"])
        self.assertIsNotNone(active["turnover_vs_20d"])
        self.assertEqual(inactive["source"], "provider_board")
        self.assertEqual(inactive["quote_status"], "no_data")

    def test_failed_update_keeps_previous_data_time(self) -> None:
        latest = build_latest_payload(
            history=[],
            run_time="2026-06-10T16:10:00+08:00",
            source={"provider": "eastmoney"},
            run_errors=["fetch failed"],
            update_failed=True,
            previous_latest={"data_time": "2026-06-09"},
        )
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["data_time"], "2026-06-09")
        self.assertIn("数据源异常", latest["message"])

    def test_stale_ok_message(self) -> None:
        latest = build_latest_payload(
            history=[
                {
                    "date": "2026-06-12",
                    "board_id": "custom_watch",
                    "board_name": "我的观察池",
                    "priority": 1,
                    "coverage": 1.0,
                    "status": [],
                    "error_flags": [],
                }
            ],
            run_time="2026-06-15T16:10:00+08:00",
            source={"provider": "eastmoney", "latest_fetched_date": "2026-06-12"},
            run_errors=[],
            update_failed=False,
            previous_latest={"data_time": "2026-06-12"},
            status_override="stale_ok",
            message_override="今日行情尚未更新，当前展示上一交易日数据",
        )
        self.assertEqual(latest["status"], "stale_ok")
        self.assertIn("上一交易日", latest["message"])

    def test_non_trading_day_keeps_previous_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "docs"
            data_dir = output / "data"
            data_dir.mkdir(parents=True)
            previous_record = {
                "date": "2026-06-12",
                "board_id": "custom_watch",
                "board_name": "我的观察池",
                "priority": 1,
                "member_count": 3,
                "valid_quote_count": 3,
                "coverage": 1.0,
                "equal_weight_change_pct": 1.2,
                "weighted_change_pct": 1.1,
                "advance_ratio": 0.66,
                "turnover_amount": 1000,
                "status": [],
                "error_flags": [],
                "eligible_for_signal": True,
            }
            (data_dir / "history.json").write_text(
                '{"generated_at":"old","records":[' + __import__("json").dumps(previous_record, ensure_ascii=False) + "]}",
                encoding="utf-8",
            )
            (data_dir / "latest.json").write_text('{"data_time":"2026-06-12"}', encoding="utf-8")
            result = build_pipeline(
                Namespace(
                    config="boards.yml",
                    output=str(output),
                    history_days=30,
                    timeout=5,
                    retries=1,
                    request_pause=0,
                    fixture=False,
                    as_of_date="2026-06-14",
                )
            )
            self.assertEqual(result, 0)
            latest = __import__("json").loads((data_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["status"], "non_trading_day")
            self.assertEqual(latest["data_time"], "2026-06-12")
            self.assertIn("非交易日", latest["message"])

    def test_fixture_pipeline_outputs_static_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "docs"
            result = build_pipeline(
                Namespace(
                    config="boards.yml",
                    output=str(output),
                    history_days=130,
                    timeout=5,
                    retries=1,
                    request_pause=0,
                    fixture=True,
                )
            )
            self.assertEqual(result, 0)
            self.assertTrue((output / "index.html").exists())
            self.assertTrue((output / "data" / "latest.json").exists())
            self.assertTrue((output / "data" / "history.json").exists())
            self.assertTrue((output / "data" / "history.csv").exists())
            latest = json.loads((output / "data" / "latest.json").read_text(encoding="utf-8"))
            member_payload = json.loads((output / "data" / "members" / f"{latest['data_time']}.json").read_text(encoding="utf-8"))
            first_member = member_payload["boards"][0]["members"][0]
            self.assertIn("day_change_pct", first_member)
            self.assertIn("change_5d_pct", first_member)
            self.assertIn("turnover_amount", first_member)
            self.assertIn("data_source", first_member)
            html = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("board-toggle", html)
            self.assertIn("member-heatmap", html)
            self.assertIn("data/members/", html)

    def test_stock_code_normalization(self) -> None:
        self.assertEqual(normalize_stock_code("sh600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("000001"), "000001.SZ")
        self.assertEqual(normalize_stock_code("688001"), "688001.SH")


if __name__ == "__main__":
    unittest.main()
