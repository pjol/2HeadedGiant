#!/usr/bin/env python3
"""Manage LIBRARY.md and ARCHIVE.md for concurrent agent work."""

from __future__ import annotations

import argparse
import copy
import json
import os
import posixpath
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_BEGIN = "<!-- agent-work-coordinator-state"
STATE_END = "agent-work-coordinator-state -->"
DEFAULT_LIBRARY = "LIBRARY.md"
DEFAULT_ARCHIVE = "ARCHIVE.md"
STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": utc_now(),
        "implementations": {},
        "checkouts": {},
        "queues": {},
    }


def normalize_path(raw: str) -> str:
    path = raw.strip().replace("\\", "/")
    if not path:
        raise ValueError("empty path is not allowed")
    if path.startswith("/"):
        raise ValueError(f"absolute paths are not allowed: {raw}")
    path = posixpath.normpath(path)
    if path in {".", ""} or path == ".." or path.startswith("../"):
        raise ValueError(f"path must stay inside the repo: {raw}")
    return path


def normalize_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in paths:
        path = normalize_path(raw)
        if path not in seen:
            normalized.append(path)
            seen.add(path)
    return normalized


def slugify(text: str, limit: int = 36) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:limit].strip("-") or "work")


def make_work_id(goal: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"awc-{stamp}-{slugify(goal, 28)}-{secrets.token_hex(3)}"


def load_state(library_path: Path) -> dict[str, Any]:
    if not library_path.exists():
        return default_state()
    text = library_path.read_text()
    start = text.find(STATE_BEGIN)
    if start == -1:
        if text.strip():
            raise SystemExit(
                f"{library_path} exists but has no {STATE_BEGIN!r} block. "
                "Move or repair it before using the coordinator."
            )
        return default_state()
    body_start = start + len(STATE_BEGIN)
    end = text.find(STATE_END, body_start)
    if end == -1:
        raise SystemExit(f"{library_path} has an unterminated managed state block.")
    raw_json = text[body_start:end].strip()
    try:
        state = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{library_path} has invalid coordinator JSON: {exc}") from exc
    return normalize_state(state)


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("version", 1)
    state.setdefault("updated_at", utc_now())
    state.setdefault("implementations", {})
    state.setdefault("checkouts", {})
    state.setdefault("queues", {})
    if not isinstance(state["implementations"], dict):
        raise SystemExit("state.implementations must be an object")
    if not isinstance(state["checkouts"], dict):
        raise SystemExit("state.checkouts must be an object")
    if not isinstance(state["queues"], dict):
        raise SystemExit("state.queues must be an object")
    return state


def clean_and_rebuild(state: dict[str, Any]) -> None:
    implementations = state["implementations"]
    for work_id, impl in list(implementations.items()):
        impl.setdefault("id", work_id)
        impl.setdefault("agent", "agent")
        impl.setdefault("goal", "")
        impl.setdefault("started_at", utc_now())
        impl["planned_files"] = normalize_paths(list(impl.get("planned_files", [])))

    normalized_checkouts: dict[str, str] = {}
    for raw_path, owner in list(state["checkouts"].items()):
        path = normalize_path(raw_path)
        if owner not in implementations or path not in implementations[owner]["planned_files"]:
            continue
        normalized_checkouts[path] = owner
    state["checkouts"] = normalized_checkouts

    cleaned_queues: dict[str, list[str]] = {}
    for raw_path, raw_queue in state["queues"].items():
        path = normalize_path(raw_path)
        queue: list[str] = []
        seen: set[str] = set()
        for work_id in list(raw_queue):
            if work_id in seen:
                continue
            impl = implementations.get(work_id)
            if not impl or path not in impl["planned_files"]:
                continue
            if state["checkouts"].get(path) == work_id:
                continue
            queue.append(work_id)
            seen.add(work_id)
        if queue:
            cleaned_queues[path] = queue
    state["queues"] = cleaned_queues

    for impl in implementations.values():
        planned = impl["planned_files"]
        impl["checked_out"] = [path for path in planned if state["checkouts"].get(path) == impl["id"]]
        impl["queued"] = [path for path in planned if impl["id"] in state["queues"].get(path, [])]


def promote_queues(state: dict[str, Any]) -> None:
    clean_and_rebuild(state)
    for path in sorted(list(state["queues"].keys())):
        if path in state["checkouts"]:
            continue
        queue = state["queues"].get(path, [])
        while queue:
            next_id = queue.pop(0)
            if next_id in state["implementations"]:
                state["checkouts"][path] = next_id
                break
        if queue:
            state["queues"][path] = queue
        else:
            state["queues"].pop(path, None)
    clean_and_rebuild(state)


def queue_position(state: dict[str, Any], path: str, work_id: str) -> int | None:
    queue = state["queues"].get(path, [])
    if work_id not in queue:
        return None
    return queue.index(work_id) + 1


def format_list(items: list[str], empty: str = "_None._") -> str:
    if not items:
        return empty
    return "\n".join(f"  - `{item}`" for item in items)


def render_library(state: dict[str, Any]) -> str:
    clean_and_rebuild(state)
    state["updated_at"] = utc_now()
    lines: list[str] = [
        "# LIBRARY.md",
        "",
        "Central work-in-progress ledger for concurrent implementation.",
        "Use the agent-work-coordinator skill or `scripts/coordinator.py` to edit this file.",
        "",
        STATE_BEGIN,
        json.dumps(state, indent=2, sort_keys=True),
        STATE_END,
        "",
        "## Active Implementation Briefs",
        "",
    ]
    implementations = sorted(
        state["implementations"].values(),
        key=lambda impl: (impl.get("started_at", ""), impl.get("id", "")),
    )
    if not implementations:
        lines.extend(["_No active implementations._", ""])
    for impl in implementations:
        lines.extend(
            [
                f"### `{impl['id']}`",
                "",
                f"- Agent: {impl.get('agent', 'agent')}",
                f"- Started: {impl.get('started_at', '')}",
                f"- Goal: {impl.get('goal', '')}",
                "- Planned paths:",
                format_list(impl.get("planned_files", [])),
                "- Checked-out paths:",
                format_list(impl.get("checked_out", [])),
                "- Queued paths:",
            ]
        )
        queued_lines: list[str] = []
        for path in impl.get("queued", []):
            position = queue_position(state, path, impl["id"])
            suffix = f" ({position})" if position is not None else ""
            queued_lines.append(f"  - `{path}{suffix}`")
        lines.append("\n".join(queued_lines) if queued_lines else "_None._")
        lines.append("")

    lines.extend(["## File Checkouts", ""])
    if not state["checkouts"]:
        lines.extend(["_No checked-out files._", ""])
    else:
        for path, work_id in sorted(state["checkouts"].items()):
            goal = state["implementations"].get(work_id, {}).get("goal", "")
            lines.append(f"- `{path}` -> `{work_id}` ({goal})")
        lines.append("")

    lines.extend(["## Queues", ""])
    visible_queues = {path: queue for path, queue in state["queues"].items() if queue}
    if not visible_queues:
        lines.extend(["_No queued files._", ""])
    else:
        for path, queue in sorted(visible_queues.items()):
            lines.extend([f"### `{path}`", ""])
            for index, work_id in enumerate(queue, start=1):
                goal = state["implementations"].get(work_id, {}).get("goal", "")
                lines.append(f"- ({index}) `{work_id}` - {goal}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_library(library_path: Path, state: dict[str, Any]) -> None:
    library_path.write_text(render_library(state))


def ensure_archive(archive_path: Path) -> None:
    if not archive_path.exists():
        archive_path.write_text("# ARCHIVE.md\n\nCompleted implementation briefs.\n")


def archive_entry(impl: dict[str, Any], completed_at: str) -> str:
    lines = [
        "",
        f"## Completed {completed_at} - `{impl['id']}`",
        "",
        f"- Agent: {impl.get('agent', 'agent')}",
        f"- Started: {impl.get('started_at', '')}",
        f"- Completed: {completed_at}",
        f"- Goal: {impl.get('goal', '')}",
        "- Planned paths:",
        format_list(impl.get("planned_files", [])),
        "- Checked-out paths at completion:",
        format_list(impl.get("checked_out", [])),
        "- Queued paths at completion:",
        format_list(impl.get("queued", [])),
        "",
    ]
    return "\n".join(lines)


def append_archive(archive_path: Path, impl: dict[str, Any], completed_at: str) -> None:
    ensure_archive(archive_path)
    with archive_path.open("a") as archive:
        archive.write(archive_entry(impl, completed_at))


def tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in STOPWORDS}


def duplicate_candidates(
    state: dict[str, Any],
    work_id: str | None,
    goal: str | None,
    files: list[str] | None,
) -> list[dict[str, Any]]:
    if not goal and not files:
        return []
    planned = set(files or [])
    goal_tokens = tokens(goal or "")
    normalized_goal = " ".join(sorted(goal_tokens))
    candidates: list[dict[str, Any]] = []
    for other_id, impl in state["implementations"].items():
        if work_id and other_id == work_id:
            continue
        other_files = set(impl.get("planned_files", []))
        other_tokens = tokens(impl.get("goal", ""))
        file_overlap = sorted(planned & other_files)
        same_goal = bool(normalized_goal and normalized_goal == " ".join(sorted(other_tokens)))
        token_overlap = 0.0
        if goal_tokens and other_tokens:
            token_overlap = len(goal_tokens & other_tokens) / len(goal_tokens | other_tokens)
        if same_goal or (file_overlap and token_overlap >= 0.35):
            candidates.append(
                {
                    "id": other_id,
                    "goal": impl.get("goal", ""),
                    "agent": impl.get("agent", "agent"),
                    "started_at": impl.get("started_at", ""),
                    "overlapping_files": file_overlap,
                    "similarity": round(token_overlap, 2),
                }
            )
    return candidates


def render_status(state: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    clean_and_rebuild(state)
    lines: list[str] = []
    active = state["implementations"]
    if active:
        lines.append("Active implementations:")
        for work_id, impl in sorted(active.items()):
            checked = ", ".join(impl.get("checked_out", [])) or "none"
            queued = []
            for path in impl.get("queued", []):
                position = queue_position(state, path, work_id)
                queued.append(f"{path} ({position})" if position else path)
            lines.append(f"- {work_id}: {impl.get('goal', '')}")
            lines.append(f"  checked out: {checked}")
            lines.append(f"  queued: {', '.join(queued) or 'none'}")
    else:
        lines.append("No active implementations.")
    if candidates:
        lines.append("")
        lines.append("Potential duplicate briefs:")
        for candidate in candidates:
            overlap = ", ".join(candidate["overlapping_files"]) or "none"
            lines.append(f"- {candidate['id']}: {candidate['goal']} (overlap: {overlap})")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    library_path = Path(args.library)
    should_create = not library_path.exists()
    state = load_state(library_path)
    promote_queues(state)
    if should_create:
        save_library(library_path, state)
    files = normalize_paths(args.files or []) if args.files else None
    candidates = duplicate_candidates(state, args.id, args.goal, files)
    if args.json:
        print(json.dumps({"state": state, "duplicate_candidates": candidates}, indent=2, sort_keys=True))
    else:
        print(render_status(state, candidates))
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    files = normalize_paths(args.files)
    work_id = args.id or make_work_id(args.goal)
    library_path = Path(args.library)
    state = load_state(library_path)
    promote_queues(state)

    implementations = state["implementations"]
    if work_id in implementations:
        impl = implementations[work_id]
        old_files = set(impl.get("planned_files", []))
        new_files = set(files)
        for path, owner in list(state["checkouts"].items()):
            if owner == work_id and path not in new_files:
                del state["checkouts"][path]
        for path, queue in list(state["queues"].items()):
            state["queues"][path] = [queued_id for queued_id in queue if queued_id != work_id or path in new_files]
            if not state["queues"][path]:
                del state["queues"][path]
        impl["agent"] = args.agent
        impl["goal"] = args.goal
        impl["planned_files"] = files
        if old_files != new_files:
            impl["updated_at"] = utc_now()
    else:
        impl = {
            "id": work_id,
            "agent": args.agent,
            "goal": args.goal,
            "started_at": utc_now(),
            "planned_files": files,
            "checked_out": [],
            "queued": [],
        }
        implementations[work_id] = impl

    for path in files:
        owner = state["checkouts"].get(path)
        queue = state["queues"].setdefault(path, [])
        if owner == work_id:
            continue
        if owner and owner != work_id:
            if work_id not in queue:
                queue.append(work_id)
            continue
        if queue:
            if queue[0] == work_id:
                queue.pop(0)
                state["checkouts"][path] = work_id
            elif work_id not in queue:
                queue.append(work_id)
        else:
            state["checkouts"][path] = work_id
        if not state["queues"].get(path):
            state["queues"].pop(path, None)

    promote_queues(state)
    save_library(library_path, state)
    result_impl = state["implementations"][work_id]
    result = {
        "id": work_id,
        "goal": result_impl["goal"],
        "checked_out": result_impl.get("checked_out", []),
        "queued": [
            {"path": path, "position": queue_position(state, path, work_id)}
            for path in result_impl.get("queued", [])
        ],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    library_path = Path(args.library)
    archive_path = Path(args.archive)
    state = load_state(library_path)
    promote_queues(state)
    impl = state["implementations"].get(args.id)
    if not impl:
        raise SystemExit(f"No active implementation with id {args.id!r}.")
    archived_impl = copy.deepcopy(impl)
    completed_at = utc_now()

    for path, owner in list(state["checkouts"].items()):
        if owner == args.id:
            del state["checkouts"][path]
    for path, queue in list(state["queues"].items()):
        state["queues"][path] = [work_id for work_id in queue if work_id != args.id]
        if not state["queues"][path]:
            del state["queues"][path]
    del state["implementations"][args.id]
    promote_queues(state)
    save_library(library_path, state)
    append_archive(archive_path, archived_impl, completed_at)
    print(json.dumps({"id": args.id, "completed_at": completed_at, "archived": str(archive_path)}, indent=2))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    library_path = Path(args.library)
    initial_queued: set[str] | None = None
    while True:
        if args.pull:
            subprocess.run(["git", "pull", "--ff-only"], check=False)
        state = load_state(library_path)
        promote_queues(state)
        impl = state["implementations"].get(args.id)
        if not impl:
            print(json.dumps({"id": args.id, "status": "not-active"}, indent=2))
            return 0
        queued = set(impl.get("queued", []))
        checked = set(impl.get("checked_out", []))
        if initial_queued is None:
            initial_queued = set(queued)
        newly_available = sorted(initial_queued & checked)
        if not queued or newly_available:
            print(
                json.dumps(
                    {
                        "id": args.id,
                        "checked_out": impl.get("checked_out", []),
                        "queued": [
                            {"path": path, "position": queue_position(state, path, args.id)}
                            for path in impl.get("queued", [])
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coordinate concurrent agent work in LIBRARY.md.")
    parser.add_argument("--library", default=DEFAULT_LIBRARY, help="Path to LIBRARY.md")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show active work and duplicate candidates")
    status.add_argument("--id", help="Current work id to exclude from duplicate checks")
    status.add_argument("--goal", help="Requested work summary for duplicate checks")
    status.add_argument("--files", nargs="*", help="Planned repo-relative files for duplicate checks")
    status.add_argument("--json", action="store_true", help="Print machine-readable status")
    status.set_defaults(func=cmd_status)

    request = subparsers.add_parser("request", help="Request checkouts or queue unavailable files")
    request.add_argument("--id", help="Stable work id. Generated if omitted.")
    request.add_argument("--agent", default=os.environ.get("USER", "agent"), help="Agent or user label")
    request.add_argument("--goal", required=True, help="Brief implementation goal")
    request.add_argument("--files", nargs="+", required=True, help="Repo-relative files to reserve")
    request.set_defaults(func=cmd_request)

    finish = subparsers.add_parser("finish", help="Release active work and append it to ARCHIVE.md")
    finish.add_argument("--archive", default=DEFAULT_ARCHIVE, help="Path to ARCHIVE.md")
    finish.add_argument("--id", required=True, help="Work id to finish")
    finish.set_defaults(func=cmd_finish)

    wait = subparsers.add_parser("wait", help="Poll until a queued file is promoted")
    wait.add_argument("--id", required=True, help="Work id to wait for")
    wait.add_argument("--interval", type=float, default=10.0, help="Polling interval in seconds")
    wait.add_argument("--pull", action="store_true", help="Run git pull --ff-only before each check")
    wait.set_defaults(func=cmd_wait)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
