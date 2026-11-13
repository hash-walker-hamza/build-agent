"""End to end on a toy repo with a stub `claude`: plan -> build -> gate (fails, fix round, passes) -> ship."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from buildagent import agent as agent_mod, config, ship

ROOT = Path(__file__).resolve().parents[1]
KEY = "TOY-7"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def setup(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    main = tmp_path / "toy"
    git(tmp_path, "clone", "-q", str(origin), str(main))
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        git(main, "config", k, v)
    (main / "README").write_text("toy\n")
    (main / ".env").write_text("SECRET=1\n")  # gitignored file a worktree needs
    (main / ".gitignore").write_text(".env\n")
    git(main, "add", "README", ".gitignore")
    git(main, "commit", "-qm", "init")
    git(main, "push", "-q", "origin", "HEAD:main")

    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests/stub_claude.py'} \"$@\"\n")
    stub.chmod(0o755)
    home = tmp_path / "home"
    (home / "briefs").mkdir(parents=True)
    (home / "briefs" / f"{KEY}.md").write_text("# Toy\nSay hello.\n\n## Decided after kickoff\n- say hello\n\n## Notes\n")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f"""
[agent]
home = "{home}"
claude = "{stub}"
author = "Tester"
max_rounds = 2
[brief]
dir = "{home / 'briefs'}"
[tools]
connector_servers = ["Gmail"]
local_mcp_servers = ["claude-in-chrome"]
[github]
reviewers = ["someone"]
[[repos]]
name = "toy"
path = "{main}"
github = "me/toy"
copy_files = [".env"]
migrations_dir = "migrations"
allow = ["make test:*"]
  [[repos.gate]]
  name = "greeting"
  run = "grep -q fixed hello.txt"
  [[repos.gate]]
  name = "commit-keys"
  python = "{ROOT / 'examples/plugins/commit_keys.py'}"
""")
    return {"cfg": config.load(cfg_file), "main": main, "home": home, "calls": tmp_path / "calls.jsonl"}


def test_plan_build_gate_fix_and_dry_ship(setup, monkeypatch):
    monkeypatch.setenv("STUB_CALLS", str(setup["calls"]))
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-the-model")
    out = []
    a = agent_mod.Agent(setup["cfg"], KEY, out=out.append)

    assert a.run(dry=True) == 0

    st = a.load_state()
    assert st["phase"] == "ready" and st["rounds"] == 1
    assert st["plan"]["slug"] == "say-hello"
    wt = Path(st["repos"]["toy"]["worktree"])
    assert (wt / ".env").read_text() == "SECRET=1\n"
    assert "fixed" in (wt / "hello.txt").read_text()
    shipped = [l for l in out if l.startswith("[dry-run]")]
    assert len(shipped) == 2 and "TOY-7-say-hello-migration" in shipped[0] and "TOY-7: Say hello" in shipped[1]

    calls = [json.loads(l) for l in setup["calls"].read_text().splitlines()]
    kinds = [c["argv"][1][:10] for c in calls]
    assert kinds == ["You plan t", "You are Te", "Review a c", "You are Te", "Review a c"]
    for c in calls:
        assert c["env"]["GH_TOKEN"] is None
        assert c["env"]["GH_CONFIG_DIR"].endswith("gh-empty")
        assert c["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
        deny = c["argv"][c["argv"].index("--disallowedTools") + 1:]
        assert "mcp__claude_ai_Gmail" in deny and "mcp__claude-in-chrome" in deny and "Bash(git push:*)" in deny
    build = calls[1]["argv"]
    allowed = build[build.index("--allowedTools") + 1:build.index("--disallowedTools")]
    assert f"Write(/{wt}/**)" in allowed and "Bash(make test:*)" in allowed
    assert not any(r.startswith(("Bash(cat", "Bash(grep", "Bash(head")) for r in allowed)
    review = calls[2]["argv"]
    assert not any(r.startswith(("Write(", "Edit(")) for r in review[review.index("--allowedTools"):])


def test_ship_splits_the_migration_into_its_own_branch(setup, monkeypatch):
    monkeypatch.setenv("STUB_CALLS", str(setup["calls"]))
    a = agent_mod.Agent(setup["cfg"], KEY, out=lambda *_: None)
    assert a.run(dry=True) == 0
    opened, asked = [], []
    monkeypatch.setattr(ship, "open_pr", lambda repo, branch, title, body, dry, out=print:
                        opened.append((branch, title, body)) or len(opened))
    monkeypatch.setattr(ship, "request_review", lambda cfg, repo, n: asked.append(n))

    st = a.load_state()
    a.phase_ship(st, dry=False)

    main = setup["main"]
    files = lambda b: sorted(git(main, "diff", "--name-only", f"origin/main...{b}").split())  # noqa: E731
    assert files("TOY-7-say-hello-migration") == ["migrations/001_hello.sql"]
    assert files("TOY-7-say-hello-code") == ["hello.txt"]
    (_, _, mbody), (_, title, body) = opened
    assert title == "TOY-7: Say hello" and "Release gate: #1" in body
    assert "Decided without you" not in body
    assert "picked the word hello" in (setup["home"] / "runs" / KEY / "toy" / "decided.md").read_text()
    assert asked == [1, 2] and a.load_state()["phase"] == "shipped"
