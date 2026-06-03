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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_BEGIN = "<!-- agent-work-coordinator-state"
STATE_END = "agent-work-coordinator-state -->"
DEFAULT_LIBRARY = "LIBRARY.md"
DEFAULT_ARCHIVE = "ARCHIVE.md"
DEFAULT_STALE_SECONDS = 120
MAX_CHECKINS = 20
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


def parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def age_seconds(raw: str | None, now: datetime | None = None) -> int | None:
    timestamp = parse_timestamp(raw)
    if not timestamp:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - timestamp).total_seconds()))


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


def make_agent_uuid() -> str:
    return str(uuid.uuid4())


def normalize_agent_uuid(raw: str) -> str:
    try:
        return str(uuid.UUID(raw.strip()))
    except ValueError as exc:
        raise ValueError(f"agent UUID must be a valid UUID: {raw}") from exc


def agent_display(impl: dict[str, Any]) -> str:
    label = impl.get("agent", "agent")
    agent_uuid = impl.get("agent_uuid", "")
    if not agent_uuid:
        return label
    return f"{label} [{agent_uuid}]"


def checkin_age(impl: dict[str, Any]) -> int | None:
    return age_seconds(impl.get("last_checkin_at") or impl.get("started_at"))


def is_stale(impl: dict[str, Any], threshold_seconds: int) -> bool:
    age = checkin_age(impl)
    return age is not None and age >= threshold_seconds


def append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def git_result(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def remote_status(fetch: bool = True, cwd: Path | None = None) -> dict[str, Any]:
    inside = git_result(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    if not inside or inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"state": "not-git"}

    result: dict[str, Any] = {"state": "unknown"}
    if fetch:
        fetched = git_result(["fetch", "--quiet"], cwd=cwd)
        if not fetched or fetched.returncode != 0:
            result["fetch_error"] = (fetched.stderr.strip() if fetched else "git executable not found")

    upstream = git_result(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd)
    if not upstream or upstream.returncode != 0:
        result["state"] = "no-upstream"
        return result
    result["upstream"] = upstream.stdout.strip()

    local = git_result(["rev-parse", "HEAD"], cwd=cwd)
    remote = git_result(["rev-parse", "@{u}"], cwd=cwd)
    if not local or not remote or local.returncode != 0 or remote.returncode != 0:
        result["state"] = "unknown"
        return result
    local_sha = local.stdout.strip()
    remote_sha = remote.stdout.strip()
    result["local"] = local_sha
    result["remote"] = remote_sha
    if local_sha == remote_sha:
        result["state"] = "up-to-date"
        return result

    local_behind = git_result(["merge-base", "--is-ancestor", "HEAD", "@{u}"], cwd=cwd)
    remote_behind = git_result(["merge-base", "--is-ancestor", "@{u}", "HEAD"], cwd=cwd)
    if local_behind and local_behind.returncode == 0:
        result["state"] = "behind"
    elif remote_behind and remote_behind.returncode == 0:
        result["state"] = "ahead"
    else:
        result["state"] = "diverged"
    return result


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
        impl.setdefault("agent_uuid", "")
        impl.setdefault("goal", "")
        impl.setdefault("started_at", utc_now())
        impl.setdefault("last_checkin_at", impl.get("started_at", utc_now()))
        impl.setdefault("progress_note", "")
        impl["checkins"] = list(impl.get("checkins", []))[-MAX_CHECKINS:]
        impl["planned_files"] = normalize_paths(list(impl.get("planned_files", [])))
        impl["completed_files"] = normalize_paths(list(impl.get("completed_files", [])))
        impl["completed_files"] = [path for path in impl["completed_files"] if path in impl["planned_files"]]
        impl["bumped_files"] = normalize_paths(list(impl.get("bumped_files", [])))
        impl["bumped_files"] = [
            path
            for path in impl["bumped_files"]
            if path in impl["planned_files"] and path not in impl["completed_files"]
        ]

    normalized_checkouts: dict[str, str] = {}
    for raw_path, owner in list(state["checkouts"].items()):
        path = normalize_path(raw_path)
        if owner not in implementations or path not in implementations[owner]["planned_files"]:
            continue
        if path in implementations[owner].get("completed_files", []):
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
            if path in impl.get("completed_files", []):
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
        completed = set(impl.get("completed_files", []))
        impl["checked_out"] = [
            path for path in planned if path not in completed and state["checkouts"].get(path) == impl["id"]
        ]
        impl["queued"] = [
            path for path in planned if path not in completed and impl["id"] in state["queues"].get(path, [])
        ]
        impl["bumped_files"] = [path for path in impl.get("bumped_files", []) if path not in impl["checked_out"]]


def format_checkins(checkins: list[dict[str, Any]], empty: str = "_None._") -> str:
    if not checkins:
        return empty
    lines: list[str] = []
    for checkin in checkins[-5:]:
        at = checkin.get("at", "")
        note = checkin.get("note", "")
        files = ", ".join(checkin.get("files", []))
        suffix = f" (`{files}`)" if files else ""
        lines.append(f"  - {at}: {note}{suffix}")
    return "\n".join(lines)


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


def preempt_stale_blockers(
    state: dict[str, Any],
    requester_id: str,
    files: list[str] | None,
    threshold_seconds: int,
) -> list[dict[str, Any]]:
    clean_and_rebuild(state)
    implementations = state["implementations"]
    requester = implementations.get(requester_id)
    if not requester:
        raise SystemExit(f"No active implementation with id {requester_id!r}.")

    target_paths = normalize_paths(files) if files else list(requester.get("queued", []))
    preemptions: list[dict[str, Any]] = []
    now = utc_now()
    for path in target_paths:
        queue = state["queues"].get(path, [])
        if requester_id not in queue:
            continue
        owner_id = state["checkouts"].get(path)
        if not owner_id or owner_id == requester_id:
            continue
        owner = implementations.get(owner_id)
        if not owner or not is_stale(owner, threshold_seconds):
            continue

        del state["checkouts"][path]
        queue = [work_id for work_id in queue if work_id != owner_id]
        if path in owner.get("planned_files", []) and path not in owner.get("completed_files", []):
            queue.append(owner_id)
            bumped = list(owner.get("bumped_files", []))
            append_unique(bumped, path)
            owner["bumped_files"] = bumped
            owner["last_bumped_at"] = now
        state["queues"][path] = queue
        preemptions.append(
            {
                "path": path,
                "stale_owner": owner_id,
                "stale_owner_agent": agent_display(owner),
                "last_checkin_at": owner.get("last_checkin_at", ""),
                "age_seconds": checkin_age(owner),
            }
        )

    promote_queues(state)
    for preemption in preemptions:
        preemption["new_owner"] = state["checkouts"].get(preemption["path"])
    return preemptions


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
                f"- Agent: {agent_display(impl)}",
                f"- Started: {impl.get('started_at', '')}",
                f"- Last check-in: {impl.get('last_checkin_at', '')}",
                f"- Goal: {impl.get('goal', '')}",
                f"- Progress: {impl.get('progress_note', '') or '_None._'}",
                "- Planned paths:",
                format_list(impl.get("planned_files", [])),
                "- Completed paths:",
                format_list(impl.get("completed_files", [])),
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
        lines.extend(["- Bumped paths:", format_list(impl.get("bumped_files", [])), "- Recent check-ins:"])
        lines.append(format_checkins(impl.get("checkins", [])))
        lines.append("")

    lines.extend(["## File Checkouts", ""])
    if not state["checkouts"]:
        lines.extend(["_No checked-out files._", ""])
    else:
        for path, work_id in sorted(state["checkouts"].items()):
            impl = state["implementations"].get(work_id, {})
            goal = impl.get("goal", "")
            lines.append(f"- `{path}` -> `{work_id}` by {agent_display(impl)} ({goal})")
        lines.append("")

    lines.extend(["## Queues", ""])
    visible_queues = {path: queue for path, queue in state["queues"].items() if queue}
    if not visible_queues:
        lines.extend(["_No queued files._", ""])
    else:
        for path, queue in sorted(visible_queues.items()):
            lines.extend([f"### `{path}`", ""])
            for index, work_id in enumerate(queue, start=1):
                impl = state["implementations"].get(work_id, {})
                goal = impl.get("goal", "")
                lines.append(f"- ({index}) `{work_id}` by {agent_display(impl)} - {goal}")
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
        f"- Agent: {agent_display(impl)}",
        f"- Started: {impl.get('started_at', '')}",
        f"- Last check-in: {impl.get('last_checkin_at', '')}",
        f"- Completed: {completed_at}",
        f"- Goal: {impl.get('goal', '')}",
        f"- Final progress: {impl.get('progress_note', '') or '_None._'}",
        "- Planned paths:",
        format_list(impl.get("planned_files", [])),
        "- Completed paths:",
        format_list(impl.get("completed_files", [])),
        "- Checked-out paths at completion:",
        format_list(impl.get("checked_out", [])),
        "- Queued paths at completion:",
        format_list(impl.get("queued", [])),
        "- Bumped paths at completion:",
        format_list(impl.get("bumped_files", [])),
        "- Recent check-ins:",
        format_checkins(impl.get("checkins", [])),
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
                    "agent_uuid": impl.get("agent_uuid", ""),
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
            last_checkin = impl.get("last_checkin_at", "")
            last_checkin_age = checkin_age(impl)
            age_text = f"{last_checkin_age}s ago" if last_checkin_age is not None else "unknown age"
            completed = ", ".join(impl.get("completed_files", [])) or "none"
            checked = ", ".join(impl.get("checked_out", [])) or "none"
            bumped = ", ".join(impl.get("bumped_files", [])) or "none"
            queued = []
            for path in impl.get("queued", []):
                position = queue_position(state, path, work_id)
                queued.append(f"{path} ({position})" if position else path)
            lines.append(f"- {work_id} by {agent_display(impl)}: {impl.get('goal', '')}")
            lines.append(f"  last check-in: {last_checkin or 'none'} ({age_text})")
            lines.append(f"  progress: {impl.get('progress_note', '') or 'none'}")
            lines.append(f"  completed: {completed}")
            lines.append(f"  checked out: {checked}")
            lines.append(f"  queued: {', '.join(queued) or 'none'}")
            lines.append(f"  bumped: {bumped}")
    else:
        lines.append("No active implementations.")
    if candidates:
        lines.append("")
        lines.append("Potential duplicate briefs:")
        for candidate in candidates:
            overlap = ", ".join(candidate["overlapping_files"]) or "none"
            candidate_agent = candidate["agent"]
            if candidate["agent_uuid"]:
                candidate_agent = f"{candidate_agent} [{candidate['agent_uuid']}]"
            lines.append(f"- {candidate['id']} by {candidate_agent}: {candidate['goal']} (overlap: {overlap})")
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
    requested_agent_uuid = normalize_agent_uuid(args.agent_uuid) if args.agent_uuid else None
    library_path = Path(args.library)
    state = load_state(library_path)
    promote_queues(state)

    implementations = state["implementations"]
    if work_id in implementations:
        impl = implementations[work_id]
        old_files = set(impl.get("planned_files", []))
        planned_files = normalize_paths(list(impl.get("planned_files", [])) + files)
        new_files = set(planned_files)
        for path, owner in list(state["checkouts"].items()):
            if owner == work_id and path not in new_files:
                del state["checkouts"][path]
        for path, queue in list(state["queues"].items()):
            state["queues"][path] = [queued_id for queued_id in queue if queued_id != work_id or path in new_files]
            if not state["queues"][path]:
                del state["queues"][path]
        impl["agent"] = args.agent
        impl["agent_uuid"] = requested_agent_uuid or impl.get("agent_uuid") or make_agent_uuid()
        impl["goal"] = args.goal
        impl["planned_files"] = planned_files
        impl["completed_files"] = [path for path in impl.get("completed_files", []) if path in new_files]
        if old_files != new_files:
            impl["updated_at"] = utc_now()
    else:
        impl = {
            "id": work_id,
            "agent": args.agent,
            "agent_uuid": requested_agent_uuid or make_agent_uuid(),
            "goal": args.goal,
            "started_at": utc_now(),
            "last_checkin_at": utc_now(),
            "progress_note": "checkout requested",
            "checkins": [
                {
                    "at": utc_now(),
                    "note": "checkout requested",
                    "files": files,
                }
            ],
            "planned_files": files,
            "completed_files": [],
            "bumped_files": [],
            "checked_out": [],
            "queued": [],
        }
        implementations[work_id] = impl

    for path in files:
        if path in set(impl.get("completed_files", [])):
            continue
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
        "agent": result_impl["agent"],
        "agent_uuid": result_impl["agent_uuid"],
        "goal": result_impl["goal"],
        "last_checkin_at": result_impl.get("last_checkin_at", ""),
        "completed_files": result_impl.get("completed_files", []),
        "bumped_files": result_impl.get("bumped_files", []),
        "checked_out": result_impl.get("checked_out", []),
        "queued": [
            {"path": path, "position": queue_position(state, path, work_id)}
            for path in result_impl.get("queued", [])
        ],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_checkin(args: argparse.Namespace) -> int:
    library_path = Path(args.library)
    current_remote_status = remote_status(fetch=not args.skip_fetch, cwd=library_path.resolve().parent)
    if current_remote_status.get("state") in {"behind", "diverged"} and not args.allow_remote_moved:
        print(
            json.dumps(
                {
                    "status": "remote-moved",
                    "remote": current_remote_status,
                    "action": "Sync the branch, inspect LIBRARY.md for bumped files, then rerun checkin.",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    files = normalize_paths(args.files or []) if args.files else []
    state = load_state(library_path)
    promote_queues(state)
    impl = state["implementations"].get(args.id)
    if not impl:
        raise SystemExit(f"No active implementation with id {args.id!r}.")
    planned = set(impl.get("planned_files", []))
    for path in files:
        if path not in planned:
            raise SystemExit(f"{path!r} is not planned for implementation {args.id!r}.")

    now = utc_now()
    impl["last_checkin_at"] = now
    impl["progress_note"] = args.note
    checkins = list(impl.get("checkins", []))
    checkins.append(
        {
            "at": now,
            "note": args.note,
            "files": files,
            "checked_out": impl.get("checked_out", []),
            "queued": impl.get("queued", []),
            "bumped": impl.get("bumped_files", []),
            "remote_state": current_remote_status.get("state", "unknown"),
        }
    )
    impl["checkins"] = checkins[-MAX_CHECKINS:]
    impl["updated_at"] = now
    promote_queues(state)
    save_library(library_path, state)
    result_impl = state["implementations"][args.id]
    bumped = result_impl.get("bumped_files", [])
    print(
        json.dumps(
            {
                "id": args.id,
                "agent": result_impl.get("agent", "agent"),
                "agent_uuid": result_impl.get("agent_uuid", ""),
                "last_checkin_at": result_impl.get("last_checkin_at", ""),
                "progress_note": result_impl.get("progress_note", ""),
                "completed_files": result_impl.get("completed_files", []),
                "checked_out": result_impl.get("checked_out", []),
                "queued": [
                    {"path": path, "position": queue_position(state, path, args.id)}
                    for path in result_impl.get("queued", [])
                ],
                "bumped_files": bumped,
                "remote": current_remote_status,
                "action": (
                    "Discard local changes for bumped files and wait for normal queue promotion."
                    if bumped
                    else "Continue work on checked-out files."
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    files = normalize_paths(args.files)
    library_path = Path(args.library)
    state = load_state(library_path)
    promote_queues(state)
    impl = state["implementations"].get(args.id)
    if not impl:
        raise SystemExit(f"No active implementation with id {args.id!r}.")

    planned = set(impl.get("planned_files", []))
    completed = list(impl.get("completed_files", []))
    completed_set = set(completed)
    released: list[str] = []
    already_completed: list[str] = []

    for path in files:
        if path not in planned:
            raise SystemExit(f"{path!r} is not planned for implementation {args.id!r}.")
        if path in completed_set:
            already_completed.append(path)
            continue
        owner = state["checkouts"].get(path)
        if owner != args.id:
            raise SystemExit(f"{path!r} is not checked out by implementation {args.id!r}.")
        del state["checkouts"][path]
        for queued_path, queue in list(state["queues"].items()):
            if queued_path == path:
                state["queues"][queued_path] = [work_id for work_id in queue if work_id != args.id]
                if not state["queues"][queued_path]:
                    del state["queues"][queued_path]
        completed.append(path)
        completed_set.add(path)
        released.append(path)

    impl["completed_files"] = completed
    impl["updated_at"] = utc_now()
    impl["last_checkin_at"] = impl["updated_at"]
    impl["progress_note"] = f"released completed files: {', '.join(released) or 'none'}"
    checkins = list(impl.get("checkins", []))
    checkins.append(
        {
            "at": impl["updated_at"],
            "note": impl["progress_note"],
            "files": released,
            "checked_out": impl.get("checked_out", []),
            "queued": impl.get("queued", []),
            "bumped": impl.get("bumped_files", []),
        }
    )
    impl["checkins"] = checkins[-MAX_CHECKINS:]
    promote_queues(state)
    save_library(library_path, state)
    result_impl = state["implementations"][args.id]
    print(
        json.dumps(
            {
                "id": args.id,
                "released": released,
                "already_completed": already_completed,
                "completed_files": result_impl.get("completed_files", []),
                "checked_out": result_impl.get("checked_out", []),
                "queued": [
                    {"path": path, "position": queue_position(state, path, args.id)}
                    for path in result_impl.get("queued", [])
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_preempt_stale(args: argparse.Namespace) -> int:
    files = normalize_paths(args.files or []) if args.files else None
    library_path = Path(args.library)
    state = load_state(library_path)
    promote_queues(state)
    preemptions = preempt_stale_blockers(state, args.id, files, args.threshold_seconds)
    save_library(library_path, state)
    impl = state["implementations"].get(args.id, {})
    print(
        json.dumps(
            {
                "id": args.id,
                "threshold_seconds": args.threshold_seconds,
                "preemptions": preemptions,
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


def cmd_finish(args: argparse.Namespace) -> int:
    library_path = Path(args.library)
    archive_path = Path(args.archive)
    state = load_state(library_path)
    promote_queues(state)
    impl = state["implementations"].get(args.id)
    if not impl:
        raise SystemExit(f"No active implementation with id {args.id!r}.")
    completed_at = utc_now()

    completed = list(impl.get("completed_files", []))
    completed_set = set(completed)
    for path, owner in list(state["checkouts"].items()):
        if owner == args.id:
            if path not in completed_set:
                completed.append(path)
                completed_set.add(path)
            del state["checkouts"][path]
    for path, queue in list(state["queues"].items()):
        state["queues"][path] = [work_id for work_id in queue if work_id != args.id]
        if not state["queues"][path]:
            del state["queues"][path]
    impl["completed_files"] = completed
    impl["last_checkin_at"] = utc_now()
    impl["progress_note"] = "finished implementation"
    checkins = list(impl.get("checkins", []))
    checkins.append(
        {
            "at": impl["last_checkin_at"],
            "note": impl["progress_note"],
            "files": completed,
            "checked_out": [],
            "queued": impl.get("queued", []),
            "bumped": impl.get("bumped_files", []),
        }
    )
    impl["checkins"] = checkins[-MAX_CHECKINS:]
    promote_queues(state)
    archived_impl = copy.deepcopy(impl)
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
        preemptions: list[dict[str, Any]] = []
        if args.preempt_stale:
            preemptions = preempt_stale_blockers(state, args.id, None, args.threshold_seconds)
            if preemptions:
                save_library(library_path, state)
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
                        "agent": impl.get("agent", "agent"),
                        "agent_uuid": impl.get("agent_uuid", ""),
                        "completed_files": impl.get("completed_files", []),
                        "bumped_files": impl.get("bumped_files", []),
                        "checked_out": impl.get("checked_out", []),
                        "queued": [
                            {"path": path, "position": queue_position(state, path, args.id)}
                            for path in impl.get("queued", [])
                        ],
                        "preemptions": preemptions,
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
    request.add_argument("--agent-uuid", help="Stable UUID for this agent instance. Generated if omitted.")
    request.add_argument("--goal", required=True, help="Brief implementation goal")
    request.add_argument("--files", nargs="+", required=True, help="Repo-relative files to reserve")
    request.set_defaults(func=cmd_request)

    checkin = subparsers.add_parser("checkin", help="Record progress and verify remote/check-out state")
    checkin.add_argument("--id", required=True, help="Work id checking in")
    checkin.add_argument("--note", required=True, help="Short progress note")
    checkin.add_argument("--files", nargs="*", help="Planned files this note concerns")
    checkin.add_argument("--skip-fetch", action="store_true", help="Do not fetch remote before checking in")
    checkin.add_argument(
        "--allow-remote-moved",
        action="store_true",
        help="Write the checkin even if the upstream branch has moved",
    )
    checkin.set_defaults(func=cmd_checkin)

    release = subparsers.add_parser("release", help="Release completed checked-out files and promote queues")
    release.add_argument("--id", required=True, help="Work id releasing files")
    release.add_argument("--files", nargs="+", required=True, help="Checked-out files completed by this work id")
    release.set_defaults(func=cmd_release)

    preempt = subparsers.add_parser("preempt-stale", help="Move stale checkout owners to the back of queues")
    preempt.add_argument("--id", required=True, help="Queued work id requesting stale preemption")
    preempt.add_argument("--files", nargs="*", help="Queued files to consider. Defaults to all queued files.")
    preempt.add_argument(
        "--threshold-seconds",
        type=int,
        default=DEFAULT_STALE_SECONDS,
        help="Seconds since last checkin before a checkout is stale",
    )
    preempt.set_defaults(func=cmd_preempt_stale)

    finish = subparsers.add_parser("finish", help="Release active work and append it to ARCHIVE.md")
    finish.add_argument("--archive", default=DEFAULT_ARCHIVE, help="Path to ARCHIVE.md")
    finish.add_argument("--id", required=True, help="Work id to finish")
    finish.set_defaults(func=cmd_finish)

    wait = subparsers.add_parser("wait", help="Poll until a queued file is promoted")
    wait.add_argument("--id", required=True, help="Work id to wait for")
    wait.add_argument("--interval", type=float, default=10.0, help="Polling interval in seconds")
    wait.add_argument("--pull", action="store_true", help="Run git pull --ff-only before each check")
    wait.add_argument("--preempt-stale", action="store_true", help="Preempt stale checkout owners while waiting")
    wait.add_argument(
        "--threshold-seconds",
        type=int,
        default=DEFAULT_STALE_SECONDS,
        help="Seconds since last checkin before a checkout is stale",
    )
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
