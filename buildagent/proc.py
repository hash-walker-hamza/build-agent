"""Process helpers. POSIX only (macOS, Linux, WSL): process groups and flock."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
from pathlib import Path


def sh(*args, cwd=None, env=None, timeout=600, check=False, input=None):
    r = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                       input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, args[:4]))}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def shell(cmd: str, cwd, env=None, timeout=1800):
    """A config-supplied command line, run by the user's shell in its own process group."""
    return run_group(["/bin/sh", "-c", cmd], cwd=cwd, env=env, timeout=timeout)


def run_group(cmd, cwd, env, timeout):
    """subprocess.run in its own process group, so a timeout also kills the children (test runners, servers)."""
    p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return subprocess.CompletedProcess(cmd, -9, out, (err or "") + f"\ntimed out after {timeout}s")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def last_json(text, need):
    """The last JSON object in `text` that has key `need` (one line or pretty-printed)."""
    dec, i = json.JSONDecoder(), (text or "").rfind("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
            if isinstance(obj, dict) and need in obj:
                return obj
        except ValueError:
            pass
        i = text.rfind("{", 0, i)
    return None


def try_lock(path: Path):
    """An exclusive lock held for the life of the process, or None when another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    return fd


def wait_lock(path: Path, label: str, on_wait=None):
    """Queues behind the holder of `path`, then writes `label` into it. Returns the open fd; keep it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.seek(0)
        holder = fd.read().strip() or "another build"
        if on_wait:
            on_wait(holder)
        fcntl.flock(fd, fcntl.LOCK_EX)
    fd.seek(0)
    fd.truncate()
    fd.write(label)
    fd.flush()
    return fd


def write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
