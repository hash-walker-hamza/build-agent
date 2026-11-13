"""The exact tools and environment each headless `claude -p` run gets.

`--tools` decides which built-in tools exist at all; `--allowedTools` pre-approves rules; `--disallowedTools` denies
connector servers the run doesn't need and the git/gh/cloud commands no run may use. The model never holds a GitHub
or cloud login: its environment drops tokens and points gh/gcloud at empty config dirs, and git pushes are rewritten
to a URL that can't resolve.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

from .config import Config

NO_PUSH_ENV = {
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "url.build-agent-no-push://blocked/.pushInsteadOf", "GIT_CONFIG_VALUE_0": "https://github.com/",
    "GIT_CONFIG_KEY_1": "url.build-agent-no-push://blocked/.pushInsteadOf", "GIT_CONFIG_VALUE_1": "git@github.com:",
    "GIT_CONFIG_KEY_2": "credential.helper", "GIT_CONFIG_VALUE_2": "",
    "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
}
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
DENY_BASH = ["Bash(git push:*)", "Bash(git * push*)", "Bash(git -C:*)", "Bash(git -c:*)", "Bash(git send-pack:*)",
             "Bash(git fetch:*)", "Bash(git pull:*)", "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(git reset:*)",
             "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git worktree:*)", "Bash(git config:*)",
             "Bash(git remote:*)", "Bash(git credential:*)", "Bash(gh:*)", "Bash(* gh *)", "Bash(*GH_CONFIG_DIR*)",
             "Bash(*GH_TOKEN*)", "Bash(*CLOUDSDK*)", "Bash(security:*)", "Bash(gcloud:*)", "Bash(*gcloud*)",
             "Bash(aws:*)", "Bash(az:*)", "Bash(psql:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)",
             "Bash(env:*)", "Bash(unset:*)", "Bash(export:*)", "Bash(python3 -c:*)", "Bash(python -c:*)",
             "Bash(*/python -c*)", "Bash(osascript:*)", "Bash(open:*)", "Bash(launchctl:*)", "Bash(crontab:*)",
             "Bash(docker compose:*)", "Bash(docker-compose:*)", "Bash(docker rm:*)", "Bash(docker kill:*)",
             "Bash(docker system:*)"]
GIT_WRITE = ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
             "Bash(git show:*)", "Bash(git mv:*)", "Bash(git rm:*)", "Bash(git restore:*)", "Bash(git grep:*)",
             "Bash(git rev-parse:*)"]
GIT_READ = ["Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git grep:*)"]


def read_rules(paths) -> list:
    return [f"Read(/{Path(p)}/**)" for p in paths]


def write_rules(paths) -> list:
    return [r for p in paths for r in (f"Edit(/{Path(p)}/**)", f"Write(/{Path(p)}/**)")]


def flags(cfg: Config, builtins=(), mcp=()) -> list:
    """CLI flags allowing exactly `builtins` (e.g. "Read(//x/**)", "Bash(git log:*)") and `mcp` ("Server__tool")."""
    names = {b.split("(", 1)[0] for b in builtins}
    allowed = set(mcp)
    deny = []
    for server in cfg.connector_servers:
        wanted = [t for t in allowed if t.startswith(server + "__")]
        if not wanted:
            deny.append(f"mcp__claude_ai_{server}")
        else:
            deny += [f"mcp__claude_ai_{server}__{t}" for t in cfg.connector_known.get(server, [])
                     if f"{server}__{t}" not in allowed]
    deny += [f"mcp__{s}" for s in cfg.local_mcp_servers]
    out = ["--tools", ",".join(sorted(names))]
    if builtins or mcp:
        out += ["--allowedTools", *builtins, *[f"mcp__claude_ai_{t}" for t in mcp]]
    out += ["--disallowedTools", *deny, *DENY_BASH]
    return out


def claude_cmd(cfg: Config, prompt: str, builtins, mcp=()) -> list:
    cmd = [cfg.claude, "-p", prompt, "--model", cfg.model, "--max-turns", str(cfg.max_turns)]
    if cfg.setting_sources is not None:
        cmd += ["--setting-sources", cfg.setting_sources]
    return cmd + flags(cfg, builtins, mcp)


def model_env(cfg: Config, rdir: Path, mcp=()) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in TOKEN_VARS}
    for d in ("gh-empty", "gcloud-empty"):
        (rdir / d).mkdir(parents=True, exist_ok=True)
    user = os.environ.get("USER") or getpass.getuser()
    env.update(USER=user, LOGNAME=os.environ.get("LOGNAME") or user,
               ENABLE_CLAUDEAI_MCP_SERVERS="true" if mcp else "false", **NO_PUSH_ENV,
               GH_CONFIG_DIR=str(rdir / "gh-empty"), CLOUDSDK_CONFIG=str(rdir / "gcloud-empty"),
               GOOGLE_APPLICATION_CREDENTIALS="/dev/null")
    if mcp:
        env["MCP_CONNECTION_NONBLOCKING"] = "false"
    if cfg.path_env:
        env["PATH"] = cfg.path_env
    return env


def tool_env(cfg: Config, extra=None) -> dict:
    """Environment for the agent's own commands (gate steps, setup): the user's, with the configured PATH."""
    env = dict(os.environ)
    if cfg.path_env:
        env["PATH"] = cfg.path_env
    env.update(extra or {})
    return env
