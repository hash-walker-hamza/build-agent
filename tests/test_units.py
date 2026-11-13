from pathlib import Path

import pytest

from buildagent import config, gate, tools


def write(tmp_path, body):
    f = tmp_path / "c.toml"
    f.write_text(body)
    return f


def test_config_resolves_relative_paths_against_the_config_file(tmp_path):
    cfg = config.load(write(tmp_path, """
[agent]
home = "state"
[[repos]]
name = "a"
path = "../a"
github = "o/a"
  [[repos.gate]]
  name = "p"
  python = "plugins/p.py"
"""))
    assert cfg.home == tmp_path / "state"
    assert cfg.repos["a"].path == (tmp_path / "../a").resolve()
    assert cfg.repos["a"].gate[0].python == tmp_path / "plugins/p.py"


@pytest.mark.parametrize("repos,err", [
    ("", "no [[repos]]"),
    ('[[repos]]\nname = "a"\npath = "/x"\n', "needs `github`"),
    ('[[repos]]\nname = "a"\npath = "/x"\ngithub = "o/a"\n[[repos]]\nname = "a"\npath = "/y"\ngithub = "o/b"\n',
     "listed twice"),
    ('[[repos]]\nname = "a"\npath = "/x"\ngithub = "o/a"\n  [[repos.gate]]\n  name = "g"\n  run = "x"\n  python = "y"\n',
     "exactly one of run/python"),
])
def test_config_refuses_incomplete_repos(tmp_path, repos, err):
    with pytest.raises(config.ConfigError, match=err.replace("[", r"\[").replace("]", r"\]")):
        config.load(write(tmp_path, repos))


def test_partly_allowed_connector_denies_its_other_known_tools(tmp_path):
    cfg = config.load(write(tmp_path, """
[tools]
connector_servers = ["Jira", "Gmail"]
[tools.connector_known]
Jira = ["getIssue", "editIssue"]
[[repos]]
name = "a"
path = "/x"
github = "o/a"
"""))
    out = tools.flags(cfg, builtins=["Read(//x/**)"], mcp=["Jira__getIssue"])
    deny = out[out.index("--disallowedTools") + 1:]
    assert "mcp__claude_ai_Gmail" in deny and "mcp__claude_ai_Jira__editIssue" in deny
    assert "mcp__claude_ai_Jira__getIssue" not in deny
    assert out[:2] == ["--tools", "Read"]


@pytest.mark.parametrize("response,open_", [
    (None, True),
    ({"action": "fixed"}, False),
    ({"action": "rebutted", "evidence": "short"}, True),
    ({"action": "rebutted", "evidence": "x" * 30}, False),
])
def test_unresolved(response, open_):
    p = {"id": "p1", "summary": "s", "rebuttable": True}
    responses = [dict(response, id="p1")] if response else []
    assert (gate.unresolved([p], responses) == [p]) is open_


def test_a_mechanical_problem_cannot_be_rebutted():
    p = {"id": "p1", "summary": "s", "rebuttable": False}
    assert gate.unresolved([p], [{"id": "p1", "action": "rebutted", "evidence": "x" * 30}]) == [p]


def test_command_step_skips_when_nothing_matches(tmp_path):
    cfg = config.load(write(tmp_path, '[[repos]]\nname = "a"\npath = "/x"\ngithub = "o/a"\n'))
    ctx = gate.GateContext(key="K-1", repo="a", worktree=str(tmp_path), base="origin/main", run_dir=str(tmp_path),
                           pr_body_file="", plan={}, changed=["README.md"])
    step = config.GateStep(name="py", run="exit 1", when_changed=r"\.py$")
    assert gate.run_step(cfg, step, ctx) == []
    step.when_changed = None
    assert gate.run_step(cfg, step, ctx)[0]["id"] == "gate:py"


def test_a_crashing_plugin_is_a_problem_not_a_crash(tmp_path):
    cfg = config.load(write(tmp_path, '[[repos]]\nname = "a"\npath = "/x"\ngithub = "o/a"\n'))
    plugin = tmp_path / "boom.py"
    plugin.write_text("def checks(ctx):\n    raise ValueError('nope')\n")
    ctx = gate.GateContext(key="K-1", repo="a", worktree=str(tmp_path), base="origin/main", run_dir=str(tmp_path),
                           pr_body_file="", plan={}, changed=[])
    [p] = gate.run_step(cfg, config.GateStep(name="b", python=Path(plugin)), ctx)
    assert p["id"] == "gate:b-crashed" and "nope" in p["summary"] and not p["rebuttable"]
