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
  --hook               Stop-hook entry: no ledger or no freeze yet -> exit 0; else --run, exit 2 on unmet/handoff

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
CURRENT_ROOT: Path | None = None
GATE_RE = re.compile(r"^- \[( |x|X)\] (G\d+):\s*(.+?)\s*$")
FIELD_RE = re.compile(r"^\s*([A-Z]+):\s*(.*?)\s*$")
FIELDS = {"CHECK", "EXPECT", "CWD", "TIMEOUT", "FILES", "KIND", "RED", "EVIDENCE", "COVERS", "ENV", "RETRIES"}
# Files that silently change how test runners / interpreters behave. If one is added or
# modified after the freeze and is not itself frozen, the oracle is no longer the oracle.
INFLUENCE = re.compile(r"(^|/)(conftest\.py|sitecustomize\.py|usercustomize\.py|[^/]*\.pth|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|"
                       r"\.env[^/]*|package\.json|jest\.config\.[^/]+|vitest\.config\.[^/]+|babel\.config\.[^/]+|\.babelrc|tsconfig[^/]*\.json|"
                       r"\.npmrc|\.mocharc[^/]*|Makefile|\.bashrc|\.zshrc|\.profile|__init__\.py)$")
CLEAN_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "SHELL")
MAX_OUTPUT = 8 * 1024 * 1024  # bytes of CHECK output; more is a failed gate, not a parse problem
# Generated dependency trees: never the product, never an oracle input; excluded from the influence scan when gitignored.
DEP_DIRS = re.compile(r"(^|/)(node_modules|\.venv|venv|\.tox|\.nox|vendor|target|build|dist|__pycache__|\.cache|\.pytest_cache|\.mypy_cache|site-packages)/")
TOOLCHAIN_KEYS = ("VIRTUAL_ENV", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "JAVA_HOME", "NVM_DIR", "PYENV_ROOT", "npm_config_cache")
# SUBJECT commands whose output does not depend on the change under review.
CONSTANT_CMD = re.compile(r"^(git\s+(log|rev-parse|rev-list|describe|branch|remote|config|status|tag|show-ref|symbolic-ref)|date|whoami|pwd|uname|hostname|id|ls|wc|cat|head|tail|stat|md5|shasum|sha256sum|python[3]?\s+--version|node\s+-v)\b")
R_RE = re.compile(r"^(R\d+):\s*(.+?)\s*$")
H_RE = re.compile(r"^(H\d+):\s*(.+?)\s*$")
CAP_RE = re.compile(r"^(max_iterations|stall_iters|max_ci_attempts|max_supersedes|max_gates_per_r|max_mutants_per_file|mutation_required|regression_only):\s*(\d+)\s*$")
MODE_RE = re.compile(r"^(strictness|witness):\s*(\w+)\s*$")
VAGUE = re.compile(r"looks good|covers the feature|works correctly|as expected|properly|correctly", re.I)
# Tokens in a CHECK that look like paths.
PATHISH = re.compile(r"(?<![\w-])((?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)*\.[A-Za-z0-9]{1,8}|(?:\./|\.\./)[\w./-]+)")
# Always enforced: these observe nothing or soften failure regardless of mode.
BAD_CHECK_ALWAYS = [
    (re.compile(r"\bexit\s+0\b"), "exit 0"),
    (re.compile(r"passWithNoTests|--no-verify"), "skip/soften flag"),
    (re.compile(r"(^|\s)(touch|cp|mv|rm|tee|sed\s+-i)\s|>"), "mutating command or redirection in CHECK"),
]
# Enforced only under strictness: strict — closes the shell-syntax bypass classes at the cost of false refusals.
BAD_CHECK = [
    (re.compile(r"(^|[;&|(]\s*)(echo|printf|true|false|command|eval|exec|source|env|xargs|nohup|nice|time|builtin)\b"), "shell no-op/indirection"),
    (re.compile(r"(^|[;&|(]\s*):(\s|$)"), ":"),
    (re.compile(r"\b(sh|bash|zsh|dash)\s+-c\b"), "nested shell"),
    (re.compile(r"\|\|"), "'||' fallback (a gate must be conjunctive)"),
    (re.compile(r"python[3]?\s+-c\b"), "python -c"),
    (re.compile(r"<"), "input redirection"),
    (re.compile(r"[$`]|<<|(^|[;&|(]\s*)\w+=\S"), "shell expansion/heredoc/assignment (use a repo-owned script)"),
    (re.compile(r"(^|[;&|(\s])(if|then|else|elif|fi|while|until|for|do|done|case|esac|function|select|!|\{|\}|\[\[|\[)(\s|$|;)"), "shell control flow (a gate is a straight && chain)"),
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
        if self.nid.name != ".no-illusory-done" or self.ledger.name != "LEDGER.md":
            die(f"ledger must be <repo>/.no-illusory-done/LEDGER.md (got {self.ledger})")
        top = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             env={**GIT_ENV, "PATH": safe_path(self.root)}).stdout.strip()
        if not top or Path(top).resolve() != self.root:
            die(f".no-illusory-done must sit at the git toplevel ({top or 'not a git repo'}), not {self.root}")
        self.plan = self.nid / "PLAN.md"
        self.freeze = self.nid / "FREEZE.sha256"
        self.state = self.nid / "STATE.md"
        self.evidence = self.nid / "evidence"
        self.ci = self.nid / "CI.md"
        os.chdir(self.root)
        global CURRENT_ROOT
        CURRENT_ROOT = self.root

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
    sanity_text("PLAN.md", ctx.plan.read_text(encoding="utf-8"))
    reqs, highs, setup = {}, {}, []
    caps = {"max_iterations": 8, "stall_iters": 3, "max_ci_attempts": 3, "max_supersedes": 3, "max_gates_per_r": 4,
            "max_mutants_per_file": 0, "mutation_required": 0, "regression_only": 0,
            "strictness": "lite", "witness": "remote", "mutate_cmd": "", "mutate_expect": ""}
    expected_new: list[str] = []
    product: list[str] = []
    explicit: set[str] = set()
    for ln, raw in enumerate(ctx.plan.read_text(encoding="utf-8").replace("\r", "").splitlines(), 1):
        line = raw.strip().lstrip("-* ").strip()
        m = CAP_RE.match(line)
        if m:
            caps[m.group(1)] = int(m.group(2)); explicit.add(m.group(1)); continue
        m = MODE_RE.match(line)
        if m:
            k, v = m.group(1), m.group(2)
            if k == "strictness" and v not in ("lite", "strict"): die(f"PLAN.md strictness must be lite|strict (got {v})")
            if k == "witness" and v not in ("remote", "local"): die(f"PLAN.md witness must be remote|local (got {v})")
            caps[k] = v; continue
        if line.startswith("MUTATE:"):
            caps["mutate_cmd"] = line[7:].strip(); continue
        if line.startswith("MUTATE_EXPECT:"):
            caps["mutate_expect"] = line[14:].strip(); continue
        if line.startswith("SETUP:"):
            setup.append(line[6:].strip()); continue
        if line.startswith("PRODUCT:"):
            for x in line[8:].split(","):
                x = x.strip().rstrip("/")
                if not x: continue
                if x in (".", "") or x.startswith(".no-illusory-done") or x.startswith("scripts/nid_check"):
                    die(f"PLAN.md PRODUCT may not be the repo root or the checker/ledger dir: {x}")
                product.append(x)
            continue
        if line.startswith("EXPECTED_NEW:"):
            for x in line[13:].split(","):
                x = x.strip()
                if x:
                    expected_new.append(x)
            continue
        m = R_RE.match(line)
        if m:
            if m.group(1) in reqs: die(f"PLAN.md duplicate {m.group(1)} (line {ln})")
            if len(m.group(2)) < 4 or VAGUE.search(m.group(2)):
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
            if len(kv["STATE"]) < 4 or len(kv["FALSIFIER"]) < 4:
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
                if in_nid(ctx, sp):
                    die(f"PLAN.md {hid}: SUBJECT may not be inside .no-illusory-done")
                if sp.suffix.lower() in (".md", ".txt", ".rst", ".adoc", ".html", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".csv", ".xml"):
                    die(f"PLAN.md {hid}: SUBJECT {s} is a prose/data file the implementer can rewrite to say anything; cite code, or a '$ command' that observes behaviour")
            kv["SUBJECTS"] = subjects
            highs[hid] = kv
    if not reqs:
        die("PLAN.md has no R1.. requirement clauses")
    caps["_expected_new"] = expected_new
    caps["_product"] = product
    if caps["strictness"] == "strict" and caps["mutation_required"] == 0 and not caps["mutate_cmd"] and "mutation_required" not in explicit:
        caps["mutation_required"] = 1  # strict mode requires mutation unless explicitly waived
    if bool(caps["mutate_cmd"]) != bool(caps["mutate_expect"]):
        die("PLAN.md MUTATE: and MUTATE_EXPECT: must be given together")
    if not product:
        die("PLAN.md must declare PRODUCT: <paths the implementation may change> (frozen)")
    return reqs, highs, setup, caps


LOOKALIKE_ID = re.compile(r"^\s*(?:- \[.\]\s*)?[RHGＲＨＧ][^\s:]{0,4}[:：]")


def sanity_text(name: str, text: str) -> None:
    """Refuse invisible / lookalike characters in structural files."""
    import unicodedata
    for ln, line in enumerate(text.splitlines(), 1):
        for ch in line:
            cat = unicodedata.category(ch)
            if cat in ("Cf", "Co", "Cn") or "\u2000" <= ch <= "\u200f" or "\u2028" <= ch <= "\u202f" or ch in "\ufeff\u2060":
                die(f"{name} line {ln}: invisible/format character U+{ord(ch):04X} not allowed")
        head = re.split(r"[:：]", line, 1)[0]
        for ch in head:
            if unicodedata.east_asian_width(ch) == "F" or "\uff00" <= ch <= "\uffef":
                die(f"{name} line {ln}: fullwidth character {ch!r} in an id/field position (lookalike id)")
        fm = FIELD_RE.match(line)
        if fm and fm.group(1) in FIELDS:
            continue
        if re.match(r"^\s*-\s*\[.?\]", line) and not GATE_RE.match(line):
            die(f"{name} line {ln}: looks like a gate but is not exactly '- [ ] Gn: title': {line.strip()!r}")
        if LOOKALIKE_ID.match(line) and not (R_RE.match(line.strip().lstrip("-* ").strip()) or H_RE.match(line.strip().lstrip("-* ").strip()) or GATE_RE.match(line)):
            die(f"{name} line {ln}: looks like an id but is not a valid R/H/G line: {line.strip()!r}")


def in_nid(ctx: Ctx, p: Path) -> bool:
    r = str(p.resolve())
    return r == str(ctx.nid) or r.startswith(str(ctx.nid) + os.sep)


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
    return bool(shutil.which(first))


def parse_ledger(ctx: Ctx) -> list[Gate]:
    text = ctx.ledger.read_text(encoding="utf-8").replace("\r", "") if ctx.ledger.exists() else ""
    if not text.strip(): die("ledger missing or empty")
    sanity_text("LEDGER.md", text)
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
        caps_ = parse_plan(ctx)[3]
        rules = BAD_CHECK_ALWAYS + (BAD_CHECK if caps_["strictness"] == "strict" else [])
        for rx, name in rules:
            if rx.search(chk): die(f"{g.id}: forbidden CHECK pattern ({name})" + ("" if caps_["strictness"] == "strict" else ""))
        lit = exp[1:-1] if is_regex(exp) else exp
        if lit and lit in chk: die(f"{g.id}: CHECK contains EXPECT text (self-fulfilling)")
        if is_regex(exp):
            try:
                rx = re.compile(lit)
            except re.error as e:
                die(f"{g.id}: EXPECT regex invalid: {e}")
            for probe in ("", "FAIL", "error", "x", "NOT THE REQUEST", "Traceback", "Error: something went wrong here", "0", "false"):
                if rx.fullmatch(probe): die(f"{g.id}: EXPECT regex is vacuous (matches {probe!r})")
            literal = re.sub(r"\\.|[\^$.*+?()\[\]{}|]|\d+,?\d*", "", lit)
            if len(re.sub(r"[^A-Za-z0-9_-]", "", literal)) < 3:
                die(f"{g.id}: EXPECT regex has no meaningful literal (needs ≥3 literal alphanumerics, e.g. /NID G\\d+/)")
        if len(lit.strip()) < 3: die(f"{g.id}: EXPECT too short to be a success marker")
        for kv in g.env:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*=[^\s$`]*", kv) or kv.split("=")[0] in ("PATH", "PYTHONPATH", "NODE_PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONSTARTUP", "BASH_ENV", "ENV"):
                die(f"{g.id}: ENV entry not allowed: {kv!r} (literal KEY=value only; no PATH/PYTHONPATH/NODE_PATH/LD_PRELOAD)")
        if g.f.get("RED", "required") not in ("required", "pass-ok"): die(f"{g.id}: bad RED value")
        if "RETRIES" in g.f: die(f"{g.id}: RETRIES is not supported — a flaky oracle is not an oracle; fix the test")
        try:
            t = int(g.f.get("TIMEOUT", "300"))
        except ValueError:
            die(f"{g.id}: TIMEOUT must be an integer")
        if t <= 0 or t > 3600: die(f"{g.id}: TIMEOUT must be 1..3600 seconds")
        cwd = ctx.root / g.f.get("CWD", ".")
        if not cwd.is_dir() or ctx.rel(cwd).startswith(".."): die(f"{g.id}: CWD not a dir inside repo")
        if not g.files: die(f"{g.id}: FILES is empty — a runnable gate must depend on at least one frozen oracle file")
        # Every existing file the CHECK names must be frozen (FILES). A path that does not
        # exist yet is product output the implementation will create.
        declared = set(g.files)
        if re.search(r"[*?\[\]~]", chk): die(f"{g.id}: globs/tilde in CHECK are not allowed (name files explicitly)")
        import shlex
        try:
            lex = shlex.shlex(chk, posix=True, punctuation_chars=True); lex.whitespace_split = True
            words = [w for w in lex if w not in ("(", ")", "<", ">", "&")]
        except ValueError as e:
            die(f"{g.id}: CHECK is not parseable as shell words ({e})")
        toks = set(w for w in words if w not in ("&&", "||", "|", ";"))
        prod = parse_plan(ctx)[3]["_product"]
        # The oracle may READ the product but may not EXECUTE it: an executed product file is an oracle the
        # implementer writes. Refuse a command word, an interpreter target, or a `-m module` under PRODUCT.
        INTERP = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "deno", "bun", "php", "tsx", "ts-node", "npx", "go", "java", "dotnet", "cargo"}
        # Segment rule: inside a segment run by an interpreter/runner, NO argument may be a PRODUCT path (covers -r/-S/-- preloads).
        seg, segs = [], []
        for w in words:
            if w in ("&&", ";", "|", "||"): segs.append(seg); seg = []
            else: seg.append(w)
        segs.append(seg)
        for sg in segs:
            if sg and sg[0] in INTERP:
                for w in sg[1:]:
                    wp = cwd / w.lstrip("./") if not os.path.isabs(w) else Path(w)
                    if w and not w.startswith("-") and wp.exists() and under(ctx.rel(wp), prod):
                        die(f"{g.id}: CHECK passes product path {ctx.rel(wp)} to {sg[0]} — the oracle must be a frozen file, never the product (read it with grep/diff, do not load it)")
        for i, w in enumerate(words):
            cand = None
            if i == 0 or (i > 0 and words[i - 1] in INTERP) or (i > 0 and words[i - 1] == "run" and i > 1 and words[i - 2] == "go"):
                cand = w
            elif i > 1 and words[i - 1] == "-m" and words[i - 2] in INTERP:
                cand = w.replace(".", "/")
            if cand:
                for c in (cand, cand + ".py", cand + "/__main__.py", cand + ".js"):
                    cp = cwd / c
                    if cp.exists() and under(ctx.rel(cp), prod):
                        die(f"{g.id}: CHECK executes product path {ctx.rel(cp)} — the oracle must be a frozen file, never the product")
        for tok in toks:
            if not tok or tok == "--": continue
            p = cwd / tok
            if p.is_dir() and tok not in (".", "./") and not in_nid(ctx, p):
                reld = ctx.rel(p)
                if not under(reld, prod):
                    inside = [ctx.rel(x) for x in p.rglob("*") if x.is_file() and not DEP_DIRS.search(ctx.rel(x))]
                    if any(x not in declared for x in inside):
                        die(f"{g.id}: CHECK names directory {reld} outside PRODUCT; every file under it must be in FILES (or name files explicitly)")
            if p.is_file() and not in_nid(ctx, p):
                relp = ctx.rel(p)
                if under(relp, prod): continue  # product files may be read (never executed, checked above)
                if relp not in declared and tok not in declared:
                    die(f"{g.id}: CHECK references existing file {relp} not in FILES (existing inputs must be frozen; delete it before --red if the implementation must regenerate it)")
        for f in g.files:
            fp = ctx.root / f
            if not fp.is_file() or not inside_repo(ctx, fp): die(f"{g.id}: FILES entry missing, not a regular file, or symlinks outside the repo: {f}")
    checks = [g.f.get("CHECK", "").strip() for g in gates if g.kind == "cmd"]
    if len(checks) != len(set(checks)): die("two gates have identical CHECK commands (duplicate observation)")
    if all(g.kind == "llm-judge" for g in gates):
        die("all gates are llm-judge; at least one runnable gate required")
    if not any(g.kind == "cmd" and g.f.get("RED", "required") == "required" for g in gates) and not parse_plan(ctx)[3]["regression_only"]:
        die("at least one gate must be RED: required (otherwise nothing proves new behavior); set regression_only: 1 in PLAN.md for a regression-only ledger")
    reqs, highs, _, _ = parse_plan(ctx)
    covered = set()
    for g in gates:
        for r in g.covers:
            if r not in reqs: die(f"{g.id}: COVERS unknown requirement {r}")
        if g.kind == "cmd": covered.update(g.covers)
    n_cmd, n_judge = sum(g.kind == "cmd" for g in gates), sum(g.kind == "llm-judge" for g in gates)
    if n_judge > n_cmd: die(f"{n_judge} llm-judge gates vs {n_cmd} runnable gates: judgment may not outnumber observation")
    missing = sorted(set(reqs) - covered, key=lambda x: int(x[1:]))
    if missing: die(f"requirements with no RUNNABLE gate (llm-judge coverage does not count): {','.join(missing)}")
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
    timeout, retries = int(g.f.get("TIMEOUT", "300")), 0
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
                if len(raw) > MAX_OUTPUT:
                    out, code = out[-4096:] + f"\n[NID OUTPUT TOO LARGE: {len(raw)} bytes > {MAX_OUTPUT}]", -1
            except subprocess.TimeoutExpired:
                kill_group(proc)
                try:
                    raw, _ = proc.communicate(timeout=5)
                    note = "[NID TIMEOUT: process group killed]"
                except subprocess.TimeoutExpired:
                    # a detached descendant still holds the pipe: stop reading, do not wait for it
                    try: proc.stdout.close()
                    except OSError: pass
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: pass
                    raw, note = b"", "[NID TIMEOUT: process group killed; a detached descendant kept the pipe open — output discarded]"
                out, code, to = raw.decode("utf-8", "replace") + "\n" + note, -1, True
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


# A mutant counts as killed only when a TEST ASSERTION failed. Any other failure (import error, IndexError, missing
# file, crash) proves the module was loaded, not that its behaviour was tested. Whitelist, not blacklist.
REAL_KILL = re.compile(r"(AssertionError|assert\b|FAILED|FAIL:|Failures?:|failures?=|expected|Expected|not ok|✗|✘|AssertionFailed|ExpectationFailed|mismatch)")


def clean_env(g: Gate | None = None) -> dict:
    """Nothing inherited from the caller's shell except a whitelist; gate ENV: values are frozen literals."""
    env = {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}
    # PATH: absolute, outside the repo, no empty/'.' entries — an implementer-added bin/ must never shadow a tool.
    safe = []
    for d in env.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        if not d or not os.path.isabs(d): continue
        rd = os.path.realpath(d)
        if CURRENT_ROOT is not None and (rd == str(CURRENT_ROOT) or rd.startswith(str(CURRENT_ROOT) + os.sep)): continue
        safe.append(d)
    env["PATH"] = os.pathsep.join(safe) or "/usr/bin:/bin"
    for k in TOOLCHAIN_KEYS:  # toolchain homes are allowed only when they live outside the repo
        v = os.environ.get(k)
        if v and os.path.isabs(v) and not (CURRENT_ROOT and (os.path.realpath(v) == str(CURRENT_ROOT) or os.path.realpath(v).startswith(str(CURRENT_ROOT) + os.sep))):
            env[k] = v
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "NPM_CONFIG_USERCONFIG": "/dev/null",
                "PIP_CONFIG_FILE": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                "NODE_OPTIONS": "", "CI": "1", "NID": "1"})
    if g is not None:
        for kv in g.env:
            k, _, v = kv.partition("="); env[k] = v
    return env


def under(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def influence_check(ctx: Ctx, gates: list[Gate]) -> None:
    """The implementer may change only PRODUCT paths (+EXPECTED_NEW). Anything else changed since the freeze
    — a loader hook, a symlink, a bin/, a runner config — means the oracle is no longer the oracle."""
    frozen = set(read_freeze(ctx)[0])
    caps = parse_plan(ctx)[3]
    expected, product = set(caps["_expected_new"]), caps["_product"]
    fcommit = git(ctx, "log", "-1", "--format=%H", "--", ctx.rel(ctx.freeze)).stdout.strip()
    if git(ctx, "ls-files", "--stage").stdout.count("160000 ") or any(x.endswith("/") for x in git(ctx, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "--directory", "-z").stdout.split("\0") if x and (ctx.root / x / ".git").exists()):
        die("submodules / nested git repositories are not supported (changes inside them are invisible to the freeze)")
    changed = changed_files(ctx, fcommit)
    out_of_scope = [f for f in changed if not under(f, product) and f not in expected and not f.startswith(".no-illusory-done/")]
    if out_of_scope:
        die(f"files changed since the freeze outside PRODUCT {product}: {', '.join(out_of_scope[:10])} -> only the product may change (HANDOFF if the plan is wrong)")
    for f in changed:
        p = ctx.root / f
        if p.is_symlink() and not inside_repo(ctx, p):
            die(f"symlink {f} added since the freeze points outside the repo")
    visible = set(changed)
    bad = [f for f in changed_files(ctx, fcommit, include_ignored=True)
           if INFLUENCE.search(f) and f not in frozen and f not in expected and not (f not in visible and DEP_DIRS.search(f))]
    if bad:
        die(f"runner-influencing files changed since the freeze and are not frozen or EXPECTED_NEW: {', '.join(bad)}")


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
        elif re.fullmatch(r"[0-9a-f]{64}  .+", line):
            h, path = line[:64], line[66:]
            if path in files: die(f"FREEZE line {ln}: duplicate file {path}")
            files[path] = h
        else:
            die(f"FREEZE line {ln}: malformed: {line!r}")
    if not files: die("FREEZE.sha256 has no file hashes")
    return files, reds, sup


GIT_ENV = {**{k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ},
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}


def safe_path(root: Path | None) -> str:
    out = []
    for d in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        if not d or not os.path.isabs(d): continue
        rd = os.path.realpath(d)
        if root is not None and (rd == str(root) or rd.startswith(str(root) + os.sep)): continue
        out.append(d)
    return os.pathsep.join(out) or "/usr/bin:/bin"


def git(ctx: Ctx, *args) -> subprocess.CompletedProcess:
    """All git reads ignore replacement objects, aliases, hooks, global/system config and inherited env; git itself is
    resolved through a PATH with relative and repo-internal entries removed."""
    env = {**GIT_ENV, "PATH": safe_path(ctx.root)}
    return subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", str(ctx.root), *args], capture_output=True, text=True, env=env)


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
        if git(ctx, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            problems.append("shallow repository: history is truncated, freeze cannot be witnessed (git fetch --unshallow)")
        gitdir = Path(git(ctx, "rev-parse", "--git-common-dir").stdout.strip())
        gitdir = gitdir if gitdir.is_absolute() else ctx.root / gitdir
        if (gitdir / "info" / "grafts").exists() or (gitdir / "shallow").exists():
            problems.append("grafted/shallow history present (.git/info/grafts or .git/shallow): freeze cannot be witnessed")
        head = git(ctx, "show", f"HEAD:{relf}")
        if head.returncode != 0:
            problems.append("FREEZE.sha256 not committed at HEAD")
        elif sha(head.stdout.encode()) != sha_file(ctx.freeze):
            problems.append("FREEZE.sha256 differs from HEAD (re-hash detected)")
        else:
            n = int(git(ctx, "rev-list", "--count", "HEAD", "--", relf).stdout.strip() or 0)
            # Remote witness: a rewritten local history cannot forge a commit that a remote already holds.
            fcommit = git(ctx, "log", "-1", "--format=%H", "--", relf).stdout.strip()
            remotes = git(ctx, "remote").stdout.split()
            if remotes and parse_plan(ctx)[3]["witness"] == "local":
                if not quiet: print("FREEZE WARNING: witness: local — remote not queried; the freeze witness is local history only")
            elif remotes:
                witnessed, unreachable = [], []
                for rname in remotes:
                    try:
                        lr = subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", str(ctx.root),
                                             "ls-remote", "--heads", "--tags", rname], capture_output=True, text=True, timeout=30,
                                            env={**GIT_ENV, "PATH": safe_path(ctx.root)})
                    except subprocess.TimeoutExpired:
                        unreachable.append(rname); continue
                    if lr.returncode != 0:
                        unreachable.append(rname); continue
                    tips = [ln.split()[0] for ln in lr.stdout.splitlines() if ln.strip()]
                    if any(git(ctx, "merge-base", "--is-ancestor", fcommit, t).returncode == 0 for t in tips):
                        witnessed.append(rname)
                if not witnessed:
                    problems.append(f"freeze commit {fcommit[:8]} is not reachable from any ref on any reachable remote "
                                    f"(reachable: {[r for r in remotes if r not in unreachable]}, unreachable: {unreachable}) — push it, or go online")
                elif unreachable and not quiet:
                    print(f"FREEZE NOTE: witnessed by {witnessed}; unreachable remotes ignored: {unreachable}")
            elif not quiet:
                print("FREEZE WARNING: no git remote — the freeze witness is local history only (rewritable)")
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
    for x in parse_plan(ctx)[3]["_expected_new"]:
        if (ctx.root / x).exists(): die(f"PLAN.md EXPECTED_NEW {x} already exists at freeze time (declare only files the implementation will create)")
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
    head_tracked = set(x for x in git(ctx, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout.split("\0") if x)
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


def ref_blob(ctx: Ctx, name: str) -> str:
    r = git(ctx, "rev-parse", "--verify", "-q", f"refs/nid/{name}")
    return git(ctx, "cat-file", "-p", r.stdout.strip()).stdout if r.returncode == 0 else ""


def set_ref_blob(ctx: Ctx, name: str, content: str) -> None:
    """Compare-and-swap so concurrent worktrees cannot lose updates."""
    old = git(ctx, "rev-parse", "--verify", "-q", f"refs/nid/{name}").stdout.strip()
    h = subprocess.run(["git", "--no-replace-objects", "-C", str(ctx.root), "hash-object", "-w", "--stdin"], input=content,
                       capture_output=True, text=True, env={**GIT_ENV, "PATH": safe_path(ctx.root)}).stdout.strip()
    args = ["update-ref", f"refs/nid/{name}", h, old or "0" * 40]  # null old value = must not exist yet
    if git(ctx, *args).returncode != 0:
        die(f"refs/nid/{name} changed concurrently (another --run in a linked worktree?) — rerun")


def set_ref_counter(ctx: Ctx, name: str, value: int) -> None:
    set_ref_blob(ctx, name, str(value))


def nid_snapshot(ctx: Ctx) -> dict[str, str]:
    """Hashes of everything under .no-illusory-done except evidence/ (which the checker itself writes)."""
    out = {}
    for p in ctx.nid.rglob("*"):
        if p.is_file() and "evidence" not in p.relative_to(ctx.nid).parts:
            out[str(p.relative_to(ctx.nid))] = sha_file(p)
    return out


def marker_in_product(ctx: Ctx, gates: list[Gate]) -> None:
    """A success marker is the oracle's word, never the product's. If any changed PRODUCT file contains a gate's
    EXPECT literal, the gate can be satisfied by `cat`-ing the product (or by a masked fallback that does), which is
    a sentinel — structural, so it holds in lite mode where shell-syntax bans are off."""
    caps = parse_plan(ctx)[3]
    fcommit = git(ctx, "log", "-1", "--format=%H", "--", ctx.rel(ctx.freeze)).stdout.strip()
    literals = [(g.id, (g.f["EXPECT"].strip()[1:-1] if is_regex(g.f["EXPECT"].strip()) else g.f["EXPECT"].strip()))
                for g in gates if g.kind == "cmd"]
    for f in changed_files(ctx, fcommit, include_ignored=True):
        if not under(f, caps["_product"]): continue
        p = ctx.root / f
        if not p.is_file() or p.stat().st_size > 4 * 1024 * 1024: continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for gid, lit in literals:
            if lit and lit in text:
                die(f"{gid}: product file {f} contains the success marker {lit!r} — the marker must be printed by the frozen oracle, never stored in the product (sentinel)")


def stage_a(ctx: Ctx, gates: list[Gate], record=True) -> tuple[bool, dict, list[str]]:
    """Freeze -> run -> freeze again. Returns (pass, results, unmet)."""
    if not verify_freeze(ctx, gates, quiet=True):
        verify_freeze(ctx, gates); die("refusing: freeze mismatch before run")
    influence_check(ctx, gates)
    before = nid_snapshot(ctx)
    results = run_all(ctx, gates, record)
    if not verify_freeze(ctx, gates, quiet=True):
        verify_freeze(ctx, gates)
        die("a CHECK mutated a frozen file during the run -> reject")
    after = nid_snapshot(ctx)
    if before != after:
        changed = sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})
        die(f"a CHECK wrote into .no-illusory-done during the run ({', '.join(changed)}) -> product code may not touch checker files; reject")
    marker_in_product(ctx, gates)
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
    new_e = {k: ("Satisfied" if r["pass"] else "Refuted") for k, r in results.items()}
    prev_vec = ref_blob(ctx, "evector").strip()
    new_vec = ",".join(f"{k}={v}" for k, v in sorted(new_e.items()))
    stall = 0 if ok else (stall + 1 if prev_vec == new_vec else 0)
    it += 1
    write_state(ctx, gates, results, it, stall, old)
    set_ref_counter(ctx, "iteration", it); set_ref_counter(ctx, "stall", stall); set_ref_blob(ctx, "evector", new_vec)
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
        if in_nid(ctx, p): return f"{hid}: pointer inside .no-illusory-done (not an artifact)"
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


def run_setup(ctx: Ctx) -> None:
    """SETUP: lines from the frozen PLAN, run in the clean env before Stage A (clean checkout needs deps)."""
    _, _, setup, _ = parse_plan(ctx)
    if not setup: return
    def state():
        return {f: (sha_file(ctx.root / f) if (ctx.root / f).is_file() else None) for f in changed_files(ctx, "HEAD")}, nid_snapshot(ctx)
    before = state()
    for cmd in setup:
        r = subprocess.run(["bash", "-o", "errexit", "-o", "pipefail", "-c", cmd], cwd=str(ctx.root), timeout=3600,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean_env())
        if r.returncode != 0:
            die(f"SETUP failed ({cmd!r}, exit {r.returncode}):\n" + r.stdout.decode("utf-8", "replace")[-2000:])
    after = state()
    if before != after:
        diff = sorted(set(before[0]) ^ set(after[0]) | {k for k in before[0] if k in after[0] and before[0][k] != after[0][k]})
        die(f"SETUP changed non-ignored files ({', '.join(diff[:10]) or 'checker files'}): setup may only install dependencies into gitignored paths, never touch product or tests")


def ci_verdict(ctx: Ctx) -> tuple[str, list[str]]:
    """Run SETUP, re-run Stage A, then validate CI.md. Returns (effective verdict, problems)."""
    gates = parse_ledger(ctx)
    _, highs, _, caps0 = parse_plan(ctx)
    run_setup(ctx)
    attempts = ref_counter(ctx, "ci_attempts")
    if attempts >= caps0["max_ci_attempts"]:
        return "reject", [f"HANDOFF REQUIRED: {attempts} CI attempts already rejected (max_ci_attempts={caps0['max_ci_attempts']})"]
    v = parse_ci(ctx)
    fz = verify_freeze(ctx, gates, quiet=True)
    a_ok, results, unmet = stage_a(ctx, gates, record=False)
    judge = [g.id for g in gates if g.kind == "llm-judge"]
    def repo_state():
        return ({f: (sha_file(ctx.root / f) if (ctx.root / f).is_file() else None) for f in changed_files(ctx, "HEAD")}, nid_snapshot(ctx))
    pre = repo_state()
    verdicts, problems = check_ci_pointers(ctx, highs, judge)
    post = repo_state()
    if pre != post:
        diff = sorted(set(pre[0]) ^ set(post[0]) | {k for k in pre[0] if k in post[0] and pre[0][k] != post[0][k]})
        problems.append(f"a pointer command changed files ({', '.join(diff[:10]) or 'checker files'}): pointer commands must be observational -> reject")
        verdicts = {k: "fail" for k in verdicts}
    if flaky_ids(results): problems.append(f"flaky gates passed only on retry: {','.join(flaky_ids(results))} (process fail)")
    mut = mutation_verdict(ctx, gates)
    _, _, _, caps = parse_plan(ctx)
    if mut["status"] == "fail": problems.append(f"VACUOUS ORACLE: {len(mut['survivors'])} mutants survived: " + "; ".join(mut["survivors"][:5]))
    # mutation_required: 0 waives ONLY "no python to mutate"; a capped or node-less python run is never waived.
    mutation_inconclusive = mut["status"] == "inconclusive" and (caps["mutation_required"] or mut.get("reason") != "no-python")
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
    if problems: return "reject", problems
    if mutation_inconclusive:
        hint = "set mutation_required: 0 in PLAN.md (frozen) to accept non-python changes" if mut.get("reason") == "no-python" else "remove the mutant cap / make the python mutable — this cannot be waived"
        return "inconclusive", [f"mutation inconclusive ({mut['note']}); {hint}"]
    return "merge-ok", []


def cmd_ci(ctx: Ctx) -> None:
    verdict, problems = ci_verdict(ctx)
    for p in problems: print(f"CI PROBLEM: {p}")
    if verdict != "merge-ok":
        set_ref_counter(ctx, "ci_attempts", ref_counter(ctx, "ci_attempts") + 1)
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


def changed_files(ctx: Ctx, since: str, include_ignored: bool = False) -> list[str]:
    """Committed-since-freeze + uncommitted + untracked (+ .gitignore'd files when include_ignored)."""
    def z(*args):
        return [x for x in git(ctx, "-c", "core.quotePath=false", *args).stdout.split("\0") if x]
    out = z("diff", "--name-only", "-z", since, "HEAD", "--")
    out += z("diff", "--name-only", "-z", "HEAD", "--")
    if include_ignored:
        out += [x for x in z("ls-files", "--others", "-z") if ".git/" not in x and not x.startswith(".no-illusory-done/")]
    else:
        out += z("-c", "core.excludesFile=/dev/null", "ls-files", "--others", "--exclude-per-directory=.gitignore", "-z")
    return sorted(set(out))


def count_mutants(src: str) -> int:
    m = Mutator(10**9); m.visit(ast.parse(src)); return m.count


def mutation_verdict(ctx: Ctx, gates: list[Gate], verbose=False) -> dict:
    """Returns {"status": pass|fail|inconclusive, "survivors": [...], "total": n, "note": str}.
    If PLAN declares MUTATE:/MUTATE_EXPECT:, that external tool (Stryker, mutmut, cargo-mutants…) is the verdict."""
    caps = parse_plan(ctx)[3]
    if caps["mutate_cmd"]:
        cmd, exp = caps["mutate_cmd"], caps["mutate_expect"]
        for rx, name in BAD_CHECK_ALWAYS + BAD_CHECK:
            if rx.search(cmd): return {"status": "fail", "reason": "bad-cmd", "survivors": [f"MUTATE command forbidden ({name})"], "total": 0, "note": ""}
        try:
            r = subprocess.run(["bash", "-o", "errexit", "-o", "pipefail", "-c", cmd], cwd=str(ctx.root), timeout=3600,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean_env())
        except subprocess.TimeoutExpired:
            return {"status": "fail", "reason": "timeout", "survivors": ["MUTATE timed out"], "total": 0, "note": ""}
        out = r.stdout.decode("utf-8", "replace")
        if verbose: print(out[-3000:])
        ok = r.returncode == 0 and expect_match(exp, out)
        return {"status": "pass" if ok else "fail", "reason": "external", "survivors": [] if ok else [f"MUTATE exit {r.returncode}, last line {(out.strip().splitlines() or [''])[-1]!r}"], "total": -1, "note": f"external: {cmd}"}
    frozen, _, _ = read_freeze(ctx)
    fcommit = git(ctx, "log", "-1", "--format=%H", "--", ctx.rel(ctx.freeze)).stdout.strip()
    changed = [f for f in changed_files(ctx, fcommit, include_ignored=True) if f.endswith(".py") and f not in frozen and not f.startswith("scripts/nid_check") and (ctx.root / f).is_file()]
    changed = sorted(set(changed))
    if not changed:
        return {"status": "inconclusive", "reason": "no-python", "survivors": [], "total": 0, "note": "no changed .py source since freeze (mutation v1: python only)"}
    survivors, total, truncated = [], 0, []
    _, _, _, caps = parse_plan(ctx)
    cap = caps["max_mutants_per_file"]
    for f in changed:
        src = (ctx.root / f).read_text(encoding="utf-8")
        all_n = count_mutants(src)
        if all_n == 0: continue
        n = all_n if cap == 0 else min(all_n, cap)
        if n < all_n:
            truncated.append(f"{f}: {n}/{all_n}")
            print(f"NOTE: {f}: {all_n} mutants, cap max_mutants_per_file={cap} -> result can only be fail or inconclusive")
        for i in range(1, n + 1):
            total += 1
            m = Mutator(i); tree = m.visit(ast.parse(src)); ast.fix_missing_locations(tree)
            git(ctx, "worktree", "prune")
            with tempfile.TemporaryDirectory() as td:
                wt = Path(td) / "wt"
                if git(ctx, "worktree", "add", "--detach", str(wt), "HEAD").returncode != 0: die("git worktree add failed")
                try:
                    # carry uncommitted working tree changes, then apply mutant
                    for cf in changed_files(ctx, "HEAD", include_ignored=True):
                        if DEP_DIRS.search(cf + "/"): continue
                        s, d = ctx.root / cf, wt / cf
                        if s.is_file(): d.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(s, d)
                    (wt / f).parent.mkdir(parents=True, exist_ok=True)
                    (wt / f).write_text(ast.unparse(tree), encoding="utf-8")
                    sub = Ctx(wt / ".no-illusory-done" / "LEDGER.md")
                    rs = run_all(sub, gates, record=True)
                    failed = [gid for gid, r in rs.items() if not r["pass"]]
                    # A kill counts only if some gate failed WITHOUT an import/syntax crash: a mutant that
                    # merely breaks importing proves the test imported the module, not that it tested it.
                    real = [gid for gid in failed if REAL_KILL.search((sub.evidence / f"{gid}.out").read_text(errors="replace") if (sub.evidence / f"{gid}.out").exists() else "")]
                    killed = bool(real)
                    crash_only = bool(failed) and not real
                finally:
                    os.chdir(ctx.root)
                    git(ctx, "worktree", "remove", "--force", str(wt))
            if verbose: print(f"{f} #{i} {m.desc}: {'killed' if killed else ('SURVIVED (crash-only: failed without an assertion)' if crash_only else 'SURVIVED')}")
            if not killed: survivors.append(f"{f}#{i} {m.desc}" + (" [crash-only]" if crash_only else ""))
    if total == 0:
        return {"status": "inconclusive", "reason": "no-mutable-nodes", "survivors": [], "total": 0, "note": "changed python has no mutable nodes"}
    if survivors:
        return {"status": "fail", "survivors": survivors, "total": total, "note": ""}
    if truncated:
        return {"status": "inconclusive", "reason": "truncated", "survivors": [], "total": total, "note": "mutants truncated by cap: " + "; ".join(truncated)}
    return {"status": "pass", "survivors": [], "total": total, "note": ""}


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
    print(f"FREEZE: {'match' if fz else 'mismatch'}" + ("" if git(ctx, "remote").stdout.strip() else "  (no remote: witness is local history only)"))
    print(f"FLAKY: {','.join(flaky_ids(results)) or 'none'}")
    mv = mutation_verdict(ctx, gates) if (fz and a_ok) else {"status": "not-run", "survivors": [], "note": ""}
    print(f"MUTATION: {mv['status']}" + (f" ({len(mv['survivors'])} survived)" if mv['survivors'] else "") + (f" — {mv['note']}" if mv.get('note') else ""))
    print(f"ITER: {it}")
    print("EVIDENCE: " + ("; ".join(f"{k} → exit {r['exit']}{' TIMEOUT' if r['attempts'][-1]['timeout'] else ''}, expect {r['expect_match']}, {r['sha256'][:12]}" for k, r in results.items()) or "none"))
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
            p = Path(a.ci).resolve()
            if p.name != "CI.md": die("--ci expects <repo>/.no-illusory-done/CI.md")
            cmd_ci(Ctx(p.parent / "LEDGER.md"))
        elif a.verify_freeze:
            ctx = Ctx(None); sys.exit(0 if verify_freeze(ctx, parse_ledger(ctx)) else 1)
        elif a.report: cmd_report(Ctx(None))
        elif a.hook:
            here = Path.cwd().resolve()
            if not any((d / ".no-illusory-done" / "LEDGER.md").exists() for d in (here, *here.parents)):
                sys.exit(0)
            ctx = Ctx(None)
            if not ctx.freeze.exists():
                print("NID: ledger exists but no freeze yet (test-writer phase) — stop allowed; run --red before implementing")
                sys.exit(0)
            cmd_run(ctx, hook=True)
    except SystemExit:
        raise
    except Exception as e:  # any crash is a failed verdict, never a pass
        die(f"checker crashed ({type(e).__name__}: {e}) -> fail closed", 2)


if __name__ == "__main__":
    main()
