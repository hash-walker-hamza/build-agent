"""The gate: each repo's configured checks, then a fresh reviewer run. Problems go back to the build run.

A gate step is either a shell command (non-zero exit = one problem, its output's tail as the summary) or a Python
module exposing `checks(ctx) -> list[dict]`, each dict {"id", "summary", "rebuttable"?}. ctx is a GateContext.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from . import prompts, tools
from .config import Config, GateStep, Repo
from .proc import last_json, sh, shell


@dataclass
class GateContext:
    key: str
    repo: str
    worktree: str
    base: str            # e.g. origin/main
    run_dir: str
    pr_body_file: str
    plan: dict
    changed: list        # paths changed against the merge base


def changed_files(wt: Path, base: str) -> list:
    return [p for p in sh("git", "diff", "--name-only", f"origin/{base}...HEAD", cwd=wt).stdout.splitlines() if p]


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"gate_{path.stem}_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_step(cfg: Config, step: GateStep, ctx: GateContext) -> list:
    if step.when_changed and not any(re.search(step.when_changed, p) for p in ctx.changed):
        return []
    pid = f"gate:{step.name}"
    if step.python:
        try:
            found = _load_module(step.python).checks(ctx)
        except Exception as e:  # noqa: BLE001 - a broken plugin is a gate problem, not a crash
            return [{"id": f"{pid}-crashed", "source": step.name, "rebuttable": False,
                     "summary": f"{step.python.name} raised {type(e).__name__}: {str(e)[:300]}"}]
        return [{"id": f"{pid}:{p.get('id', i + 1)}", "source": step.name, "summary": str(p.get("summary", ""))[:600],
                 "rebuttable": bool(p.get("rebuttable", step.rebuttable))} for i, p in enumerate(found or [])]
    env = tools.tool_env(cfg, {"BUILD_KEY": ctx.key, "BUILD_REPO": ctx.repo, "BUILD_WORKTREE": ctx.worktree,
                               "BUILD_BASE": ctx.base, "BUILD_RUN_DIR": ctx.run_dir,
                               "BUILD_PR_BODY": ctx.pr_body_file})
    r = shell(step.run, cwd=ctx.worktree, env=env, timeout=step.timeout)
    if r.returncode == 0:
        return []
    tail = (r.stdout + r.stderr).strip()[-600:]
    return [{"id": pid, "source": step.name, "rebuttable": step.rebuttable,
             "summary": f"`{step.run}` exited {r.returncode}: {tail}"}]


def run_checks(cfg: Config, repo: Repo, key: str, wt: Path, rdir: Path, plan: dict) -> tuple:
    """(problems, results) for this repo's gate steps."""
    ctx = GateContext(key=key, repo=repo.name, worktree=str(wt), base=f"origin/{repo.base}", run_dir=str(rdir),
                      pr_body_file=str(rdir / "pr.md"), plan=plan, changed=changed_files(wt, repo.base))
    problems, results = [], {}
    for step in repo.gate:
        found = run_step(cfg, step, ctx)
        results[step.name] = "pass" if not found else [p["id"] for p in found]
        problems += found
    (rdir / "gate-context.json").write_text(json.dumps(asdict(ctx), indent=1))
    return problems, results


def review(cfg: Config, run_model, key: str, repo: Repo, wt: Path, rdir: Path, other_wts: list, results: dict):
    diff = rdir / "review.diff"
    diff.write_text(sh("git", "diff", f"origin/{repo.base}...HEAD", cwd=wt).stdout)
    other = f"Read-only context, the other repo(s): {', '.join(map(str, other_wts))}." if other_wts else ""
    answered = rdir / "answered.json"
    if answered.exists():
        other += (f" Earlier findings and how they were answered are in {answered}: raise one again only if its "
                  "answer is wrong, and say why.")
    prompt = prompts.REVIEW.format(wt=wt, diff=diff, ticket=rdir.parent / "ticket.md",
                                   brief=cfg.briefs / f"{key}.md", pr=rdir / "pr.md", other=other,
                                   base=repo.base, checks=json.dumps(results),
                                   repo_review=repo.review_instructions)
    allowed = tools.read_rules([wt, *other_wts, rdir, cfg.briefs]) + ["Grep", "Glob", *tools.GIT_READ]
    r = run_model(key, f"review-{repo.short}", prompt, wt, allowed, rdir / "review")
    found = last_json(r.stdout, "findings")
    if found is None:
        return [{"id": "review-missing", "source": "reviewer", "rebuttable": False,
                 "summary": "the reviewer run printed no findings JSON"}]
    return [{"id": f"review:{i + 1}", "source": "reviewer", "rebuttable": True,
             "summary": f"[{f.get('severity')}] {f.get('file')}:{f.get('line')}: {f.get('summary')} — {f.get('scenario')}"}
            for i, f in enumerate(found.get("findings") or []) if f.get("severity") in ("high", "medium")]


def rebuttal_key(p: dict) -> str:
    """An accepted rebuttal holds for this repo and this exact problem text only."""
    return f"{p.get('repo')}|{p['id']}|{hashlib.sha1(p['summary'].encode()).hexdigest()[:12]}"


def unresolved(problems: list, responses: list) -> list:
    """Problems still open after a fix round: unanswered, or rebutted when they can't be rebutted."""
    answered = {r.get("id"): r for r in responses or [] if isinstance(r, dict)}
    out = []
    for p in problems:
        r = answered.get(p["id"])
        if r and r.get("action") == "fixed":
            continue
        if r and r.get("action") == "rebutted" and p["rebuttable"] and len((r.get("evidence") or "").strip()) > 20:
            continue
        out.append(p)
    return out
