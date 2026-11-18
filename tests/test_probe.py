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
        win_paths = [rf"C:\Users\demo\.gemini\antigravity-cli\conversations\{uid}.db"]
        self.assertEqual(probe.find_conv_from_paths(win_paths), uid)
        self.assertEqual(probe.parse_ps_eww_pane("259601 s005 S+ 0:01 agy acp TERN_PANE=1378684502035 HOME=/Users/demo"), "1378684502035")

    def test_windows_discovery_matches_title_uuid_and_sqlite_workspace_uri(self) -> None:
        import sqlite3

        uid_resumed = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        uid_fresh = "99999999-8888-7777-6666-555555555555"
        uid_stale = "12121212-3434-5656-7878-909090909090"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True)

            for cid, ws_uri, with_wal in (
                (uid_resumed, "file:///C:/Users/Alice/AppOne", False),
                (uid_stale, "file:///C:/Users/Alice/AppTwo", False),
                (uid_fresh, "file:///C:/Users/Alice/AppTwo", True),
            ):
                tdir = root / "brain" / cid / ".system_generated" / "logs"
                tdir.mkdir(parents=True)
                (tdir / "transcript.jsonl").write_text('{"type":"USER_INPUT"}\n', encoding="utf-8")
                db_path = conv_dir / f"{cid}.db"
                conn = sqlite3.connect(db_path)
                conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
                conn.execute(
                    "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                    (b"\x0a(" + ws_uri.encode("utf-8") + b"z",),
                )
                conn.commit()
                conn.close()
                if with_wal:
                    Path(f"{db_path}-wal").write_bytes(b"wal")

            hints = [
                {"pane": 10, "cwd": r"C:\Users\Alice\AppOne", "title": f"agy --conversation={uid_resumed}", "command": None},
                {"pane": 20, "cwd": r"C:\Users\Alice\AppTwo", "title": "New Chat", "command": None},
                # `omp.exe` worker in same workspace must NEVER steal or duplicate `agy`'s conversation
                {"pane": 21, "cwd": r"C:\Users\Alice\AppTwo", "title": "omp worker", "command": r"C:\Local\omp.exe --auto-approve"},
            ]

            # 1. With live `agy acp` process: resolves pane 10 (title UUID) and pane 20 (active .db-wal workspace match), skips omp pane 21
            found = probe.discover_panes_windows(
                hints,
                root,
                win_procs=[{"ProcessId": 4200, "ParentProcessId": 1000, "CommandLine": "agy.cmd acp"}],
            )
            self.assertEqual(set(found.keys()), {"10", "20"})
            self.assertEqual(found["10"]["conversation_id"], uid_resumed)
            self.assertEqual(found["20"]["conversation_id"], uid_fresh)

            # 2. With zero live `agy` processes: unresolved workspace pane 20 is NOT falsely matched to closed `.db` files
            found_no_agy = probe.discover_panes_windows(
                [{"pane": 20, "cwd": r"C:\Users\Alice\AppTwo", "title": "Claude", "command": None}],
                root,
                win_procs=[],
            )
            self.assertEqual(found_no_agy, {})

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
