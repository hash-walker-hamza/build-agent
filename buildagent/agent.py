"""Build agent: from a ticket brief with the author's decisions to open PRs with reviewers asked.

    build-agent <KEY>                   plan, build, gate, ship (resumes from the last finished phase)
    build-agent <KEY> --status          where the build is
    build-agent <KEY> --dry-run         plan, build and gate; print what would be pushed instead of pushing
    build-agent <KEY> --request-review  ask reviewers on opened PRs that weren't asked yet
    build-agent --check                 validate the config and the tools it needs

Phases (state in <home>/state/<KEY>.json; each run's output in <home>/runs/<KEY>/):
  plan   one read-only run: brief + ticket -> repos to change, branch slug, whether a migration is needed
  build  one run per repo in its own worktree: code, tests, commits, PR description (pr.md). No push possible.
  gate   the repo's configured checks, then a fresh reviewer; problems go back to the build run (fix or rebut with
         evidence), at most max_rounds rounds. Not ready after that: nothing is pushed.
  ship   migration commits as their own PR; code PRs; push; reviewers asked; post_ship hook.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as config_mod, gate, prompts, ship, tools
from .proc import last_json, run_group, sh, shell, try_lock, wait_lock, write_atomic


class Agent:
    def __init__(self, cfg: config_mod.Config, key: str, out=print):
        self.cfg, self.key, self.out = cfg, key, out

    # ---------- log, notify, state ----------

    def log(self, msg, **extra):
        self.cfg.logs.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now().isoformat(timespec="seconds"), "key": self.key, "msg": msg, **extra}
        with (self.cfg.logs / "decisions.jsonl").open("a") as f:
            f.write(json.dumps(line, default=str) + "\n")
        self.out(f"[{datetime.now():%H:%M:%S}] {msg}")

    def notify(self, text):
        """POST {"text": ...} to the configured webhook (Google Chat and Slack incoming webhooks both take it)."""
        hook = self.cfg.notify_webhook
        if not hook:
            return
        try:
            req = urllib.request.Request(hook.read_text().strip(), data=json.dumps({"text": text}).encode(),
                                         headers={"Content-Type": "application/json; charset=UTF-8"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:  # noqa: BLE001 - a chat outage must not stop a build
            self.log(f"notify failed: {type(e).__name__}")

    def load_state(self):
        f = self.cfg.state / f"{self.key}.json"
        return json.loads(f.read_text()) if f.exists() else {"key": self.key, "phase": "new", "repos": {}, "rounds": 0}

    def save_state(self, st):
        write_atomic(self.cfg.state / f"{self.key}.json", json.dumps(st, indent=1, ensure_ascii=False))

    @property
    def rdir_root(self) -> Path:
        return self.cfg.runs / self.key

    # ---------- brief ----------

    def load_brief(self):
        """(brief text, decisions section)."""
        f = self.cfg.briefs / f"{self.key}.md"
        if not f.exists():
            raise SystemExit(f"{self.key}: no brief at {f}. Write one (see README: The brief).")
        text = f.read_text()
        for heading in self.cfg.decision_headings:
            m = re.search(rf"(?im)^##\s*{re.escape(heading)}\b.*$", text)
            if m:
                rest = text[m.end():]
                nxt = re.search(r"(?m)^## ", rest)
                return text, text[m.start():m.end() + (nxt.start() if nxt else len(rest))].strip()
        raise SystemExit(f"{self.key}: the brief has none of the decision headings "
                         f"{self.cfg.decision_headings}. Write your decisions under one, then run again.")

    # ---------- model runs ----------

    def run_model(self, key, kind, prompt, cwd, allowed, rdir, mcp=()):
        rdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        cmd = tools.claude_cmd(self.cfg, prompt, allowed, mcp)
        r = run_group(cmd, cwd=str(cwd), env=tools.model_env(self.cfg, rdir, mcp), timeout=self.cfg.run_timeout)
        (rdir / f"{stamp}-{kind}.log").write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""))
        self.log(f"{kind} run exit {r.returncode}")
        return r

    # ---------- worktrees ----------

    def ensure_worktree(self, name, slug):
        """<home>/worktrees/<repo>-<KEY> on branch <KEY>-<slug> from origin/<base>; reused when already that branch."""
        repo = self.cfg.repos[name]
        wt = self.cfg.worktrees / f"{name}-{self.key}"
        branch = f"{self.key}-{slug}"
        sh("git", "fetch", "origin", repo.base, "--quiet", cwd=repo.path, check=True, timeout=300)
        if wt.exists():
            cur = sh("git", "branch", "--show-current", cwd=wt).stdout.strip()
            if cur != branch:
                raise RuntimeError(f"{wt} exists on branch {cur!r}, not {branch!r}: move it or finish it by hand")
            return wt, branch
        wt.parent.mkdir(parents=True, exist_ok=True)
        if sh("git", "rev-parse", "--verify", "--quiet", branch, cwd=repo.path).returncode == 0:
            sh("git", "worktree", "add", wt, branch, cwd=repo.path, check=True)
        else:
            sh("git", "worktree", "add", "-b", branch, wt, f"origin/{repo.base}", cwd=repo.path, check=True)
        for pattern in repo.copy_files:  # gitignored files the tests read (.env and the like)
            for src in repo.path.glob(pattern):
                dest = wt / src.relative_to(repo.path)
                if src.is_file() and not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
        for cmd in repo.setup:
            r = shell(cmd, cwd=wt, env=tools.tool_env(self.cfg), timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(f"{name} setup `{cmd}` failed: {(r.stdout + r.stderr).strip()[-300:]}")
        return wt, branch

    # ---------- phases ----------

    def phase_plan(self, st, brief, dsec):
        rdir = self.rdir_root
        rdir.mkdir(parents=True, exist_ok=True)
        repos = "\n".join(f"- {r.name}: {r.path} (base {r.base})" for r in self.cfg.repos.values())
        tracker = self.cfg.tracker_hint.format(key=self.key) if self.cfg.tracker_hint else ""
        prompt = prompts.PLAN.format(key=self.key, author=self.cfg.author, dsec=section_name(dsec), rdir=rdir,
                                     brief=brief, repos=repos, tracker=tracker,
                                     repo_names=" | ".join(f'"{n}"' for n in self.cfg.repos))
        allowed = (tools.read_rules([*(r.path for r in self.cfg.repos.values()), rdir, self.cfg.briefs])
                   + [f"Write(/{rdir}/**)", "Grep", "Glob", "Bash(git show:*)", "Bash(git log:*)", "Bash(git grep:*)"])
        r = self.run_model(self.key, "plan", prompt, rdir, allowed, rdir, mcp=self.cfg.tracker_mcp)
        plan = last_json(r.stdout, "repos")
        if plan is None:
            raise RuntimeError("the plan run printed no plan JSON")
        if plan.get("stop") or not plan["repos"]:
            raise SystemExit(f"{self.key}: the plan says not to build: {plan.get('stop')}")
        plan["slug"] = re.sub(r"[^a-z0-9-]+", "-", plan.get("slug", "change").lower()).strip("-")[:40] or "change"
        plan["repos"] = [r for r in plan["repos"] if r in self.cfg.repos]
        if not plan["repos"]:
            raise RuntimeError("the plan names no configured repo")
        (rdir / "plan.json").write_text(json.dumps(plan, indent=1))
        st.update(plan=plan, phase="planned")
        self.save_state(st)
        self.log(f"plan: {plan['repos']}, slug {plan['slug']}, migration {plan.get('migration')}")

    def build_allowed(self, name, wt, others, rdir):
        repo = self.cfg.repos[name]
        return (tools.read_rules([wt, *others, rdir, self.rdir_root, self.cfg.briefs]) + tools.write_rules([wt, rdir])
                + tools.GIT_WRITE + [a if a.startswith("Bash(") else f"Bash({a})" for a in repo.allow]
                + ["Grep", "Glob"])

    def phase_build(self, st, dsec, round_n=0, problems=None):
        plan = st["plan"]
        wts = {}
        for name in plan["repos"]:
            wt, branch = self.ensure_worktree(name, plan["slug"])
            wts[name] = (wt, branch)
            st["repos"].setdefault(name, {}).update(worktree=str(wt), branch=branch)
        self.save_state(st)
        for name in plan["repos"]:
            repo = self.cfg.repos[name]
            wt, branch = wts[name]
            rdir = self.rdir_root / name
            rdir.mkdir(parents=True, exist_ok=True)
            others = [w for n, (w, _) in wts.items() if n != name]
            other = (f"The other change(s) for this ticket are being built in {', '.join(map(str, others))} "
                     "(read them; don't edit them)." if others else "")
            mode = ""
            if problems is not None:
                mine = [p for p in problems if p.get("repo") == name]
                if not mine:
                    continue
                pfile, resp = rdir / f"problems-{round_n}.json", rdir / f"responses-{round_n}.json"
                pfile.write_text(json.dumps(mine, indent=1))
                mode = prompts.FIX_MODE.format(n=round_n, problems=pfile, responses=resp)
            migrations = prompts.MIGRATIONS.format(dir=repo.migrations_dir) if repo.migrations_dir else ""
            prompt = prompts.BUILD.format(
                key=self.key, author=self.cfg.author, repo=name, wt=wt, branch=branch, base=repo.base,
                brief_file=self.cfg.briefs / f"{self.key}.md", dsec=section_name(dsec), rdir_root=self.rdir_root,
                rdir=rdir, other=other, mode=mode, migrations=migrations,
                repo_instructions=f"\nThis repo:\n{repo.instructions}\n" if repo.instructions else "")
            r = self.run_model(self.key, f"build-{repo.short}-r{round_n}", prompt, wt,
                               self.build_allowed(name, wt, others, rdir), rdir, mcp=self.cfg.tracker_mcp)
            res = last_json(r.stdout, "done") or {"done": False, "blocked": "the build run printed no result"}
            st["repos"][name]["last_run"] = res
            if not res.get("done"):
                st.update(phase="blocked", blocked=f"{name}: {res.get('blocked')}")
                self.save_state(st)
                return False
        st["phase"] = "built"
        self.save_state(st)
        return True

    def phase_gate(self, st, dsec):
        plan = st["plan"]
        while True:
            problems = []
            for name in plan["repos"]:
                repo, info = self.cfg.repos[name], st["repos"][name]
                wt, rdir = Path(info["worktree"]), self.rdir_root / name
                if not sh("git", "rev-list", f"origin/{repo.base}..HEAD", cwd=wt).stdout.strip():
                    problems.append({"id": "no-commits", "repo": name, "source": "mechanical", "rebuttable": False,
                                     "summary": "the branch has no commits"})
                    continue
                found, results = gate.run_checks(self.cfg, repo, self.key, wt, rdir, plan)
                (rdir / f"checks-{st['rounds']}.json").write_text(json.dumps(results, indent=1))
                others = [Path(st["repos"][o]["worktree"]) for o in plan["repos"] if o != name]
                found += gate.review(self.cfg, self.run_model, self.key, repo, wt, rdir, others, results)
                title, _ = ship.read_pr_md(rdir / "pr.md")
                if not title:
                    found.append({"id": "pr-md", "source": "build", "rebuttable": False,
                                  "summary": f"{rdir / 'pr.md'} is missing or has no `TITLE: {self.key}: ...` line"})
                for p in found:
                    p["repo"] = name
                problems += found
            accepted = st.setdefault("rebutted", {})
            problems = [p for p in problems if not (p["source"] != "reviewer" and gate.rebuttal_key(p) in accepted)]
            st["open_problems"] = problems
            self.save_state(st)
            self.log(f"gate round {st['rounds']}: {len(problems)} problem(s)", problems=[p["id"] for p in problems])
            if not problems:
                st["phase"] = "ready"
                self.save_state(st)
                return True
            if st["rounds"] >= self.cfg.max_rounds:
                st["phase"] = "stuck"
                self.save_state(st)
                return False
            st["rounds"] += 1
            self.save_state(st)
            if not self.phase_build(st, dsec, round_n=st["rounds"], problems=problems):
                return None
            responses = []
            for name in plan["repos"]:
                rdir = self.rdir_root / name
                f = rdir / f"responses-{st['rounds']}.json"
                try:
                    mine = json.loads(f.read_text()) if f.exists() else []
                except ValueError:
                    mine = []
                mine = mine if isinstance(mine, list) else []
                responses += mine
                asked = {p["id"]: p["summary"] for p in problems if p["repo"] == name and p["source"] == "reviewer"}
                ans = rdir / "answered.json"
                prev = json.loads(ans.read_text()) if ans.exists() else []
                ans.write_text(json.dumps(prev + [{"finding": asked[r["id"]], **r} for r in mine
                                                  if isinstance(r, dict) and r.get("id") in asked], indent=1))
            still = gate.unresolved(problems, responses)
            by_id = {r.get("id"): r for r in responses if isinstance(r, dict)}
            for p in problems:
                r = by_id.get(p["id"])
                if p not in still and r and r.get("action") == "rebutted":
                    accepted[gate.rebuttal_key(p)] = {"summary": p["summary"][:300],
                                                      "evidence": r.get("evidence", "")[:600]}
            self.save_state(st)
            self.log(f"fix round {st['rounds']}: {len(problems) - len(still)} answered, {len(still)} open")

    def phase_ship(self, st, dry):
        prs = ship.ship(self.cfg, st, self.cfg.runs, dry, self.save_state, out=self.out)
        if dry:
            return
        st["phase"] = "shipped"
        self.save_state(st)
        links = ", ".join(f"https://github.com/{self.cfg.repos[p['repo']].github}/pull/{p['n']}"
                          for p in prs.values() if p.get("n"))
        if self.cfg.post_ship:
            r = shell(self.cfg.post_ship, cwd=self.rdir_root,
                      env=tools.tool_env(self.cfg, {"BUILD_KEY": self.key, "BUILD_PRS": json.dumps(prs)}))
            self.log(f"post_ship exit {r.returncode}")
        self.notify(f"Build {self.key}: opened {links}" + (f"; asked {', '.join(self.cfg.reviewers)} to review."
                                                           if self.cfg.reviewers else "."))
        mine = self.decided_text()
        if mine:
            self.notify(f"Build {self.key}: decided without you (not in the PR):\n{mine[:3500]}")
        self.log(f"shipped: {links}")

    def decided_text(self):
        parts = [(f.parent.name, f.read_text().strip()) for f in sorted(self.rdir_root.glob("*/decided.md"))
                 if f.read_text().strip()]
        if len(parts) == 1:
            return parts[0][1]
        return "\n\n".join(f"*{name}*\n{text}" for name, text in parts)

    # ---------- entry points ----------

    def status(self):
        st = self.load_state()
        self.out(json.dumps({k: st.get(k) for k in ("phase", "plan", "rounds", "blocked", "prs")}, indent=1))
        for p in st.get("open_problems") or []:
            self.out(f"- [{p.get('repo')}] {p['id']}: {p['summary'][:200]}")
        mine = self.decided_text()
        if mine:
            self.out("\nDecided without you (not in the PR):\n" + mine)

    def request_review(self):
        st = self.load_state()
        for pr in (st.get("prs") or {}).values():
            if pr.get("n") and not pr.get("review_requested"):
                ship.request_review(self.cfg, self.cfg.repos[pr["repo"]], pr["n"])
                pr["review_requested"] = True
        self.save_state(st)

    def run(self, dry=False):
        lock = try_lock(self.cfg.state / f"{self.key}.lock")
        if lock is None:
            raise SystemExit(f"{self.key}: a build is already running")
        st = self.load_state()
        # One build per machine at a time: test containers and dev servers of two builds starve each other.
        machine = wait_lock(self.cfg.state / "_machine.lock", self.key,
                            on_wait=lambda h: self.log(f"queued behind {h}"))
        try:
            brief, dsec = self.load_brief()
            if st["phase"] == "new":
                self.phase_plan(st, brief, dsec)
            if st["phase"] in ("planned", "blocked"):
                st.pop("blocked", None)
                if not self.phase_build(st, dsec):
                    self.notify(f"Build {self.key} stopped while building: {st.get('blocked')}")
                    return 1
            if st["phase"] in ("built", "stuck"):
                if st["phase"] == "stuck":
                    st["rounds"] = 0
                gated = self.phase_gate(st, dsec)
                if gated is None:
                    self.notify(f"Build {self.key} stopped in a fix round: {st.get('blocked')}")
                    return 1
                if not gated:
                    open_ = "; ".join(f"{p['id']}: {p['summary'][:90]}" for p in st.get("open_problems", [])[:5])
                    self.notify(f"Build {self.key}: not ready after {self.cfg.max_rounds} fix rounds, nothing pushed. "
                                f"Open: {open_}")
                    return 1
            if st["phase"] == "ready":
                self.phase_ship(st, dry)
            return 0
        except RuntimeError as e:
            self.log(f"failed: {e}")
            self.notify(f"Build {self.key} failed: {str(e)[:300]}")
            return 1
        finally:
            machine.close()
            lock.close()


def section_name(dsec: str) -> str:
    return dsec.splitlines()[0].lstrip("# ").strip()


def check(cfg: config_mod.Config, out=print) -> int:
    """Validates what a build needs before it is started. Returns the number of problems."""
    bad = 0

    def say(ok, what):
        nonlocal bad
        bad += 0 if ok else 1
        out(f"{'ok  ' if ok else 'FAIL'} {what}")

    for tool in (cfg.claude, "git", "gh"):
        say(shutil.which(tool, path=cfg.path_env) is not None, f"`{tool}` on PATH")
    gh = sh("gh", "auth", "status") if shutil.which("gh") else None
    say(gh is not None and gh.returncode == 0, "gh is logged in (used only by the ship phase)")
    say(cfg.briefs.exists(), f"brief dir {cfg.briefs}")
    for repo in cfg.repos.values():
        ok = (repo.path / ".git").exists()
        say(ok, f"repo {repo.name}: {repo.path} is a git checkout")
        if ok:
            r = sh("git", "rev-parse", "--verify", "--quiet", f"origin/{repo.base}", cwd=repo.path)
            say(r.returncode == 0, f"repo {repo.name}: origin/{repo.base} exists")
        for step in repo.gate:
            if step.python:
                say(step.python.exists(), f"repo {repo.name}: gate plugin {step.python}")
    if cfg.notify_webhook:
        say(cfg.notify_webhook.exists(), f"webhook file {cfg.notify_webhook}")
    return bad


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg_path = None
    if "--config" in args:
        i = args.index("--config")
        cfg_path = Path(args[i + 1])
        del args[i:i + 2]
    cfg = config_mod.load(cfg_path)
    if args[:1] == ["--check"]:
        return 1 if check(cfg) else 0
    if not args or not cfg.key_pattern.match(args[0]):
        print(__doc__)
        return 2
    agent = Agent(cfg, args[0])
    if "--status" in args:
        agent.status()
        return 0
    if "--request-review" in args:
        agent.request_review()
        return 0
    return agent.run(dry="--dry-run" in args)
