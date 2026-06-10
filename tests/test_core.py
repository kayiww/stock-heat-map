from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.ashare_static import (
    FixtureClient,
    build_latest_payload,
    build_pipeline,
    classify_status,
    enabled_boards,
    load_config,
    merge_board_members,
    normalize_stock_code,
    parse_simple_yaml,
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
        self.assertIn("今日更新失败", latest["message"])

    def test_fixture_pipeline_outputs_static_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "public"
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

    def test_stock_code_normalization(self) -> None:
        self.assertEqual(normalize_stock_code("sh600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("000001"), "000001.SZ")
        self.assertEqual(normalize_stock_code("688001"), "688001.SH")


if __name__ == "__main__":
    unittest.main()
