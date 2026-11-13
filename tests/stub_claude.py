#!/usr/bin/env python3
"""Stands in for `claude -p` in tests: acts out the plan, build, fix and review runs on a toy repo, and records each
call's argv and environment so tests can check what a real run would have been given."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

prompt = sys.argv[sys.argv.index("-p") + 1]
calls = Path(os.environ["STUB_CALLS"])
with calls.open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                        "env": {k: os.environ.get(k) for k in ("GH_TOKEN", "GH_CONFIG_DIR", "GIT_CONFIG_KEY_0",
                                                               "ENABLE_CLAUDEAI_MCP_SERVERS")}}) + "\n")


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


if prompt.startswith("You plan"):
    rdir = re.search(r"to (\S+)/ticket\.md", prompt).group(1)
    Path(rdir, "ticket.md").write_text("Say hello.\n")
    print(json.dumps({"repos": ["toy"], "slug": "Say Hello!", "migration": True, "summary": "says hello"}))
elif prompt.startswith("You are") and "build agent" in prompt:
    key = re.search(r"Implement ticket (\S+)", prompt).group(1)
    pr = re.search(r"write it to (\S+/pr\.md)", prompt).group(1)
    fix = re.search(r"Write (\S+/responses-\d+\.json)", prompt)
    if fix:
        Path("hello.txt").write_text("hello, fixed\n")
        git("commit", "-qam", f"{key}: fix greeting")
        problems = json.loads(Path(re.search(r"problems in (\S+\.json)", prompt).group(1)).read_text())
        Path(fix.group(1)).write_text(json.dumps([{"id": p["id"], "action": "fixed", "evidence": "rewrote it"}
                                                  for p in problems]))
    else:
        Path("migrations").mkdir(exist_ok=True)
        Path("migrations/001_hello.sql").write_text("create table hello (id int);\n")
        git("add", "migrations")
        git("commit", "-qm", f"{key}: add hello table")
        Path("hello.txt").write_text("hello, draft\n")
        git("add", "hello.txt")
        git("commit", "-qm", f"{key}: say hello")
        Path(pr).write_text(f"TITLE: {key}: Say hello\nAdds a greeting.\n\nRelease gate: MIGRATION_PR\n\n"
                            "## Decided without you\n- picked the word hello\n")
    print(json.dumps({"done": True, "summary": "ok"}))
elif prompt.startswith("Review"):
    print(json.dumps({"findings": [{"file": "hello.txt", "line": 1, "severity": "low", "summary": "nit"}]}))
else:
    print("unknown prompt", file=sys.stderr)
    sys.exit(3)
