"""Loads and checks the TOML config. Every machine- or project-specific value lives here, nowhere in the code."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    import tomli as tomllib

DEFAULT_CONFIG = Path(os.environ.get("BUILD_AGENT_CONFIG", "~/.config/build-agent/config.toml")).expanduser()


class ConfigError(SystemExit):
    pass


def _path(value: str | None, base: Path) -> Path | None:
    if not value:
        return None
    p = Path(os.path.expandvars(value)).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


@dataclass
class GateStep:
    name: str
    run: str | None = None        # shell command, run in the worktree; non-zero exit is a problem
    python: Path | None = None    # module exposing checks(ctx) -> list[dict]
    rebuttable: bool = False
    timeout: int = 1800
    when_changed: str | None = None  # regex on changed paths; the step is skipped when nothing matches


@dataclass
class Repo:
    name: str
    path: Path
    github: str                   # owner/name
    base: str = "main"
    short: str = ""
    copy_files: list = field(default_factory=list)   # globs copied from the main checkout into a new worktree
    setup: list = field(default_factory=list)        # commands run once in a new worktree
    allow: list = field(default_factory=list)        # extra Bash(...) rules for the build run
    instructions: str = ""                           # appended to the build prompt for this repo
    review_instructions: str = ""                    # appended to the reviewer's checklist
    migrations_dir: str | None = None                # commits touching only this dir ship as their own PR
    gate: list = field(default_factory=list)         # [GateStep]


@dataclass
class Config:
    source: Path
    home: Path
    model: str
    max_rounds: int
    run_timeout: int
    max_turns: int
    key_pattern: re.Pattern
    author: str
    claude: str
    path_env: str | None
    setting_sources: str | None
    briefs: Path
    decision_headings: list
    tracker_mcp: list
    tracker_hint: str
    connector_servers: list
    connector_known: dict
    local_mcp_servers: list
    reviewers: list
    notify_webhook: Path | None
    post_ship: str | None
    repos: dict                                      # name -> Repo, in config order

    @property
    def state(self) -> Path:
        return self.home / "state"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

    @property
    def worktrees(self) -> Path:
        return self.home / "worktrees"

    @property
    def logs(self) -> Path:
        return self.home / "logs"


def load(path: Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG).expanduser()
    if not path.exists():
        raise ConfigError(f"no config at {path}. Copy examples/config.example.toml there, or set BUILD_AGENT_CONFIG.")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    base = path.parent
    agent, brief, tracker, tools = (raw.get(k, {}) for k in ("agent", "brief", "tracker", "tools"))
    github, notify, hooks = (raw.get(k, {}) for k in ("github", "notify", "hooks"))

    repos = {}
    for r in raw.get("repos", []):
        for need in ("name", "path", "github"):
            if not r.get(need):
                raise ConfigError(f"{path}: every [[repos]] needs `{need}`")
        if r["name"] in repos:
            raise ConfigError(f"{path}: repo `{r['name']}` is listed twice")
        steps = []
        for g in r.get("gate", []):
            if not g.get("name") or bool(g.get("run")) == bool(g.get("python")):
                raise ConfigError(f"{path}: repo {r['name']}: each gate step needs a name and exactly one of run/python")
            steps.append(GateStep(name=g["name"], run=g.get("run"), python=_path(g.get("python"), base),
                                  rebuttable=bool(g.get("rebuttable", False)), timeout=int(g.get("timeout", 1800)),
                                  when_changed=g.get("when_changed")))
        repos[r["name"]] = Repo(
            name=r["name"], path=_path(r["path"], base), github=r["github"], base=r.get("base", "main"),
            short=r.get("short") or r["name"], copy_files=list(r.get("copy_files", [])),
            setup=list(r.get("setup", [])), allow=list(r.get("allow", [])),
            instructions=r.get("instructions", "").strip(), review_instructions=r.get("review_instructions", "").strip(),
            migrations_dir=(r.get("migrations_dir") or "").strip("/") or None, gate=steps)
    if not repos:
        raise ConfigError(f"{path}: no [[repos]]")

    return Config(
        source=path,
        home=_path(agent.get("home", "~/.build-agent"), base),
        model=agent.get("model", "claude-opus-5-5"),
        max_rounds=int(agent.get("max_rounds", 3)),
        run_timeout=int(agent.get("run_timeout", 3 * 3600)),
        max_turns=int(agent.get("max_turns", 400)),
        key_pattern=re.compile(agent.get("key_pattern", r"^[A-Z][A-Z0-9]*-\d+$")),
        author=agent.get("author", "the author"),
        claude=agent.get("claude", "claude"),
        path_env=agent.get("path"),
        setting_sources=agent.get("setting_sources"),
        briefs=_path(brief.get("dir", "~/.build-agent/briefs"), base),
        decision_headings=list(brief.get("decision_headings", ["Decided after kickoff", "Decision", "Decisions"])),
        tracker_mcp=list(tracker.get("mcp", [])),
        tracker_hint=tracker.get("hint", ""),
        connector_servers=list(tools.get("connector_servers", [])),
        connector_known=dict(tools.get("connector_known", {})),
        local_mcp_servers=list(tools.get("local_mcp_servers", [])),
        reviewers=list(github.get("reviewers", [])),
        notify_webhook=_path(notify.get("webhook_file"), base),
        post_ship=hooks.get("post_ship"),
        repos=repos,
    )
