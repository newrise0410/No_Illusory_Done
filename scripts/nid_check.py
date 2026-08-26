#!/usr/bin/env python3
"""nid_check.py — No Illusory Done checker.

Trust model: NOTHING on disk written by an LLM is trusted as evidence.
Every verdict-producing command (--run, --ci, --report, --mutate) re-executes
the oracles in this invocation. STATE.md / evidence/ are outputs for humans,
never inputs to a verdict.

Subcommands (exit 0 only on the stated success):
  --status LEDGER      parse ledger + PLAN.md; traceability, H-line rules
  --red LEDGER         run every gate, require RED, write FREEZE.sha256
  --verify-freeze      hashes match working tree; FREEZE committed exactly once
  --run LEDGER         re-run gates (freeze verified before AND after); caps enforced
  --ci CI.md           re-run Stage A, then validate CI.md pointers (bound to SUBJECT)
  --mutate LEDGER      python AST mutants of changed source; each must be killed
  --report             re-run everything and print the machine report
  --hook               Stop-hook entry: no ledger -> exit 0; else --run, exit 2 on unmet/handoff

PLAN.md:
  R1: <atomic requirement>
  H1: <state> | FALSIFIER: <observation> | SUBJECT: <path-prefix or cmd-prefix>[, ...]
  SETUP: <cmd>           max_iterations: 8    stall_iters: 3
Gate:
  - [ ] G1: <state>
    CHECK / EXPECT / CWD / TIMEOUT / RETRIES / FILES / KIND / RED / COVERS / ENV
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SELF = Path(__file__).resolve()
GATE_RE = re.compile(r"^- \[( |x|X)\] (G\d+):\s*(.+?)\s*$")
FIELD_RE = re.compile(r"^\s*([A-Z]+):\s*(.*?)\s*$")
FIELDS = {"CHECK", "EXPECT", "CWD", "TIMEOUT", "RETRIES", "FILES", "KIND", "RED", "EVIDENCE", "COVERS", "ENV"}
# Files that silently change how test runners / interpreters behave. If one is added or
# modified after the freeze and is not itself frozen, the oracle is no longer the oracle.
INFLUENCE = re.compile(r"(^|/)(conftest\.py|sitecustomize\.py|usercustomize\.py|[^/]*\.pth|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|"
                       r"\.env[^/]*|package\.json|jest\.config\.[^/]+|vitest\.config\.[^/]+|babel\.config\.[^/]+|\.babelrc|tsconfig[^/]*\.json|"
                       r"\.npmrc|\.mocharc[^/]*|Makefile|\.bashrc|\.zshrc|\.profile|__init__\.py)$")
CLEAN_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "SHELL")
# SUBJECT commands whose output does not depend on the change under review.
CONSTANT_CMD = re.compile(r"^(git\s+(log|rev-parse|rev-list|describe|branch|remote|config|status|tag|show-ref|symbolic-ref)|date|whoami|pwd|uname|hostname|id|ls|wc|cat|head|tail|stat|md5|shasum|sha256sum|python[3]?\s+--version|node\s+-v)\b")
R_RE = re.compile(r"^(R\d+):\s*(.+?)\s*$")
H_RE = re.compile(r"^(H\d+):\s*(.+?)\s*$")
CAP_RE = re.compile(r"^(max_iterations|stall_iters|max_ci_attempts|max_supersedes|max_gates_per_r):\s*(\d+)\s*$")
VAGUE = re.compile(r"looks good|covers the feature|works correctly|as expected|properly|correctly", re.I)
# Tokens in a CHECK that look like paths.
PATHISH = re.compile(r"(?<![\w-])((?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)*\.[A-Za-z0-9]{1,8}|(?:\./|\.\./)[\w./-]+)")
BAD_CHECK = [
    (re.compile(r"(^|[;&|(]\s*)(echo|printf|true|false|command|eval|exec|source|env|xargs|nohup|nice|time|builtin)\b"), "shell no-op/indirection"),
    (re.compile(r"(^|[;&|(]\s*):(\s|$)"), ":"),
    (re.compile(r"\b(sh|bash|zsh|dash)\s+-c\b"), "nested shell"),
    (re.compile(r"\bexit\s+0\b"), "exit 0"),
    (re.compile(r"passWithNoTests|--no-verify|\|\|\s*true"), "skip/soften flag"),
    (re.compile(r"python[3]?\s+-c\b"), "python -c"),
    (re.compile(r"(^|\s)(touch|cp|mv|rm|tee|sed\s+-i|>>?)\s"), "mutating command in CHECK"),
    (re.compile(r"[$`]|<<|(^|[;&|(]\s*)\w+=\S"), "shell expansion/heredoc/assignment (use a repo-owned script)"),
]


# --------------------------------------------------------------------------
def die(msg: str, code: int = 2) -> None:
    print(f"NID FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(p: Path) -> str:
    return sha(p.read_bytes())


class Ctx:
    """All paths resolve against the repo root (parent of .no-illusory-done)."""

    def __init__(self, ledger: Path | None):
        if ledger is None:
            here = Path.cwd().resolve()
            for d in (here, *here.parents):
                if (d / ".no-illusory-done" / "LEDGER.md").exists():
                    ledger = d / ".no-illusory-done" / "LEDGER.md"; break
            else:
                die("no .no-illusory-done/LEDGER.md in cwd or any parent")
        self.ledger = ledger.resolve()
        self.nid = self.ledger.parent
        self.root = self.nid.parent
        self.plan = self.nid / "PLAN.md"
        self.freeze = self.nid / "FREEZE.sha256"
        self.state = self.nid / "STATE.md"
        self.evidence = self.nid / "evidence"
        self.ci = self.nid / "CI.md"
        os.chdir(self.root)

    def rel(self, p: Path) -> str:
        return os.path.relpath(p.resolve(), self.root)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
class Gate:
    def __init__(self, gid, title, checked):
        self.id, self.title, self.checked, self.f = gid, title, checked, {}

    kind = property(lambda s: s.f.get("KIND", "cmd"))
    files = property(lambda s: [x.strip() for x in s.f.get("FILES", "").split(",") if x.strip()])
    env = property(lambda s: [x.strip() for x in s.f.get("ENV", "").split(",") if x.strip()])
    covers = property(lambda s: [x.strip() for x in s.f.get("COVERS", "").split(",") if x.strip()])


def is_regex(e): return len(e) >= 2 and e.startswith("/") and e.endswith("/")


def expect_match(exp: str, output: str) -> bool:
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if not lines:
        return False
    last = lines[-1]
    if is_regex(exp):
        try:
            return re.fullmatch(exp[1:-1], last) is not None
        except re.error:
            return False
    return last == exp.strip()


def parse_plan(ctx: Ctx):
    if not ctx.plan.exists():
        die("PLAN.md missing")
    reqs, highs, setup = {}, {}, []
    caps = {"max_iterations": 8, "stall_iters": 3, "max_ci_attempts": 3, "max_supersedes": 1, "max_gates_per_r": 4}
    for ln, raw in enumerate(ctx.plan.read_text(encoding="utf-8").replace("\r", "").splitlines(), 1):
        line = raw.strip().lstrip("-* ").strip()
        m = CAP_RE.match(line)
        if m:
            caps[m.group(1)] = int(m.group(2)); continue
        if line.startswith("SETUP:"):
            setup.append(line[6:].strip()); continue
        m = R_RE.match(line)
        if m:
            if m.group(1) in reqs: die(f"PLAN.md duplicate {m.group(1)} (line {ln})")
            if len(m.group(2)) < 8 or VAGUE.search(m.group(2)):
                die(f"PLAN.md {m.group(1)}: too vague (line {ln})")
            reqs[m.group(1)] = m.group(2); continue
        m = H_RE.match(line)
        if m:
            hid, body = m.group(1), m.group(2)
            if hid in highs: die(f"PLAN.md duplicate {hid} (line {ln})")
            parts = [p.strip() for p in body.split("|")]
            kv = {"STATE": parts[0]}
            for p in parts[1:]:
                k, _, v = p.partition(":")
                kv[k.strip()] = v.strip()
            for k in ("FALSIFIER", "SUBJECT"):
                if not kv.get(k): die(f"PLAN.md {hid}: missing '| {k}: ...' (line {ln})")
            if len(kv["STATE"]) < 8 or len(kv["FALSIFIER"]) < 8:
                die(f"PLAN.md {hid}: state/FALSIFIER too vague (line {ln})")
            if VAGUE.search(body): die(f"PLAN.md {hid}: forbidden vague phrase (line {ln})")
            if falsifier_is_command(kv["FALSIFIER"]):
                die(f"PLAN.md {hid}: FALSIFIER is a command -> make it a KIND: cmd gate")
            subjects = [s.strip() for s in kv["SUBJECT"].split(",") if s.strip()]
            for s in subjects:
                if s.startswith("$ "):
                    if len(s) < 6 or any(rx.search(s[2:]) for rx, _ in BAD_CHECK):
                        die(f"PLAN.md {hid}: SUBJECT command too short or forbidden: {s}")
                    if CONSTANT_CMD.match(s[2:].strip()):
                        die(f"PLAN.md {hid}: SUBJECT command output does not depend on the change ({s}); cite a command that observes the product")
                    continue
                sp = (ctx.root / s)
                if not inside_repo(ctx, sp) or not sp.is_file():
                    die(f"PLAN.md {hid}: SUBJECT must be an existing regular file inside the repo (not a directory, not a symlink out): {s}")
                if str(sp.resolve()).startswith(str(ctx.nid)):
                    die(f"PLAN.md {hid}: SUBJECT may not be inside .no-illusory-done")
            kv["SUBJECTS"] = subjects
            highs[hid] = kv
    if not reqs:
        die("PLAN.md has no R1.. requirement clauses")
    return reqs, highs, setup, caps


def inside_repo(ctx: Ctx, p: Path) -> bool:
    """True iff p (and every symlink it goes through) resolves inside the repo root."""
    try:
        r = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not str(r).startswith(str(ctx.root) + os.sep):
        return False
    # walk components to reject symlinks that point outside
    cur = ctx.root
    for part in os.path.relpath(p, ctx.root).split(os.sep):
        cur = cur / part
        if cur.is_symlink() and not str(cur.resolve()).startswith(str(ctx.root) + os.sep):
            return False
    return True


def falsifier_is_command(f: str) -> bool:
    if re.search(r"[$|`]|&&|\.\/|^-|\s-[a-zA-Z]\b", f):
        return True
    first = f.split()[0] if f.split() else ""
    return bool(shutil.which(first)) and len(f.split()) > 1


def parse_ledger(ctx: Ctx) -> list[Gate]:
    text = ctx.ledger.read_text(encoding="utf-8").replace("\r", "") if ctx.ledger.exists() else ""
    if not text.strip(): die("ledger missing or empty")
    gates, cur = [], None
    for ln, line in enumerate(text.splitlines(), 1):
        m = GATE_RE.match(line)
        if m:
            cur = Gate(m.group(2), m.group(3), m.group(1).lower() == "x"); gates.append(cur); continue
        fm = FIELD_RE.match(line)
        if fm and cur is not None and fm.group(1) in FIELDS:
            if fm.group(1) in cur.f: die(f"{cur.id}: duplicate field {fm.group(1)} (line {ln})")
            cur.f[fm.group(1)] = fm.group(2)
    if not gates: die("zero gates")
    seen = set()
    for g in gates:
        if g.id in seen: die(f"duplicate gate id {g.id}")
        seen.add(g.id)
        if re.match(r"^(run|execute|check|test|verify|ensure|make sure)\b", g.title, re.I):
            die(f"{g.id}: title is an activity, not a state: {g.title!r}")
        if g.kind not in ("cmd", "llm-judge"): die(f"{g.id}: bad KIND")
        if not g.covers: die(f"{g.id}: missing COVERS")
        if g.kind == "llm-judge":
            if "CHECK" in g.f or "EXPECT" in g.f: die(f"{g.id}: llm-judge gates must not have CHECK/EXPECT")
            continue
        chk, exp = g.f.get("CHECK", "").strip(), g.f.get("EXPECT", "").strip()
        if not chk or not exp: die(f"{g.id}: missing CHECK or EXPECT")
        for rx, name in BAD_CHECK:
            if rx.search(chk): die(f"{g.id}: forbidden CHECK pattern ({name})")
        lit = exp[1:-1] if is_regex(exp) else exp
        if lit and lit in chk: die(f"{g.id}: CHECK contains EXPECT text (self-fulfilling)")
        if is_regex(exp):
            try:
                rx = re.compile(lit)
            except re.error as e:
                die(f"{g.id}: EXPECT regex invalid: {e}")
            for probe in ("", "FAIL", "error", "x", "NOT THE REQUEST", "Traceback"):
                if rx.fullmatch(probe): die(f"{g.id}: EXPECT regex is vacuous (matches {probe!r})")
        if len(lit.strip()) < 3: die(f"{g.id}: EXPECT too short to be a success marker")
        for kv in g.env:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*=[^\s$`]*", kv) or kv.split("=")[0] in ("PATH", "PYTHONPATH", "NODE_PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONSTARTUP", "BASH_ENV", "ENV"):
                die(f"{g.id}: ENV entry not allowed: {kv!r} (literal KEY=value only; no PATH/PYTHONPATH/NODE_PATH/LD_PRELOAD)")
        if g.f.get("RED", "required") not in ("required", "pass-ok"): die(f"{g.id}: bad RED value")
        try:
            t, r = int(g.f.get("TIMEOUT", "300")), int(g.f.get("RETRIES", "0"))
        except ValueError:
            die(f"{g.id}: TIMEOUT/RETRIES must be integers")
        if t <= 0 or not 0 <= r <= 2: die(f"{g.id}: TIMEOUT >0, RETRIES 0..2")
        cwd = ctx.root / g.f.get("CWD", ".")
        if not cwd.is_dir() or ctx.rel(cwd).startswith(".."): die(f"{g.id}: CWD not a dir inside repo")
        # Every existing file the CHECK names must be frozen (FILES). A path that does not
        # exist yet is product output the implementation will create.
        declared = set(g.files)
        toks = set(PATHISH.findall(chk)) | {t.strip("\"'") for t in re.split(r"[\s;&|()<>]+", chk) if t.strip("\"'")}
        for tok in toks:
            if tok.startswith("-"): continue
            p = cwd / tok
            if p.is_file() and not str(p.resolve()).startswith(str(ctx.nid)):
                relp = ctx.rel(p)
                if relp not in declared and tok not in declared:
                    die(f"{g.id}: CHECK references existing file {relp} not in FILES (existing inputs must be frozen; delete it before --red if the implementation must regenerate it)")
        if not g.files: die(f"{g.id}: FILES is empty — a runnable gate must depend on at least one frozen oracle file")
        for f in g.files:
            fp = ctx.root / f
            if not fp.is_file() or not inside_repo(ctx, fp): die(f"{g.id}: FILES entry missing, not a regular file, or symlinks outside the repo: {f}")
    checks = [g.f.get("CHECK", "").strip() for g in gates if g.kind == "cmd"]
    if len(checks) != len(set(checks)): die("two gates have identical CHECK commands (duplicate observation)")
    if all(g.kind == "llm-judge" for g in gates):
        die("all gates are llm-judge; at least one runnable gate required")
    if not any(g.kind == "cmd" and g.f.get("RED", "required") == "required" for g in gates):
        die("at least one gate must be RED: required (otherwise nothing proves new behavior)")
    reqs, highs, _, _ = parse_plan(ctx)
    covered = set()
    for g in gates:
        for r in g.covers:
            if r not in reqs: die(f"{g.id}: COVERS unknown requirement {r}")
        covered.update(g.covers)
    missing = sorted(set(reqs) - covered, key=lambda x: int(x[1:]))
    if missing: die(f"requirements with no gate: {','.join(missing)}")
    _, _, _, caps = parse_plan(ctx)
    for r in reqs:
        n = sum(1 for g in gates if r in g.covers)
        if n > caps["max_gates_per_r"]: die(f"{r} is covered by {n} gates (max_gates_per_r={caps['max_gates_per_r']}): traceability dilution")
    return gates


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def run_gate(ctx: Ctx, g: Gate, record=True) -> dict:
    cwd = (ctx.root / g.f.get("CWD", ".")).resolve()
    timeout, retries = int(g.f.get("TIMEOUT", "300")), int(g.f.get("RETRIES", "0"))
    attempts = []
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            proc = subprocess.Popen(["bash", "-o", "errexit", "-o", "pipefail", "-o", "nounset", "-c", g.f["CHECK"]],
                                    cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=clean_env(g), start_new_session=True)
            try:
                raw, _ = proc.communicate(timeout=timeout)
                out, code, to = raw.decode("utf-8", "replace"), proc.returncode, False
            except subprocess.TimeoutExpired:
                kill_group(proc)
                raw, _ = proc.communicate()
                out, code, to = raw.decode("utf-8", "replace") + "\n[NID TIMEOUT: process group killed]", -1, True
        except OSError as e:
            out, code, to = f"[NID EXEC ERROR] {e}", -1, False
        em = expect_match(g.f["EXPECT"], out)
        ok = code == 0 and not to and em
        attempts.append({"attempt": attempt, "exit": code, "timeout": to, "expect_match": em,
                         "sha256": sha(out.encode()), "bytes": len(out.encode()),
                         "seconds": round(time.time() - t0, 2), "pass": ok})
        if record:
            ctx.evidence.mkdir(parents=True, exist_ok=True)
            (ctx.evidence / f"{g.id}.out").write_text(out, encoding="utf-8")
        if ok: break
    last = attempts[-1]
    return {"id": g.id, "pass": last["pass"], "exit": last["exit"], "expect_match": last["expect_match"],
            "sha256": last["sha256"], "bytes": last["bytes"], "attempts": attempts,
            "flaky": last["pass"] and len(attempts) > 1}


def kill_group(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


CRASH_KILL = re.compile(r"(ImportError|ModuleNotFoundError|SyntaxError|NameError|IndentationError|cannot import name)")


def clean_env(g: Gate | None = None) -> dict:
    """Nothing inherited from the caller's shell except a whitelist; gate ENV: values are frozen literals."""
    env = {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}
    env.update({"PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "NODE_OPTIONS": "", "CI": "1", "NID": "1"})
    if g is not None:
        for kv in g.env:
            k, _, v = kv.partition("="); env[k] = v
    return env


def influence_check(ctx: Ctx, gates: list[Gate]) -> None:
    """Refuse if a runner/interpreter-influencing file was added or changed since the freeze and is not frozen."""
    frozen = set(read_freeze(ctx)[0])
    fcommit = git(ctx, "log", "-1", "--format=%H", "--", ctx.rel(ctx.freeze)).stdout.strip()
    bad = [f for f in changed_files(ctx, fcommit) if INFLUENCE.search(f) and f not in frozen]
    if bad:
        die(f"runner-influencing files changed since the freeze and are not frozen: {', '.join(bad)} -> the oracle is no longer the oracle")


def run_all(ctx: Ctx, gates: list[Gate], record=True) -> dict[str, dict]:
    return {g.id: run_gate(ctx, g, record) for g in gates if g.kind == "cmd"}


# --------------------------------------------------------------------------
# Freeze
# --------------------------------------------------------------------------
def freeze_targets(ctx: Ctx, gates: list[Gate]) -> list[Path]:
    paths = {ctx.ledger, ctx.plan, SELF}
    for g in gates:
        for f in g.files:
            paths.add((ctx.root / f).resolve())
    return sorted(paths)


def read_freeze(ctx: Ctx):
    if not ctx.freeze.exists(): die("FREEZE.sha256 missing (run --red)")
    files, reds, sup = {}, {}, []
    for ln, line in enumerate(ctx.freeze.read_text().splitlines(), 1):
        parts = line.split()
        if not parts: continue
        if parts[0] == "SUPERSEDE" and len(parts) >= 3:
            sup.append(line[len("SUPERSEDE "):]); continue
        if parts[0] == "RED" and len(parts) == 4 and re.fullmatch(r"[0-9a-f]{64}", parts[2]):
            if parts[1] in reds: die(f"FREEZE line {ln}: duplicate RED {parts[1]}")
            reds[parts[1]] = {"sha": parts[2], "exit": int(parts[3])}
        elif len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            if parts[1] in files: die(f"FREEZE line {ln}: duplicate file {parts[1]}")
            files[parts[1]] = parts[0]
        else:
            die(f"FREEZE line {ln}: malformed: {line!r}")
    if not files: die("FREEZE.sha256 has no file hashes")
    return files, reds, sup


def git(ctx: Ctx, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ctx.root), *args], capture_output=True, text=True)


def verify_freeze(ctx: Ctx, gates: list[Gate] | None = None, quiet=False) -> bool:
    files, reds, sup = read_freeze(ctx)
    problems = []
    for f, h in files.items():
        p = ctx.root / f
        if not p.exists(): problems.append(f"missing {f}")
        elif sha_file(p) != h: problems.append(f"modified {f}")
    for must in (ctx.rel(ctx.ledger), ctx.rel(ctx.plan), ctx.rel(SELF)):
        if must not in files: problems.append(f"{must} not in freeze")
    if gates is not None:
        for g in gates:
            for f in g.files:
                if f not in files and ctx.rel(ctx.root / f) not in files:
                    problems.append(f"{g.id} FILES {f} not in freeze")
            if g.kind == "cmd" and g.id not in reds:
                problems.append(f"{g.id} has no RED record")
    if git(ctx, "rev-parse", "--is-inside-work-tree").returncode != 0:
        problems.append("not a git repository (freeze cannot be witnessed)")
    else:
        relf = ctx.rel(ctx.freeze)
        head = git(ctx, "show", f"HEAD:{relf}")
        if head.returncode != 0:
            problems.append("FREEZE.sha256 not committed at HEAD")
        elif sha(head.stdout.encode()) != sha_file(ctx.freeze):
            problems.append("FREEZE.sha256 differs from HEAD (re-hash detected)")
        else:
            n = int(git(ctx, "rev-list", "--count", "HEAD", "--", relf).stdout.strip() or 0)
            if n != 1 + len(sup):
                problems.append(f"FREEZE.sha256 touched by {n} commits but {len(sup)} SUPERSEDE declared (undeclared re-freeze)")
            elif sup:
                _, _, _, caps = parse_plan(ctx)
                if len(sup) > caps["max_supersedes"]:
                    problems.append(f"{len(sup)} re-freezes exceed max_supersedes={caps['max_supersedes']}: human review required")
                if not quiet:
                    for x in sup: print(f"FREEZE SUPERSEDED: {x}")
    if not quiet:
        for pr in problems: print(f"FREEZE MISMATCH: {pr}")
        print("FREEZE: match" if not problems else "FREEZE: mismatch")
    return not problems


def cmd_red(ctx: Ctx, supersede: str | None = None) -> None:
    gates = parse_ledger(ctx)
    sup = []
    if ctx.freeze.exists():
        if not supersede or len(supersede.strip()) < 12:
            die("FREEZE.sha256 already exists. Re-freezing requires --supersede '<reason>' (ledger-defect handoff); it is recorded permanently.")
        _, _, sup = read_freeze(ctx)
        _, _, _, caps = parse_plan(ctx)
        if len(sup) + 1 > caps["max_supersedes"]:
            die(f"re-freeze #{len(sup) + 1} exceeds max_supersedes={caps['max_supersedes']}: a human must raise the cap in PLAN.md (frozen) — HANDOFF")
        prev = git(ctx, "log", "-1", "--format=%h", "--", ctx.rel(ctx.freeze)).stdout.strip() or "uncommitted"
        sup.append(f"{len(sup) + 1} prev={prev} {supersede.strip()}")
    bad, reds = [], []
    head_tracked = set(git(ctx, "ls-tree", "-r", "--name-only", "HEAD").stdout.split())
    for g in gates:
        if g.kind != "cmd": continue
        r = run_gate(ctx, g, record=False)
        if r["pass"]:
            if g.f.get("RED", "required") == "required":
                bad.append(g.id)
            elif not all(f in head_tracked for f in g.files):
                die(f"{g.id}: RED: pass-ok requires FILES already committed at HEAD before this freeze (a regression gate)")
        elif g.f.get("RED") == "pass-ok":
            die(f"{g.id}: RED: pass-ok but the gate fails now; it is not a regression gate")
        reds.append(f"RED {g.id} {r['sha256']} {r['exit']}")
        print(f"{g.id}: {'GREEN (not RED!)' if r['pass'] else 'RED'} exit={r['exit']}")
    if bad: die(f"gates already pass before implementation: {','.join(bad)}")
    lines = [f"{sha_file(p)}  {ctx.rel(p)}" for p in freeze_targets(ctx, gates)]
    ctx.freeze.write_text("\n".join(lines + reds + [f"SUPERSEDE {x}" for x in sup]) + "\n")
    print(f"RED recorded + froze {len(lines)} files -> {ctx.rel(ctx.freeze)}; commit it now (one commit per freeze).")


# --------------------------------------------------------------------------
# STATE.md (human-facing output only; never read for verdicts)
# --------------------------------------------------------------------------
STATE_ROW = re.compile(r"^\|\s*(G\d+)\s*\|\s*([\w-]+)\s*\|\s*(\w+)\s*\|(.*)\|\s*$")


def read_state(ctx: Ctx):
    st, it, stall = {}, 0, 0
    if ctx.state.exists():
        for line in ctx.state.read_text().splitlines():
            m = STATE_ROW.match(line)
            if m: st[m.group(1)] = {"E": m.group(2), "B": m.group(3), "note": m.group(4).strip()}
            elif line.startswith("iteration:"):
                try: it = int(line.split(":")[1])
                except ValueError: die("STATE.md malformed (iteration); delete it to reset")
            elif line.startswith("stall:"):
                try: stall = int(line.split(":")[1])
                except ValueError: die("STATE.md malformed (stall); delete it to reset")
            elif line.startswith("|") and not STATE_ROW.match(line) and not re.match(r"^\|\s*(id|-+)\s*\|", line):
                die(f"STATE.md malformed row: {line!r}; delete it to reset")
    return st, it, stall


def write_state(ctx: Ctx, gates, results, it, stall, old):
    rows = ["| id | E (evidence) | B (belief) | note |", "|----|----|----|----|"]
    for g in gates:
        prev = old.get(g.id, {"B": "Unaddress", "note": ""})
        if g.kind == "llm-judge":
            e, note = "CI-only", ""
        else:
            r = results[g.id]; e = "Satisfied" if r["pass"] else "Refuted"
            note = f"exit={r['exit']} sha={r['sha256'][:12]}" + (" flaky" if r["flaky"] else "")
        rows.append(f"| {g.id} | {e} | {prev['B']} | {note} |")
    ctx.state.write_text(f"iteration: {it}\nstall: {stall}\n\n" + "\n".join(rows) + "\n")


def ref_counter(ctx: Ctx, name: str) -> int:
    """Counter stored as a git ref (refs/nid/<name>); harder to reset by accident than a .md file."""
    r = git(ctx, "rev-parse", "--verify", "-q", f"refs/nid/{name}")
    if r.returncode != 0:
        return 0
    blob = git(ctx, "cat-file", "-p", r.stdout.strip()).stdout.strip()
    try:
        return int(blob)
    except ValueError:
        die(f"refs/nid/{name} is malformed")


def set_ref_counter(ctx: Ctx, name: str, value: int) -> None:
    h = subprocess.run(["git", "-C", str(ctx.root), "hash-object", "-w", "--stdin"], input=str(value),
                       capture_output=True, text=True).stdout.strip()
    git(ctx, "update-ref", f"refs/nid/{name}", h)


def stage_a(ctx: Ctx, gates: list[Gate], record=True) -> tuple[bool, dict, list[str]]:
    """Freeze -> run -> freeze again. Returns (pass, results, unmet)."""
    if not verify_freeze(ctx, gates, quiet=True):
        verify_freeze(ctx, gates); die("refusing: freeze mismatch before run")
    influence_check(ctx, gates)
    results = run_all(ctx, gates, record)
    if not verify_freeze(ctx, gates, quiet=True):
        verify_freeze(ctx, gates)
        die("a CHECK mutated a frozen file during the run -> reject")
    unmet = [gid for gid, r in results.items() if not r["pass"]]
    return not unmet, results, unmet


def flaky_ids(results: dict) -> list[str]:
    return [gid for gid, r in results.items() if r.get("flaky")]


def cmd_run(ctx: Ctx, hook=False) -> None:
    gates = parse_ledger(ctx)
    _, _, _, caps = parse_plan(ctx)
    old, it, stall = read_state(ctx)
    it, stall = max(it, ref_counter(ctx, "iteration")), max(stall, ref_counter(ctx, "stall"))
    ok, results, unmet = stage_a(ctx, gates)
    for gid, r in results.items():
        print(f"{gid}: {'PASS' if r['pass'] else 'FAIL'} exit={r['exit']} expect={r['expect_match']} "
              f"sha={r['sha256'][:12]} bytes={r['bytes']}{' (flaky)' if r['flaky'] else ''}")
    prev_e = {k: v["E"] for k, v in old.items() if k in results}
    new_e = {k: ("Satisfied" if r["pass"] else "Refuted") for k, r in results.items()}
    stall = 0 if ok else (stall + 1 if prev_e == new_e else 0)
    it += 1
    write_state(ctx, gates, results, it, stall, old)
    set_ref_counter(ctx, "iteration", it); set_ref_counter(ctx, "stall", stall)
    judge = [g.id for g in gates if g.kind == "llm-judge"]
    if not ok:
        print(f"UNMET: {','.join(unmet)}")
        if it >= caps["max_iterations"] or stall >= caps["stall_iters"]:
            print(f"HANDOFF REQUIRED: {','.join(unmet)} (iteration {it}/{caps['max_iterations']}, stall {stall}/{caps['stall_iters']})")
            sys.exit(2 if hook else 3)
        sys.exit(2 if hook else 1)
    print("ALL MET" + (f" (llm-judge pending CI: {','.join(judge)})" if judge else ""))
    sys.exit(0)


# --------------------------------------------------------------------------
# CI
# --------------------------------------------------------------------------
CI_KEYS = ["CI", "STAGE_A", "STAGE_B", "PROCESS", "OUTCOME", "UNMET", "EVIDENCE"]
CI_ALLOWED = {"CI": {"merge-ok", "reject", "inconclusive"}, "STAGE_A": {"pass", "fail"},
              "STAGE_B": {"pass", "fail", "skipped"}, "PROCESS": {"pass", "fail"}, "OUTCOME": {"pass", "fail"}}
PTR_RE = re.compile(r"^([GH]\d+):\s*(pass|fail)\s*(.*?)\s*$")
FILE_PTR = re.compile(r"^@\s*(\S+?)(?::(\d+)(?:-(\d+))?)?\s+sha=([0-9a-f]{12,64})$")
CMD_PTR = re.compile(r"^\$\s*(.+?)\s+sha=([0-9a-f]{12,64})$")


def parse_ci(ctx: Ctx) -> dict:
    if not ctx.ci.exists() or not ctx.ci.read_text().strip(): die("CI.md missing or empty -> reject")
    vals = {}
    for line in ctx.ci.read_text().splitlines():
        m = re.match(r"^([A-Z_]+):\s*(.*?)\s*$", line)
        if m and m.group(1) in CI_KEYS and m.group(1) not in vals: vals[m.group(1)] = m.group(2)
    for k in CI_KEYS:
        if k not in vals: die(f"CI.md missing field {k} -> reject")
    for k, allowed in CI_ALLOWED.items():
        if vals[k] not in allowed: die(f"CI.md bad value {k}={vals[k]!r} -> reject")
    return vals


def verify_pointer(ctx: Ctx, hid: str, ptr: str, subjects: list[str]) -> str | None:
    m = FILE_PTR.match(ptr)
    if m:
        pth, l1, l2, h = m.group(1), m.group(2), m.group(3), m.group(4)
        p = (ctx.root / pth)
        rp = ctx.rel(p)
        if rp.startswith("..") or p.is_absolute() and not str(p.resolve()).startswith(str(ctx.root)):
            return f"{hid}: pointer outside repo: {pth}"
        if str(p.resolve()).startswith(str(ctx.nid)): return f"{hid}: pointer inside .no-illusory-done (not an artifact)"
        if not p.is_file(): return f"{hid}: pointer file missing {pth}"
        if "*" in subjects:
            pass  # llm-judge gates: any regular file inside the repo
        elif not any(not s.startswith("$ ") and rp == s for s in subjects):
            return f"{hid}: pointer {rp} is not one of the SUBJECT files {[x for x in subjects if not x.startswith('$ ')]}"
        if not inside_repo(ctx, p): return f"{hid}: pointer resolves outside the repo"
        frozen = set(read_freeze(ctx)[0])
        if rp in frozen: return f"{hid}: pointer {rp} is a frozen oracle file, not an artifact of the change"
        fcommit = git(ctx, "log", "-1", "--format=%H", "--", ctx.rel(ctx.freeze)).stdout.strip()
        if rp not in changed_files(ctx, fcommit):
            return f"{hid}: pointer {rp} is unchanged since the freeze; it cannot evidence this change"
        if l1:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            a, b = int(l1), int(l2 or l1)
            if a < 1 or b > len(lines) or a > b: return f"{hid}: line range out of bounds for {rp}"
            actual = sha("\n".join(lines[a - 1:b]).encode())
        else:
            actual = sha_file(p)
        return None if actual.startswith(h) else f"{hid}: hash mismatch for {rp} (grader did not read this version)"
    m = CMD_PTR.match(ptr)
    if m:
        cmd, h = m.group(1), m.group(2)
        if "*" not in subjects and not any(s.startswith("$ ") and cmd.strip() == s[2:].strip() for s in subjects):
            return f"{hid}: pointer command must equal a '$ ' SUBJECT command exactly"
        for rx, name in BAD_CHECK:
            if rx.search(cmd): return f"{hid}: pointer command forbidden ({name})"
        try:
            r = subprocess.run(["bash", "-o", "pipefail", "-c", cmd], cwd=str(ctx.root), timeout=300,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean_env())
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"{hid}: pointer command failed to run ({e})"
        out = r.stdout.decode("utf-8", "replace")
        if r.returncode != 0: return f"{hid}: pointer command exit {r.returncode}"
        if len(out.strip()) == 0: return f"{hid}: pointer command produced no output"
        return None if sha(out.encode()).startswith(h) else f"{hid}: command output hash mismatch (grader did not run this)"
    return f"{hid}: pass without a verifiable pointer (@ path sha=.. | $ cmd sha=..)"


def check_ci_pointers(ctx: Ctx, highs: dict, judge_ids: list[str]):
    verdicts, problems = {}, []
    for line in ctx.ci.read_text(encoding="utf-8").splitlines():
        m = PTR_RE.match(line.strip())
        if not m: continue
        hid, res, ptr = m.groups()
        if hid in verdicts: problems.append(f"{hid}: duplicate verdict line"); continue
        verdicts[hid] = res
        if res == "pass":
            subjects = highs[hid]["SUBJECTS"] if hid in highs else []
            if hid not in highs and hid not in judge_ids:
                problems.append(f"{hid}: verdict for unknown criterion"); continue
            if hid in judge_ids:
                subjects = ["*"]  # llm-judge gates: any regular in-repo file / any non-forbidden command
            why = verify_pointer(ctx, hid, ptr, subjects)
            if why: problems.append(why); verdicts[hid] = "fail"
    for hid in list(highs) + judge_ids:
        if hid not in verdicts: problems.append(f"{hid}: no verdict line in CI.md")
    return verdicts, problems


def ci_verdict(ctx: Ctx) -> tuple[str, list[str]]:
    """Re-run Stage A, then validate CI.md. Returns (effective verdict, problems)."""
    gates = parse_ledger(ctx)
    _, highs, _, _ = parse_plan(ctx)
    v = parse_ci(ctx)
    fz = verify_freeze(ctx, gates, quiet=True)
    a_ok, results, unmet = stage_a(ctx, gates, record=False)
    judge = [g.id for g in gates if g.kind == "llm-judge"]
    verdicts, problems = check_ci_pointers(ctx, highs, judge)
    if flaky_ids(results): problems.append(f"flaky gates passed only on retry: {','.join(flaky_ids(results))} (process fail)")
    mut = mutation_verdict(ctx, gates)
    if mut["status"] == "fail": problems.append(f"VACUOUS ORACLE: {len(mut['survivors'])} mutants survived: " + "; ".join(mut["survivors"][:5]))
    failed = [k for k in list(highs) + judge if verdicts.get(k) != "pass"]
    if v["CI"] != "merge-ok":
        return v["CI"], [f"mutation: {mut['status']}"] if mut["status"] != "pass" else []
    if not fz: problems.append("freeze mismatch")
    if not a_ok: problems.append(f"Stage A fails on THIS run: {','.join(unmet)}")
    if v["STAGE_A"] != "pass": problems.append("STAGE_A field is not pass")
    if v["PROCESS"] != "pass" or v["OUTCOME"] != "pass": problems.append("process/outcome not pass")
    if v["STAGE_B"] == "fail": problems.append("Stage B fail")
    if v["STAGE_B"] == "skipped" and (highs or judge): problems.append("Stage B skipped but H/llm-judge criteria exist")
    if v["UNMET"].strip().lower() not in ("none", "", "-"): problems.append(f"UNMET non-empty: {v['UNMET']}")
    if failed: problems.append(f"criteria without verified pass: {','.join(failed)}")
    return ("reject" if problems else "merge-ok"), problems


def cmd_ci(ctx: Ctx) -> None:
    verdict, problems = ci_verdict(ctx)
    for p in problems: print(f"CI PROBLEM: {p}")
    print(f"CI: {verdict}")
    sys.exit(0 if verdict == "merge-ok" else 1)


# --------------------------------------------------------------------------
# Mutation (python only, v1)
# --------------------------------------------------------------------------
class Mutator(ast.NodeTransformer):
    SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
            ast.Add: ast.Sub, ast.Sub: ast.Add, ast.And: ast.Or, ast.Or: ast.And}

    def __init__(self, target: int):
        self.target, self.count, self.desc = target, 0, None

    def hit(self, d):
        self.count += 1
        if self.count == self.target:
            self.desc = d; return True
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            if type(op) in self.SWAP and self.hit(f"L{node.lineno}: {type(op).__name__}->{self.SWAP[type(op)].__name__}"):
                node.ops[i] = self.SWAP[type(op)](); break
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if type(node.op) in self.SWAP and self.hit(f"L{node.lineno}: {type(node.op).__name__} swapped"):
            node.op = self.SWAP[type(node.op)]()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if type(node.op) in self.SWAP and self.hit(f"L{node.lineno}: {type(node.op).__name__} swapped"):
            node.op = self.SWAP[type(node.op)]()
        return node

    def visit_If(self, node):
        self.generic_visit(node)
        if self.hit(f"L{node.lineno}: if-condition negated"):
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, float) and self.hit(f"L{node.lineno}: {node.value}->{node.value * 1.5 + 1}"):
            return ast.copy_location(ast.Constant(node.value * 1.5 + 1), node)
        if isinstance(node.value, bool) and self.hit(f"L{node.lineno}: {node.value}->{not node.value}"):
            return ast.copy_location(ast.Constant(not node.value), node)
        if isinstance(node.value, int) and not isinstance(node.value, bool) and self.hit(f"L{node.lineno}: {node.value}->{node.value + 1}"):
            return ast.copy_location(ast.Constant(node.value + 1), node)
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        if node.value is not None and self.hit(f"L{node.lineno}: return -> return None"):
            return ast.copy_location(ast.Return(value=None), node)
        return node


def changed_files(ctx: Ctx, since: str) -> list[str]:
    """Committed-since-freeze + uncommitted + untracked."""
    out = git(ctx, "diff", "--name-only", since, "HEAD", "--").stdout.split()
    out += git(ctx, "diff", "--name-only", "HEAD", "--").stdout.split()
    out += git(ctx, "ls-files", "--others", "--exclude-standard").stdout.split()
    return sorted(set(out))


def count_mutants(src: str) -> int:
    m = Mutator(10**9); m.visit(ast.parse(src)); return m.count


def mutation_verdict(ctx: Ctx, gates: list[Gate], max_per_file=20, verbose=False) -> dict:
    """Returns {"status": pass|fail|inconclusive, "survivors": [...], "total": n, "note": str}."""
    frozen, _, _ = read_freeze(ctx)
    fcommit = git(ctx, "log", "-1", "--format=%H", "--", ctx.rel(ctx.freeze)).stdout.strip()
    changed = [f for f in changed_files(ctx, fcommit) if f.endswith(".py") and f not in frozen and not f.startswith("scripts/nid_check") and (ctx.root / f).is_file()]
    changed = sorted(set(changed))
    if not changed:
        return {"status": "inconclusive", "survivors": [], "total": 0, "note": "no changed .py source since freeze (mutation v1: python only)"}
    survivors, total = [], 0
    for f in changed:
        src = (ctx.root / f).read_text(encoding="utf-8")
        n = min(count_mutants(src), max_per_file)
        if n == 0: continue
        if count_mutants(src) > max_per_file: print(f"NOTE: {f}: {count_mutants(src)} mutants, testing first {max_per_file}")
        for i in range(1, n + 1):
            total += 1
            m = Mutator(i); tree = m.visit(ast.parse(src)); ast.fix_missing_locations(tree)
            git(ctx, "worktree", "prune")
            with tempfile.TemporaryDirectory() as td:
                wt = Path(td) / "wt"
                if git(ctx, "worktree", "add", "--detach", str(wt), "HEAD").returncode != 0: die("git worktree add failed")
                try:
                    # carry uncommitted working tree changes, then apply mutant
                    for cf in changed_files(ctx, "HEAD"):
                        s, d = ctx.root / cf, wt / cf
                        if s.is_file(): d.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(s, d)
                    (wt / f).write_text(ast.unparse(tree), encoding="utf-8")
                    sub = Ctx(wt / ".no-illusory-done" / "LEDGER.md")
                    rs = run_all(sub, gates, record=True)
                    failed = [gid for gid, r in rs.items() if not r["pass"]]
                    # A kill counts only if some gate failed WITHOUT an import/syntax crash: a mutant that
                    # merely breaks importing proves the test imported the module, not that it tested it.
                    real = [gid for gid in failed if not CRASH_KILL.search((sub.evidence / f"{gid}.out").read_text(errors="replace") if (sub.evidence / f"{gid}.out").exists() else "")]
                    killed = bool(real)
                    crash_only = bool(failed) and not real
                finally:
                    os.chdir(ctx.root)
                    git(ctx, "worktree", "remove", "--force", str(wt))
            if verbose: print(f"{f} #{i} {m.desc}: {'killed' if killed else ('SURVIVED (crash-only: import/syntax error, not an assertion)' if crash_only else 'SURVIVED')}")
            if not killed: survivors.append(f"{f}#{i} {m.desc}" + (" [crash-only]" if crash_only else ""))
    if total == 0:
        return {"status": "inconclusive", "survivors": [], "total": 0, "note": "changed python has no mutable nodes"}
    return {"status": "fail" if survivors else "pass", "survivors": survivors, "total": total, "note": ""}


def cmd_mutate(ctx: Ctx) -> None:
    gates = parse_ledger(ctx)
    if not verify_freeze(ctx, gates, quiet=True):
        verify_freeze(ctx, gates); die("refusing --mutate: freeze mismatch")
    a_ok, _, unmet = stage_a(ctx, gates, record=False)
    if not a_ok: die(f"baseline not ALL MET ({','.join(unmet)}); mutation is meaningless")
    mv = mutation_verdict(ctx, gates, verbose=True)
    print(f"MUTANTS: {mv['total']} killed: {mv['total'] - len(mv['survivors'])} survived: {len(mv['survivors'])}")
    if mv["status"] == "inconclusive":
        print(f"MUTATION: inconclusive — {mv['note']}"); sys.exit(1)
    if mv["survivors"]:
        print("VACUOUS ORACLE: gates did not detect these near-miss implementations:")
        for x in mv["survivors"]: print("  " + x)
        sys.exit(1)
    print("MUTATION: pass"); sys.exit(0)


# --------------------------------------------------------------------------
def cmd_report(ctx: Ctx) -> None:
    gates = parse_ledger(ctx)
    fz = verify_freeze(ctx, gates, quiet=True)
    a_ok, results, unmet = stage_a(ctx, gates, record=False) if fz else (False, {}, ["<freeze mismatch>"])
    ci, problems = ("not-run", [])
    if ctx.ci.exists() and ctx.ci.read_text().strip():
        try:
            ci, problems = ci_verdict(ctx)
        except SystemExit:
            ci = "reject(parse-fail)"
    verdict = "merge-ok" if (ci == "merge-ok" and a_ok and fz and not flaky_ids(results)) else \
              "reject" if ci.startswith("reject") else "inconclusive" if ci == "inconclusive" else "not-verified"
    _, it, _ = read_state(ctx)
    print(f"VERDICT: {verdict}")
    print(f"STAGE_A: {'pass' if a_ok else 'fail'}  (re-run in this invocation)")
    print(f"CI: {ci}")
    print(f"UNMET: {','.join(unmet) if unmet else 'none'}")
    print(f"FREEZE: {'match' if fz else 'mismatch'}")
    print(f"FLAKY: {','.join(flaky_ids(results)) or 'none'}")
    mv = mutation_verdict(ctx, gates) if (fz and a_ok) else {"status": "not-run", "survivors": [], "note": ""}
    print(f"MUTATION: {mv['status']}" + (f" ({len(mv['survivors'])} survived)" if mv['survivors'] else "") + (f" — {mv['note']}" if mv.get('note') else ""))
    print(f"ITER: {it}")
    print("EVIDENCE: " + ("; ".join(f"{k} → exit {r['exit']}, expect {r['expect_match']}, {r['sha256'][:12]}" for k, r in results.items()) or "none"))
    for p in problems: print(f"CI PROBLEM: {p}")
    print(f"CI.md: {ctx.rel(ctx.ci) if ci != 'not-run' else 'missing'}")
    sys.exit(0 if verdict == "merge-ok" else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    for name in ("status", "red", "run", "mutate"): g.add_argument(f"--{name}", metavar="LEDGER")
    g.add_argument("--ci", metavar="CI_MD")
    g.add_argument("--verify-freeze", action="store_true")
    g.add_argument("--report", action="store_true")
    g.add_argument("--hook", action="store_true")
    ap.add_argument("--supersede", metavar="REASON", help="with --red: declare a re-freeze (recorded in FREEZE)")
    a = ap.parse_args()
    try:
        if a.status:
            ctx = Ctx(Path(a.status)); gates = parse_ledger(ctx)
            print(f"OK: {len(gates)} gates ({sum(g.kind == 'cmd' for g in gates)} runnable)")
        elif a.red: cmd_red(Ctx(Path(a.red)), a.supersede)
        elif a.run: cmd_run(Ctx(Path(a.run)))
        elif a.mutate: cmd_mutate(Ctx(Path(a.mutate)))
        elif a.ci:
            p = Path(a.ci).resolve(); cmd_ci(Ctx(p.parent / "LEDGER.md"))
        elif a.verify_freeze:
            ctx = Ctx(None); sys.exit(0 if verify_freeze(ctx, parse_ledger(ctx)) else 1)
        elif a.report: cmd_report(Ctx(None))
        elif a.hook:
            here = Path.cwd().resolve()
            if not any((d / ".no-illusory-done" / "LEDGER.md").exists() for d in (here, *here.parents)):
                sys.exit(0)
            cmd_run(Ctx(None), hook=True)
    except SystemExit:
        raise
    except Exception as e:  # any crash is a failed verdict, never a pass
        die(f"checker crashed ({type(e).__name__}: {e}) -> fail closed", 2)


if __name__ == "__main__":
    main()
