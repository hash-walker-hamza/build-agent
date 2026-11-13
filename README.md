# build-agent

Takes a ticket from a written brief to open, reviewed-ready pull requests, unattended. It plans which repos change,
builds each change in its own git worktree with Claude Code, runs your gate (tests, lint, custom checks) plus an
independent AI reviewer, loops fixes until the gate is clean, then pushes and opens PRs with reviewers asked.

Runs on **macOS, Linux and WSL2**. Native Windows is not supported (it relies on POSIX process groups and `flock`);
on a Windows laptop, use WSL2.

## How a build runs

```
brief (your decisions) ──► plan ──► build (one run per repo, own worktree)
                                        │
                                        ▼
                     ┌──── gate: your checks + fresh reviewer ────┐
                     │  problems? → build run fixes or rebuts      │  ≤ max_rounds
                     └────────────────────────────────────────────┘
                                        │ clean
                                        ▼
                     ship: migration PR · code PR(s) · reviewers · post_ship hook
```

| Phase | What happens | Who acts |
|---|---|---|
| plan | Reads the brief (and the ticket, if a tracker connector is configured) and the code; picks repos, branch slug, migration yes/no. Read-only. | model |
| build | One run per repo in `<home>/worktrees/<repo>-<KEY>`: code, tests, commits, `pr.md`. Calls the brief doesn't cover go to `decided.md`, for you only. | model |
| gate | Each repo's `[[repos.gate]]` steps, then a reviewer run that sees the diff and the gate results. Problems go back to the build run, which fixes each one or rebuts it with evidence. Mechanical failures can't be rebutted. | agent + model |
| ship | Commits that touch only `migrations_dir` become their own PR off the base; the rest becomes the code PR. Push, open PRs, request reviewers, run `post_ship`. | agent (your `gh` login) |

State lives in `<home>/state/<KEY>.json`; re-running `build-agent <KEY>` resumes from the last finished phase.

## Safety model

The model runs are headless `claude -p` with exact tool lists, not a general agent:

- `--tools` loads only the built-in tools a phase needs; `--allowedTools` scopes Read/Edit/Write to the worktree, the
  run folder and the briefs, plus the Bash prefixes your config names.
- **No push, no GitHub, no cloud.** Token env vars are dropped, `gh`/`gcloud` see empty config dirs, git pushes are
  rewritten to an unresolvable URL, and `git push`/`fetch`/`reset`/`checkout`, `gh`, `curl`, `ssh` and the like are
  denied outright. Only the ship phase, which is plain Python running as you, pushes.
- claude.ai connectors are off unless `[tracker]` asks for some; then every other server in `connector_servers` is
  denied.
- There are no default `cat`/`grep`/`head` Bash rules: those prefixes can't be scoped to a folder (`cat /wt/../../x`),
  so the model reads through the path-scoped Read/Grep/Glob tools instead.

Not a sandbox: any test command you allow (`pytest:*`, `pnpm exec vitest:*`) runs code the model wrote, with your
user's file access. Treat `allow` as "commands the model may run as me".

## Install

### macOS / Linux

```bash
git clone https://github.com/hash-walker-hamza/build-agent.git ~/build-agent
cd ~/build-agent && ./install.sh
```

### Windows (WSL2)

In PowerShell (admin), once:

```powershell
wsl --install -d Ubuntu
```

Then inside Ubuntu:

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
# GitHub CLI: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
# Claude Code: https://docs.claude.com/en/docs/claude-code (needs Node 18+ or the native installer)
gh auth login
git clone https://github.com/hash-walker-hamza/build-agent.git ~/build-agent
cd ~/build-agent && ./install.sh
```

Keep your repos inside the WSL filesystem (`~/code/...`), not under `/mnt/c/...`: git and test runs on the Windows
mount are many times slower, and file modes are unreliable there. If a gate step needs Docker, install Docker Desktop
and turn on its WSL integration.

### After installing

1. Edit `~/.config/build-agent/config.toml` (see [examples/config.example.toml](examples/config.example.toml)).
2. For unattended runs, `claude setup-token` and save the token to `~/.build-agent/secrets/claude-token` (`chmod 600`).
   Without it the CLI uses your interactive login.
3. `build-agent --check` validates the config, the CLIs, the gh login, each repo and its base branch.

## The brief

`<brief dir>/<KEY>.md`, written by you (or by a kickoff tool). Free-form, with one required section whose heading is
in `decision_headings` (default `Decided after kickoff`, `Decision`, `Decisions`). That section is your decisions and
overrides the rest of the brief.

```markdown
# PROJ-123: Refunds over 30 days need approval

Context, links, edge cases, anything the model should know.

## Decided after kickoff
- Threshold is 30 calendar days from delivery, not from order.
- Approval is a new `refund_approvals` table; no change to `refunds`.
- Out of scope: the email to finance.
```

## Usage

```bash
build-agent PROJ-123              # starts detached, prints the log path
build-agent PROJ-123 --dry-run    # plan, build, gate; print what would be pushed
build-agent PROJ-123 --status     # phase, PRs, open problems, decisions made without you
build-agent PROJ-123 --request-review
build-agent PROJ-123 --foreground # same as the first, attached to the terminal
```

One build runs per machine at a time (`<home>/state/_machine.lock`); a second one queues.

## Config

Everything project- or machine-specific is in the TOML config; the code holds none of it. Annotated example:
[examples/config.example.toml](examples/config.example.toml). The parts that do the most work:

### `[[repos]]`

| Key | Meaning |
|---|---|
| `name`, `path`, `github`, `base` | Short name, main checkout, `owner/name`, base branch for branches and PRs |
| `copy_files` | Globs copied from the main checkout into each new worktree (gitignored `.env` files) |
| `setup` | Commands run once in a new worktree (`pnpm install --frozen-lockfile`) |
| `allow` | Extra Bash rules for the build run, e.g. `".venv/bin/python -m pytest:*"` |
| `instructions` | Repo-specific guidance appended to the build prompt: how to run tests, formatters, conventions |
| `review_instructions` | Extra items for the reviewer's checklist |
| `migrations_dir` | Commits that touch only this directory ship as a separate PR |

### Gate steps: `[[repos.gate]]`

Two shapes, mix freely:

```toml
[[repos.gate]]
name = "tests"
run = ".venv/bin/python -m pytest -q tests/"   # shell, cwd = worktree; non-zero exit = problem
when_changed = '\.py$'                          # optional: skip unless a changed path matches
timeout = 3600
rebuttable = false                              # can the build run argue this one away with evidence?

[[repos.gate]]
name = "house-rules"
python = "plugins/house_rules.py"               # module with checks(ctx) -> list[dict]
```

Command steps get `BUILD_KEY`, `BUILD_REPO`, `BUILD_WORKTREE`, `BUILD_BASE` (`origin/main`), `BUILD_RUN_DIR` and
`BUILD_PR_BODY` in their environment. Python steps get the same as attributes of `ctx`, plus `ctx.plan` and
`ctx.changed`, and return `[{"id", "summary", "rebuttable"?}]` (empty = pass). A plugin that raises is reported as
a gate problem. See [examples/plugins/commit_keys.py](examples/plugins/commit_keys.py).

### PR placeholders

In `pr.md` the build run can write `MIGRATION_PR` (this repo's migration PR) or `PR:<repo name>` (another repo's PR in
the same build); ship fills in the links. A `## Decided without you` section is moved out of the PR into
`decided.md` and sent to you.

## Layout

```
buildagent/agent.py    phases, state, CLI
buildagent/gate.py     gate steps, reviewer run, rebuttal bookkeeping
buildagent/ship.py     migration split, push, PRs, reviewers
buildagent/tools.py    claude flags and environment for each run
buildagent/prompts.py  plan / build / fix / review prompts
buildagent/config.py   TOML loading and validation
tests/                 unit tests + an end-to-end run against a toy repo with a stub claude
```

```bash
.venv/bin/python -m pytest -q
```
