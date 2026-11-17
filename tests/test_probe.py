from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PROBE_PATH = ROOT_DIR / "probe.py"

spec = importlib.util.spec_from_file_location("probe", PROBE_PATH)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_find_conv_from_cmdline(self) -> None:
        uid = "56ae756f-0f14-477d-85c8-ca0cebdea7be"
        self.assertEqual(probe.find_conv_from_cmdline(f"tern acp agy --resume {uid}"), uid)
        self.assertEqual(probe.find_conv_from_cmdline(f"agy --conversation={uid.upper()}"), uid)
        self.assertIsNone(probe.find_conv_from_cmdline("agy acp --disable-telemetry"))

    def test_find_conv_from_paths_and_lsof_parser(self) -> None:
        uid = "c1aa9c2f-72fc-4e5f-ae69-492389b0842e"
        lsof_sample = (
            "p259601\n"
            "fcwd\n"
            "n/Users/demo/Code/app\n"
            "f14\n"
            f"n/Users/demo/.gemini/antigravity-cli/conversations/{uid}.db\n"
        )
        cwd, paths = probe.parse_lsof_fn_output(lsof_sample)
        self.assertEqual(cwd, "/Users/demo/Code/app")
        self.assertEqual(probe.find_conv_from_paths(paths), uid)
        self.assertEqual(probe.parse_ps_eww_pane("259601 s005 S+ 0:01 agy acp TERN_PANE=1378684502035 HOME=/Users/demo"), "1378684502035")

    def test_summarize_transcript_computes_hits_and_epoch(self) -> None:
        uid = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_dir = root / "brain" / uid / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            tpath = logs_dir / "transcript.jsonl"
            records = [
                {"step_index": 0, "type": "USER_INPUT", "content": "hi"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-17T20:00:00Z",
                    "model": "gemini-4-carbon-medium",
                    "input_tokens": 20000,
                    "cache_read_tokens": 0,
                    "output_tokens": 500,
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-17T20:01:30Z",
                    "model": "gemini-4-carbon-medium",
                    "input_tokens": 2000,
                    "cache_read_tokens": 18000,
                    "output_tokens": 300,
                },
            ]
            tpath.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

            summary = probe.summarize_transcript(uid, data_dir=root)
            assert summary is not None
            self.assertEqual(summary["turn_count"], 2)
            self.assertAlmostEqual(summary["last_hit"], 0.9, places=4)
            # Total: 18000 / (22000 + 18000) = 18000 / 40000 = 0.45
            self.assertAlmostEqual(summary["total_hit"], 0.45, places=4)
            self.assertEqual(summary["last_input"], 2000)
            self.assertEqual(summary["last_cache_read"], 18000)
            self.assertEqual(summary["series"], [0.0, 0.9])
            self.assertGreater(summary["last_epoch_ms"], 0)


if __name__ == "__main__":
    unittest.main()
