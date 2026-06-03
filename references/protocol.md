# Agent Work Coordinator Protocol

## Managed Files

`LIBRARY.md` is the active ledger. `ARCHIVE.md` is append-only history for completed implementations.

`LIBRARY.md` contains a hidden JSON block delimited by:

```markdown
<!-- agent-work-coordinator-state
...
agent-work-coordinator-state -->
```

Treat that block as the source of truth. The visible markdown sections are rendered from it so humans can scan:

- Active implementation briefs
- File checkouts
- Queues

Prefer `scripts/coordinator.py` instead of hand-editing the block.

## Repository Context Note

The repository should contain a durable note telling future agents to download and use this skill from the repo before collaborative implementation work. Put the note where the project already stores agent or contributor guidance. Good candidates include `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`, `.codex/` instructions, `CONTRIBUTING.md`, or a root markdown file that agents are expected to read.

If no guidance file exists, create `AGENTS.md` at the repository root unless the repo has a clearer convention. If `LIBRARY.md` already exists, reserve the note file through the normal checkout process before editing it. If bootstrapping the protocol for the first time, add the note and initial `LIBRARY.md` in the same coordination commit.

## Work Ids

Use stable ids for the entire prompt, for example `awc-20260602-fix-login-state`. Reuse the same id when a checkout push races and must be retried. Do not generate a new id for the same implementation unless the original id was never pushed and never archived.

## Agent Instance Ids

Human-readable agent labels are not unique. `scripts/coordinator.py request` generates an `agent_uuid` for each implementation and stores it beside the label. Rendered checkouts and queues should show both the work id and `agent label [agent_uuid]`.

When rerunning `request` for an implementation that already exists in `LIBRARY.md`, preserve the existing `agent_uuid`. Use `--agent-uuid` only when retrying a checkout attempt where the UUID was generated and captured locally but not yet persisted because the push raced.

## Duplicate Work Check

Before reserving files, read all active briefs. Compare the user's requested outcome, not just file overlap. Stop and report to the user only when another active brief is already implementing the same outcome.

File overlap alone is not always duplicate work. For example, two independent changes to a central test fixture may overlap on a path but still be different work; the queue will serialize that path.

## Git Race Recovery

Checkout-stage race:

1. `git pull --ff-only`
2. Run `request`.
3. Commit `LIBRARY.md`.
4. `git push`
5. If push is rejected because the remote moved, restore only the local coordination-file changes from the failed attempt, pull, rerun `request` with the same work id, recommit, and push.

Per-file release race:

1. Finish implementation for the checked-out file or coherent file set.
2. Run `release --id <work-id> --files <paths...>`.
3. Commit the completed file changes together with `LIBRARY.md`.
4. `git push`
5. If push is rejected because the remote moved, preserve implementation edits, pull or rebase according to repo policy, rerun `release` on the updated `LIBRARY.md`, recommit if needed, and push.

Final-stage race:

1. Preserve implementation edits.
2. Pull or rebase according to repo policy.
3. If `LIBRARY.md` changed, rerun `finish --id <work-id>` so queue promotion is recalculated on the newest state.
4. Commit or amend according to repo policy, then push.

Do not use destructive whole-repo resets for this protocol.

Check-in remote movement:

1. Run `checkin --id <work-id> --note <note>` at least once every 60 seconds.
2. The command fetches remote state by default and refuses to write a check-in when the upstream branch is ahead or diverged.
3. If the check-in reports `remote-moved`, sync the branch, inspect whether any checked-out files were bumped, and rerun check-in.
4. If bumped files are reported, discard local changes only for those paths and treat them as queued work.

## Polling

When no checked-out work remains and only queued paths are left, poll every 10 seconds. Pull the latest branch state before or during polling according to repo policy. Use `wait --preempt-stale --threshold-seconds 120` or `preempt-stale` so stale checkouts do not block the queue indefinitely. Do not return to the user simply because the work is queued.

## Check-ins And Stale Preemption

Each active implementation stores `last_checkin_at`, `progress_note`, recent `checkins`, and `bumped_files`. Check in at least once every 60 seconds while holding checkouts. Commit and push the resulting `LIBRARY.md` update so other agents can see the heartbeat.

A checkout is stale after 120 seconds without a check-in. A queued implementation may preempt stale blockers with `preempt-stale --id <queued-work-id>`. Preemption removes the stale owner's checkout, appends the stale owner to the end of that file's queue, records the path under the stale owner's `bumped_files`, and promotes the next queued work id.

When an agent sees one of its paths under `bumped_files`, it must discard local changes for that path only, stop editing it, and wait for the path to be checked out again. Use targeted restoration such as `git restore -- path/to/file`; do not reset the whole repo.

## Incremental File Release

Release a checkout as soon as the implementation for that file is complete. Do not wait for unrelated files in the same implementation. `scripts/coordinator.py release` records the file under `completed_files`, removes the checkout, promotes the first queued work id for that path, and keeps the active brief open.

After `release`, commit and push the completed file changes with `LIBRARY.md` immediately. This shortens the time that other agents spend blocked in the queue.

## Manual Queue Rules

If a manual repair is unavoidable:

- Each path can have at most one checkout owner.
- Queue entries are ordered by wait position.
- Releasing a checkout promotes the first queued work id for that path.
- Removing a queued work id causes later positions to move down.
- Completed files remain in the active brief as `completed_files` until the implementation is archived.
- Stale preemption moves a stale checkout owner to the end of that file's queue and records the path under `bumped_files`.
- A bumped owner must not keep local edits for the bumped file.
- A completed implementation must have no entries remaining in active briefs, checkouts, or queues.
- Archive completed work at the end of `ARCHIVE.md` with the completion timestamp.
