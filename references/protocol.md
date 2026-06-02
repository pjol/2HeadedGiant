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

## Work Ids

Use stable ids for the entire prompt, for example `awc-20260602-fix-login-state`. Reuse the same id when a checkout push races and must be retried. Do not generate a new id for the same implementation unless the original id was never pushed and never archived.

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

Final-stage race:

1. Preserve implementation edits.
2. Pull or rebase according to repo policy.
3. If `LIBRARY.md` changed, rerun `finish --id <work-id>` so queue promotion is recalculated on the newest state.
4. Commit or amend according to repo policy, then push.

Do not use destructive whole-repo resets for this protocol.

## Polling

When no checked-out work remains and only queued paths are left, poll every 10 seconds. Pull the latest branch state before or during polling according to repo policy. Do not return to the user simply because the work is queued.

## Manual Queue Rules

If a manual repair is unavoidable:

- Each path can have at most one checkout owner.
- Queue entries are ordered by wait position.
- Releasing a checkout promotes the first queued work id for that path.
- Removing a queued work id causes later positions to move down.
- A completed implementation must have no entries remaining in active briefs, checkouts, or queues.
- Archive completed work at the end of `ARCHIVE.md` with the completion timestamp.
