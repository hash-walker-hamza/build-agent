"""Example Python gate step: every commit on the branch names the ticket key.

A plugin exposes checks(ctx) -> list of {"id", "summary", "rebuttable"?}. ctx fields: key, repo, worktree,
base (e.g. "origin/main"), run_dir, pr_body_file, plan (dict), changed (paths changed against the merge base).
An empty list means the step passed. An exception is reported as a gate problem, not a crash.
"""
import subprocess


def checks(ctx):
    subjects = subprocess.run(["git", "log", "--format=%h %s", f"{ctx.base}..HEAD"], cwd=ctx.worktree,
                              capture_output=True, text=True, check=True).stdout.splitlines()
    bad = [s for s in subjects if ctx.key not in s]
    if not bad:
        return []
    return [{"id": "missing-key", "rebuttable": False,
             "summary": f"{len(bad)} commit(s) without {ctx.key}: {'; '.join(bad[:5])}"}]
