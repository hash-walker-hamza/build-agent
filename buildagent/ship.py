"""Ship: split migration commits into their own PR, push, open PRs, ask reviewers. Runs as the user (gh login);
the model never reaches this code."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .config import Config, Repo
from .proc import sh

PRIVATE_SECTION = re.compile(r"(?ims)^##\s*decided without you\b[^\n]*\n(.*?)(?=^##\s|\Z)")


def commits_by_kind(wt: Path, base: str, mig_dir: str | None):
    """(migration-only commits, other commits, mixed commits), oldest first."""
    shas = sh("git", "rev-list", "--reverse", f"origin/{base}..HEAD", cwd=wt).stdout.split()
    if not mig_dir:
        return [], shas, []
    mig, code, mixed = [], [], []
    for s in shas:
        files = sh("git", "diff-tree", "--no-commit-id", "--name-only", "-r", s, cwd=wt).stdout.split()
        m = [f for f in files if f.startswith(mig_dir + "/")]
        (mig if m and len(m) == len(files) else code if not m else mixed).append(s)
    return mig, code, mixed


def _temp_worktree(main_repo: Path, base: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="build-split-"))
    tmp.rmdir()
    sh("git", "worktree", "add", "--detach", tmp, f"origin/{base}", cwd=main_repo, check=True)
    return tmp


def branch_from(main_repo: Path, base: str, name: str, shas: list) -> str:
    """Local branch `name` = origin/<base> + cherry-picked `shas`."""
    tmp = _temp_worktree(main_repo, base)
    try:
        for s in shas:
            sh("git", "cherry-pick", "--allow-empty", s, cwd=tmp, check=True)
        sh("git", "branch", "-f", name, "HEAD", cwd=tmp, check=True)
    finally:
        sh("git", "worktree", "remove", "--force", tmp, cwd=main_repo)
    return name


def branch_from_paths(main_repo: Path, base: str, name: str, wt: Path, mig_dir: str, migrations: bool) -> str:
    """Local branch `name` = origin/<base> + one commit holding only the migration dir's changes (or only the rest),
    for builds whose commits mixed the two."""
    tmp = _temp_worktree(main_repo, base)
    try:
        head = sh("git", "rev-parse", "HEAD", cwd=wt, check=True).stdout.strip()
        spec = ["--", mig_dir] if migrations else ["--", ".", f":(exclude){mig_dir}"]
        patch = sh("git", "diff", "--binary", f"origin/{base}", head, *spec, cwd=wt, check=True).stdout
        r = subprocess.run(["git", "apply", "--index", "-"], input=patch, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"splitting {name}: {r.stderr.strip()[:200]}")
        msg = sh("git", "log", "-1", "--format=%s", head, cwd=wt).stdout.strip()
        sh("git", "commit", "-qm", ("migration: " if migrations else "") + msg, cwd=tmp, check=True)
        sh("git", "branch", "-f", name, "HEAD", cwd=tmp, check=True)
    finally:
        sh("git", "worktree", "remove", "--force", tmp, cwd=main_repo)
    return name


def move_private(body: str, decided: Path) -> str:
    """A "Decided without you" section is the author's, not the reviewers': it leaves the body for decided.md."""
    found = PRIVATE_SECTION.search(body)
    if not found:
        return body
    text = found.group(1).strip()
    old = decided.read_text() if decided.exists() else ""
    if text and text not in old:
        decided.write_text((old.rstrip() + "\n\n" if old.strip() else "") + text + "\n")
    return (body[:found.start()] + body[found.end():]).strip()


def read_pr_md(path: Path):
    """(title, body) from the model's pr.md."""
    text = path.read_text() if path.exists() else ""
    m = re.match(r"\s*TITLE:\s*(.+)\n", text)
    title = m.group(1).strip() if m else ""
    body = (text[m.end():] if m else text).strip()
    return title, move_private(body, path.parent / "decided.md")


def open_pr(repo: Repo, branch: str, title: str, body: str, dry: bool, out=print):
    if dry:
        out(f"[dry-run] would push {branch} and open {repo.github} PR into {repo.base}: {title}")
        return None
    sh("git", "push", "-u", "origin", f"{branch}:refs/heads/{branch}", cwd=repo.path, check=True, timeout=300)
    found = sh("gh", "pr", "list", "-R", repo.github, "--head", branch, "--state", "open", "--json", "number",
               check=True, timeout=60)
    existing = json.loads(found.stdout or "[]")
    if existing:
        return existing[0]["number"]
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    try:
        r = sh("gh", "pr", "create", "-R", repo.github, "--base", repo.base, "--head", branch, "--title", title,
               "--body-file", f.name, check=True, timeout=120)
    finally:
        os.unlink(f.name)
    return int(re.search(r"/pull/(\d+)", r.stdout).group(1))


def request_review(cfg: Config, repo: Repo, n: int):
    if cfg.reviewers:
        sh("gh", "api", "-X", "POST", f"repos/{repo.github}/pulls/{n}/requested_reviewers",
           *[a for r in cfg.reviewers for a in ("-f", f"reviewers[]={r}")], check=True)


def fill_links(body: str, prs: dict, cfg: Config) -> str:
    """`PR:<repo>` placeholders become owner/name#n links to PRs opened earlier in this ship."""
    for name, pr in prs.items():
        if pr.get("n") and name in cfg.repos:
            body = body.replace(f"PR:{name}", f"{cfg.repos[name].github}#{pr['n']}")
    return body


def ship(cfg: Config, st: dict, runs: Path, dry: bool, save, out=print) -> dict:
    """Opens the PRs in plan order. Records each in st["prs"] as it opens, so a failure part-way resumes."""
    key, plan = st["key"], st["plan"]
    prs = dict(st.get("prs") or {}) if dry else st.setdefault("prs", {})
    for name in plan["repos"]:
        repo, info = cfg.repos[name], st["repos"][name]
        wt, branch = Path(info["worktree"]), info["branch"]
        title, body = read_pr_md(runs / key / name / "pr.md")
        mig, code, mixed = commits_by_kind(wt, repo.base, repo.migrations_dir)
        if mixed:
            mig, code = mig or mixed, code or mixed
        mkey = f"{name}:migration"
        if mig and mkey not in prs:
            if dry:
                mbranch = f"{branch}-migration"
            elif mixed:
                mbranch = branch_from_paths(repo.path, repo.base, f"{branch}-migration", wt, repo.migrations_dir, True)
            else:
                mbranch = branch_from(repo.path, repo.base, f"{branch}-migration", mig)
            mbody = (f"Migration for {key}, on its own so it can run before the code that needs it deploys. "
                     "The code PR merges after this one has run.")
            prs[mkey] = {"repo": name, "n": open_pr(repo, mbranch, f"{key}: migration for {plan['summary'][:60]}",
                                                     mbody, dry, out)}
            save(st) if not dry else None
        if code and name not in prs:
            if mig and not dry:
                cbranch = (branch_from_paths(repo.path, repo.base, f"{branch}-code", wt, repo.migrations_dir, False)
                           if mixed else branch_from(repo.path, repo.base, f"{branch}-code", code))
            else:
                cbranch = branch
            if prs.get(mkey, {}).get("n"):
                body = body.replace("MIGRATION_PR", f"#{prs[mkey]['n']}")
            prs[name] = {"repo": name, "n": open_pr(repo, cbranch, title, fill_links(body, prs, cfg), dry, out)}
            save(st) if not dry else None
    if dry:
        return prs
    for pr in prs.values():
        if pr.get("n") and not pr.get("review_requested"):
            request_review(cfg, cfg.repos[pr["repo"]], pr["n"])
            pr["review_requested"] = True
    save(st)
    return prs
