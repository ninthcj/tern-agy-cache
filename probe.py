#!/usr/bin/env python3
"""probe.py — Map live `agy acp` Tern panes to Antigravity transcripts across Linux, macOS, and Windows."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
TERN_PANE_ENV_RE = re.compile(r"(?:^|\s)TERN_PANE=(\d+)(?:\s|$)")
FILE_URI_BYTES_RE = re.compile(rb"file:///[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
WIN_DRIVE_RE = re.compile(r"^/?([A-Za-z]:)(/.*)?$")


def antigravity_data_dir() -> Path:
    """Resolve Antigravity CLI state directory (`~/.gemini/antigravity-cli` across Linux, macOS & Windows)."""
    override = os.environ.get("ANTIGRAVITY_CLI_HOME")
    if override:
        return Path(override).expanduser()
    gemini_home = os.environ.get("GEMINI_CLI_HOME")
    if gemini_home:
        return Path(gemini_home).expanduser() / "antigravity-cli"
    return Path.home() / ".gemini" / "antigravity-cli"


def find_conv_from_cmdline(cmd: str) -> str | None:
    if not cmd:
        return None
    for flag in ("--resume", "--conversation", "agy:"):
        idx = cmd.find(flag)
        if idx >= 0:
            m = UUID_RE.search(cmd[idx:])
            if m:
                return m.group(1).lower()
    return None


def find_conv_from_paths(paths: list[str]) -> str | None:
    for target in paths:
        norm = target.replace("\\", "/")
        if "/conversations/" in norm and norm.endswith(".db"):
            m = UUID_RE.search(Path(norm).stem)
            if m:
                return m.group(1).lower()
    return None


def normalize_workspace_key(path_or_uri: str) -> str:
    """Normalize a local path or `file://` URI into a comparable workspace key across POSIX & Windows."""
    s = path_or_uri.strip()
    if not s:
        return ""
    if s.startswith("file://"):
        parsed = urlparse(s)
        s = unquote(parsed.path or "")
    s = s.replace("\\", "/")
    m = WIN_DRIVE_RE.match(s)
    if m:
        drive = m.group(1).lower()
        rest = (m.group(2) or "").rstrip("/").lower()
        return f"{drive}{rest}"
    return s.rstrip("/") or "/"


def extract_workspace_keys_from_db(db_path: Path) -> set[str]:
    """Read workspace `file://` URIs recorded in `trajectory_metadata_blob` of an Antigravity `.db`."""
    keys: set[str] = set()
    if not db_path.is_file():
        return keys
    conn: sqlite3.Connection | None = None
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        rows = conn.execute("SELECT data FROM trajectory_metadata_blob").fetchall()
        for (blob,) in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            for m in FILE_URI_BYTES_RE.finditer(bytes(blob)):
                raw_uri = m.group(0).decode("utf-8", errors="ignore")
                # Trim trailing protobuf printable tag byte if stuck to end of path (only when path doesn't exist)
                norm = normalize_workspace_key(raw_uri)
                if norm:
                    keys.add(norm)
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return keys


def _workspace_matches(pane_key: str, db_keys: set[str]) -> bool:
    if not pane_key or not db_keys:
        return False
    if pane_key in db_keys:
        return True
    # Protobuf string fields adjacent to a printable ASCII field tag can carry 1 trailing byte in regex scans
    for candidate in db_keys:
        if len(candidate) == len(pane_key) + 1 and candidate.startswith(pane_key):
            return True
    return False


# ── Linux (/proc) discovery ────────────────────────────────────────────────


def _linux_proc_env_var(pid: str, key: str) -> str | None:
    prefix = f"{key}=".encode("utf-8")
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for entry in raw.split(b"\x00"):
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode("utf-8", errors="replace")
    return None


def _linux_proc_cmdline(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _linux_proc_ppid(pid: str) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rparen = text.rfind(")")
        if rparen < 0:
            return None
        rest = text[rparen + 2 :].split()
        return rest[1] if len(rest) > 1 else None
    except OSError:
        return None


def _linux_proc_fd_targets(pid: str) -> list[str]:
    out: list[str] = []
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return out
    for fd in entries:
        try:
            out.append(os.readlink(fd))
        except OSError:
            continue
    return out


def discover_panes_linux() -> dict[str, dict[str, Any]]:
    panes: dict[str, dict[str, Any]] = {}
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return panes

    for pid in pids:
        cmd = _linux_proc_cmdline(pid)
        if "agy acp" not in cmd or "tern acp" in cmd:
            continue
        pane = _linux_proc_env_var(pid, "TERN_PANE")
        if not pane or not pane.isdigit():
            continue
        conv_id = find_conv_from_paths(_linux_proc_fd_targets(pid))
        if not conv_id:
            ppid = _linux_proc_ppid(pid)
            if ppid:
                conv_id = find_conv_from_cmdline(_linux_proc_cmdline(ppid))
        if not conv_id:
            conv_id = find_conv_from_cmdline(cmd)
        if not conv_id:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        panes[pane] = {
            "pane": int(pane),
            "conversation_id": conv_id,
            "cwd": cwd,
            "pid": int(pid),
        }
    return panes


# ── macOS (ps + lsof) discovery ────────────────────────────────────────────


def parse_lsof_fn_output(raw: str) -> tuple[str, list[str]]:
    """Parse `lsof -Fn -p <pid>` output into `(cwd, open_paths)`."""
    cwd = ""
    paths: list[str] = []
    current_fd = ""
    for line in raw.splitlines():
        if line.startswith("f"):
            current_fd = line[1:]
        elif line.startswith("n"):
            val = line[1:]
            if current_fd == "cwd":
                cwd = val
            else:
                paths.append(val)
    return cwd, paths


def parse_ps_eww_pane(raw: str) -> str | None:
    m = TERN_PANE_ENV_RE.search(raw)
    return m.group(1) if m else None


def discover_panes_macos() -> dict[str, dict[str, Any]]:
    panes: dict[str, dict[str, Any]] = {}
    try:
        ps_out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except Exception:
        return panes

    cmd_by_pid: dict[str, tuple[str, str]] = {}
    candidates: list[tuple[str, str, str]] = []
    for line in ps_out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = parts[0], parts[1], parts[2]
        cmd_by_pid[pid] = (ppid, cmd)
        if "agy acp" in cmd and "tern acp" not in cmd:
            candidates.append((pid, ppid, cmd))

    for pid, ppid, cmd in candidates:
        try:
            env_out = subprocess.check_output(
                ["ps", "eww", "-p", pid],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
        except Exception:
            continue
        pane = parse_ps_eww_pane(env_out)
        if not pane:
            continue

        cwd = ""
        open_paths: list[str] = []
        try:
            lsof_out = subprocess.check_output(
                ["lsof", "-Fn", "-p", pid],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            cwd, open_paths = parse_lsof_fn_output(lsof_out)
        except Exception:
            pass

        conv_id = find_conv_from_paths(open_paths)
        if not conv_id and ppid in cmd_by_pid:
            conv_id = find_conv_from_cmdline(cmd_by_pid[ppid][1])
        if not conv_id:
            conv_id = find_conv_from_cmdline(cmd)
        if not conv_id:
            continue

        panes[pane] = {
            "pane": int(pane),
            "conversation_id": conv_id,
            "cwd": cwd,
            "pid": int(pid),
        }
    return panes


# ── Windows (CIM Win32_Process + Tern WindowCx hints + SQLite WAL/URI) ─────


def _query_windows_agy_processes() -> list[dict[str, Any]]:
    """Query active `agy acp` processes on Windows via PowerShell CIM without flashing a console window."""
    ps_script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match 'agy' } | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    for shell_exe in ("powershell.exe", "pwsh.exe"):
        try:
            proc = subprocess.run(
                [shell_exe, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
                **kwargs,
            )
            if proc.returncode != 0:
                continue
            raw = proc.stdout.strip()
            if not raw:
                return []
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except FileNotFoundError:
            continue
        except Exception:
            break
    return []


def _is_likely_other_agent_hint(hint: dict[str, Any]) -> bool:
    cmd = str(hint.get("command") or "").lower()
    if not cmd:
        return False
    if "agy" in cmd or "antigravity" in cmd:
        return False
    # Exclude explicit non-agy worker commands (e.g. `omp`, `claude`, `codex`)
    for other in ("omp", "oh-my-pi", "claude", "codex", "opencode"):
        if other in cmd:
            return True
    return False


def discover_panes_windows(
    hints: list[dict[str, Any]],
    data_dir: Path,
    win_procs: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map live Tern ACP `agy` panes on Windows using WindowCx hints + Win32_Process + `.db` metadata."""
    panes: dict[str, dict[str, Any]] = {}
    if not hints:
        return panes

    procs = _query_windows_agy_processes() if win_procs is None else win_procs
    has_live_agy = any("agy" in str(p.get("CommandLine") or "").lower() for p in procs)
    claimed_convs: set[str] = set()

    # 1. Direct UUID match from pane title or command (`agy --conversation=<uuid>` / `tern acp` reopen)
    unresolved_hints: list[dict[str, Any]] = []
    for h in hints:
        if _is_likely_other_agent_hint(h):
            continue
        raw_pane = h.get("pane")
        if raw_pane is None or not str(raw_pane).isdigit():
            continue
        pane_str = str(int(raw_pane))
        cwd = str(h.get("cwd") or "")
        conv_id = find_conv_from_cmdline(str(h.get("title") or "")) or find_conv_from_cmdline(
            str(h.get("command") or "")
        )
        if conv_id and (data_dir / "brain" / conv_id).is_dir():
            claimed_convs.add(conv_id)
            panes[pane_str] = {
                "pane": int(pane_str),
                "conversation_id": conv_id,
                "cwd": cwd,
                "pid": 0,
            }
        else:
            unresolved_hints.append(h)

    if not unresolved_hints or not has_live_agy:
        return panes

    # 2. Match remaining live `agy` panes against `.db` workspace URIs (preferring active `.db-wal` + newest mtime)
    conv_dir = data_dir / "conversations"
    if not conv_dir.is_dir():
        return panes

    db_candidates: list[tuple[int, float, str, Path]] = []
    for db_file in conv_dir.glob("*.db"):
        m = UUID_RE.search(db_file.stem)
        if not m:
            continue
        cid = m.group(1).lower()
        if cid in claimed_convs:
            continue
        tpath = data_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if not tpath.is_file():
            continue
        has_wal = 1 if Path(f"{db_file}-wal").exists() else 0
        try:
            mtime = tpath.stat().st_mtime
        except OSError:
            mtime = 0.0
        db_candidates.append((has_wal, mtime, cid, db_file))

    db_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    # Inspect at most the 24 most recent conversations so probes stay <15ms
    db_candidates = db_candidates[:24]
    ws_cache: dict[str, set[str]] = {}

    for h in unresolved_hints:
        pane_str = str(int(h["pane"]))
        cwd = str(h.get("cwd") or "")
        want_key = normalize_workspace_key(cwd)
        if not want_key:
            continue
        for _has_wal, _mtime, cid, db_file in db_candidates:
            if cid in claimed_convs:
                continue
            if cid not in ws_cache:
                ws_cache[cid] = extract_workspace_keys_from_db(db_file)
            if _workspace_matches(want_key, ws_cache[cid]):
                claimed_convs.add(cid)
                panes[pane_str] = {
                    "pane": int(pane_str),
                    "conversation_id": cid,
                    "cwd": cwd,
                    "pid": 0,
                }
                break

    return panes


def discover_live_agy_panes(hints: list[dict[str, Any]], data_dir: Path) -> dict[str, dict[str, Any]]:
    if sys.platform == "win32":
        return discover_panes_windows(hints, data_dir)
    if sys.platform == "darwin":
        return discover_panes_macos()
    return discover_panes_linux()


# ── Transcript summarization ───────────────────────────────────────────────


def summarize_transcript_file(tpath: Path, conv_id: str) -> dict[str, Any] | None:
    if not tpath.is_file():
        return None

    turns: list[dict[str, Any]] = []
    total_input = 0
    total_cache_read = 0
    total_output = 0
    last_model = ""

    try:
        with tpath.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"PLANNER_RESPONSE"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "PLANNER_RESPONSE":
                    continue
                inp = int(obj.get("input_tokens") or 0)
                cread = int(obj.get("cache_read_tokens") or 0)
                out = int(obj.get("output_tokens") or 0)
                denom = inp + cread
                if denom <= 0 and out <= 0:
                    continue
                hit = (cread / denom) if denom > 0 else 0.0
                total_input += inp
                total_cache_read += cread
                total_output += out
                model = str(obj.get("model") or "")
                if model:
                    last_model = model
                turns.append(
                    {
                        "step": int(obj.get("step_index") or 0),
                        "model": model,
                        "input": inp,
                        "cache_read": cread,
                        "output": out,
                        "hit": round(hit, 4),
                        "at": str(obj.get("created_at") or ""),
                    }
                )
    except OSError:
        return None

    if not turns:
        return {
            "conversation_id": conv_id,
            "turn_count": 0,
            "last_epoch_ms": 0,
            "last_hit": 0.0,
            "total_hit": 0.0,
            "last_input": 0,
            "last_cache_read": 0,
            "last_output": 0,
            "total_input": 0,
            "total_cache_read": 0,
            "total_output": 0,
            "model": "",
            "series": [],
            "turns": [],
        }

    latest = turns[-1]
    last_epoch_ms = 0
    if latest["at"]:
        try:
            dt = datetime.fromisoformat(latest["at"].replace("Z", "+00:00"))
            last_epoch_ms = int(dt.timestamp() * 1000)
        except ValueError:
            last_epoch_ms = 0
    if last_epoch_ms <= 0:
        try:
            last_epoch_ms = int(tpath.stat().st_mtime * 1000)
        except OSError:
            last_epoch_ms = 0

    total_denom = total_input + total_cache_read
    total_hit = (total_cache_read / total_denom) if total_denom > 0 else 0.0
    return {
        "conversation_id": conv_id,
        "turn_count": len(turns),
        "last_epoch_ms": last_epoch_ms,
        "last_hit": latest["hit"],
        "total_hit": round(total_hit, 4),
        "last_input": latest["input"],
        "last_cache_read": latest["cache_read"],
        "last_output": latest["output"],
        "total_input": total_input,
        "total_cache_read": total_cache_read,
        "total_output": total_output,
        "model": last_model,
        "series": [t["hit"] for t in turns[-24:]],
        "turns": turns[-12:],
    }


def summarize_transcript(conv_id: str, data_dir: Path | None = None) -> dict[str, Any] | None:
    root = data_dir or antigravity_data_dir()
    tpath = root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    return summarize_transcript_file(tpath, conv_id)


def _read_stdin_hints() -> list[dict[str, Any]]:
    if sys.stdin.isatty():
        return []
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return []
        obj = json.loads(raw)
        if isinstance(obj, dict) and isinstance(obj.get("hints"), list):
            return [x for x in obj["hints"] if isinstance(x, dict)]
    except Exception:
        pass
    return []


def main() -> int:
    data_dir = antigravity_data_dir()
    hints = _read_stdin_hints()
    live = discover_live_agy_panes(hints, data_dir)
    out: dict[str, Any] = {}
    for pane_key, info in live.items():
        stats = summarize_transcript(info["conversation_id"], data_dir=data_dir)
        if stats is not None:
            stats["pane"] = info["pane"]
            stats["cwd"] = info["cwd"]
            out[pane_key] = stats
    print(json.dumps({"panes": out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
