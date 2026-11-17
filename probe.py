#!/usr/bin/env python3
"""probe.py — Map live `agy acp` Tern panes to Antigravity transcripts (Linux & macOS) and compute prompt-cache metrics."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
TERN_PANE_ENV_RE = re.compile(r"(?:^|\s)TERN_PANE=(\d+)(?:\s|$)")


def antigravity_data_dir() -> Path:
    """Resolve Antigravity CLI state directory (`~/.gemini/antigravity-cli` by default)."""
    override = os.environ.get("ANTIGRAVITY_CLI_HOME")
    if override:
        return Path(override).expanduser()
    gemini_home = os.environ.get("GEMINI_CLI_HOME")
    if gemini_home:
        return Path(gemini_home).expanduser() / "antigravity-cli"
    return Path.home() / ".gemini" / "antigravity-cli"


def find_conv_from_cmdline(cmd: str) -> str | None:
    for flag in ("--resume", "--conversation"):
        idx = cmd.find(flag)
        if idx >= 0:
            m = UUID_RE.search(cmd[idx:])
            if m:
                return m.group(1).lower()
    return None


def find_conv_from_paths(paths: list[str]) -> str | None:
    for target in paths:
        if "/conversations/" in target and target.endswith(".db"):
            m = UUID_RE.search(Path(target).stem)
            if m:
                return m.group(1).lower()
    return None


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


def discover_live_agy_panes() -> dict[str, dict[str, Any]]:
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


def main() -> int:
    data_dir = antigravity_data_dir()
    live = discover_live_agy_panes()
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
