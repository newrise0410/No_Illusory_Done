#!/usr/bin/env python3
"""nid_check.py — No Illusory Done checker (Stage A oracle, freeze, CI parser).

Subcommands (all fail closed; exit 0 only on the stated success condition):

  --status LEDGER          parse only; exit 0 iff ledger is well-formed
  --red LEDGER             test-writer: run every gate, require RED, record
                           output hashes into FREEZE.sha256 (RED lines)
  --freeze LEDGER          hash LEDGER + gate FILES + this script -> FREEZE.sha256
  --verify-freeze          FREEZE.sha256 matches working tree AND git HEAD
  --run LEDGER             re-run every CHECK; update STATE.md E column;
                           print ALL MET / UNMET; refuses on freeze mismatch
  --ci CI.md               parse CI report; exit 0 iff CI: merge-ok is
                           internally consistent and freeze matches
  --report                 print the machine-generated final report

Ledger gate contract (indentation of field lines is free):

  - [ ] G1: <observable end state>
    CHECK: <command>
    EXPECT: <literal> | /regex/
    CWD: <dir>            (default .)
    TIMEOUT: <seconds>    (default 300)
    RETRIES: <n>          (default 0; max 2)
    FILES: a.py, b.spec   (test/spec files this gate depends on; frozen)
    KIND: cmd | llm-judge (default cmd; llm-judge has no CHECK, CI-only)
    RED: required | pass-ok  (default required)

EXPECT match rule: stdout+stderr combined, last non-empty line must equal
EXPECT literally (whitespace-stripped) or fullmatch /regex/.

Traceability (PLAN.md, same dir as LEDGER.md):
  R1: <atomic requirement clause>          every R must be covered by >=1 gate
  H1: <high-level state> | FALSIFIER: <observation that would make it false>
      a FALSIFIER that is a command ("$ ...") is refused: make it a KIND: cmd gate
  gate field  COVERS: R1,R3                every gate must cover >=1 known R

CI.md evidence pointers (checked by --ci; a pass without a verifiable pointer
is downgraded to reject):
  H1: pass @ src/x.ts:41-58 sha=<>=12 hex of those lines>
  H2: pass $ curl -s localhost/tiers | jq length sha=<>=12 hex of output>
  H3: fail <free text>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NID_DIR = Path(".no-illusory-done")
FREEZE_FILE = NID_DIR / "FREEZE.sha256"
STATE_FILE = NID_DIR / "STATE.md"
EVIDENCE_DIR = NID_DIR / "evidence"
LAST_RUN = EVIDENCE_DIR / "last-run.json"
CI_FILE = NID_DIR / "CI.md"
SELF = Path(__file__).resolve()

GATE_RE = re.compile(r"^- \[( |x|X)\] (G\d+):\s*(.+?)\s*$")
FIELD_RE = re.compile(r"^\s*([A-Z]+):\s*(.*?)\s*$")
FIELDS = {"CHECK", "EXPECT", "CWD", "TIMEOUT", "RETRIES", "FILES", "KIND", "RED", "EVIDENCE", "COVERS"}

# CHECK lines that observe nothing. Blacklist is a weak barrier; the real
# barrier is --red (oracle must fail before implementation).
BAD_CHECK = [
    (re.compile(r"(^|[;&|]\s*)echo\b"), "echo"),
    (re.compile(r"(^|[;&|]\s*)printf\b"), "printf"),
    (re.compile(r"(^|[;&|]\s*)true\s*($|[;&|])"), "true"),
    (re.compile(r"\bexit\s+0\b"), "exit 0"),
    (re.compile(r"passWithNoTests"), "passWithNoTests"),
    (re.compile(r"python[3]?\s+-c\s+['\"]print"), "python -c print"),
]


def die(msg: str, code: int = 2) -> None:
    print(f"NID FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(p: Path) -> str:
    return sha(p.read_bytes())


# --------------------------------------------------------------------------
# Ledger parsing
# --------------------------------------------------------------------------
class Gate:
    def __init__(self, gid: str, title: str, checked: bool):
        self.id = gid
        self.title = title
        self.checked = checked
        self.f: dict[str, str] = {}

    @property
    def kind(self) -> str:
        return self.f.get("KIND", "cmd")

    @property
    def files(self) -> list[str]:
        raw = self.f.get("FILES", "")
        return [x.strip() for x in raw.split(",") if x.strip()]


def parse_ledger(path: Path) -> list[Gate]:
    if not path.exists():
        die(f"ledger missing: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        die("empty ledger")
    gates: list[Gate] = []
    cur: Gate | None = None
    for ln, line in enumerate(text.splitlines(), 1):
        m = GATE_RE.match(line)
        if m:
            cur = Gate(m.group(2), m.group(3), m.group(1).lower() == "x")
            gates.append(cur)
            continue
        fm = FIELD_RE.match(line)
        if fm and cur is not None and fm.group(1) in FIELDS:
            if fm.group(1) in cur.f:
                die(f"{cur.id}: duplicate field {fm.group(1)} (line {ln})")
            cur.f[fm.group(1)] = fm.group(2)
    if not gates:
        die("zero gates")
    seen = set()
    for g in gates:
        if g.id in seen:
            die(f"duplicate gate id {g.id}")
        seen.add(g.id)
        if re.match(r"^(run|execute|check|test|verify)\b", g.title, re.I):
            die(f"{g.id}: title is an activity, not a state: {g.title!r}")
        if g.kind not in ("cmd", "llm-judge"):
            die(f"{g.id}: bad KIND {g.kind!r}")
        if g.kind == "llm-judge":
            if "CHECK" in g.f or "EXPECT" in g.f:
                die(f"{g.id}: llm-judge gates must not have CHECK/EXPECT")
            continue
        chk, exp = g.f.get("CHECK", "").strip(), g.f.get("EXPECT", "").strip()
        if not chk or not exp:
            die(f"{g.id}: missing CHECK or EXPECT")
        for rx, name in BAD_CHECK:
            if rx.search(chk):
                die(f"{g.id}: forbidden CHECK pattern ({name})")
        lit = exp[1:-1] if is_regex(exp) else exp
        if lit and lit in chk:
            die(f"{g.id}: CHECK contains EXPECT text (self-fulfilling)")
        if g.f.get("RED", "required") not in ("required", "pass-ok"):
            die(f"{g.id}: bad RED value")
        try:
            t = int(g.f.get("TIMEOUT", "300"))
            r = int(g.f.get("RETRIES", "0"))
        except ValueError:
            die(f"{g.id}: TIMEOUT/RETRIES must be integers")
        if t <= 0 or r < 0 or r > 2:
            die(f"{g.id}: TIMEOUT must be >0, RETRIES 0..2")
    if all(g.kind == "llm-judge" for g in gates):
        die("all gates are llm-judge; at least one runnable gate required")
    reqs, _ = parse_plan(path)
    check_traceability(gates, reqs)
    return gates


R_RE = re.compile(r"^(R\d+):\s*(.+?)\s*$")
H_RE = re.compile(r"^(H\d+):\s*(.+?)\s*$")
CMDISH = re.compile(r"^\$\s|^(grep|curl|git|npm|npx|pnpm|yarn|python3?|pytest|node|test|ls|cat|diff|jq|make|cargo|go)\b")


def parse_plan(ledger: Path) -> tuple[dict[str, str], dict[str, dict]]:
    plan = ledger.parent / "PLAN.md"
    if not plan.exists():
        die(f"PLAN.md missing next to {ledger}")
    reqs, highs = {}, {}
    for ln, line in enumerate(plan.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip().lstrip("-* ").strip()
        m = R_RE.match(line)
        if m:
            if m.group(1) in reqs:
                die(f"PLAN.md duplicate {m.group(1)} (line {ln})")
            reqs[m.group(1)] = m.group(2)
            continue
        m = H_RE.match(line)
        if m:
            hid = m.group(1)
            if hid in highs:
                die(f"PLAN.md duplicate {hid} (line {ln})")
            body = m.group(2)
            if "| FALSIFIER:" not in body:
                die(f"PLAN.md {hid}: missing '| FALSIFIER: <observation>' (line {ln})")
            state, fals = [x.strip() for x in body.split("| FALSIFIER:", 1)]
            if len(state) < 8 or len(fals) < 8:
                die(f"PLAN.md {hid}: state/FALSIFIER too vague (line {ln})")
            if CMDISH.match(fals):
                die(f"PLAN.md {hid}: FALSIFIER is a command -> make it a KIND: cmd gate, not a HIGH-LEVEL line")
            if re.search(r"looks good|covers the feature|works correctly|as expected", body, re.I):
                die(f"PLAN.md {hid}: forbidden vague phrase (line {ln})")
            highs[hid] = {"state": state, "falsifier": fals}
    if not reqs:
        die("PLAN.md has no R1.. requirement clauses")
    return reqs, highs


def check_traceability(gates: list[Gate], reqs: dict[str, str]) -> None:
    covered = set()
    for g in gates:
        cov = [x.strip() for x in g.f.get("COVERS", "").split(",") if x.strip()]
        if not cov:
            die(f"{g.id}: missing COVERS (which R does this gate observe?)")
        for r in cov:
            if r not in reqs:
                die(f"{g.id}: COVERS unknown requirement {r}")
        covered.update(cov)
    missing = sorted(set(reqs) - covered, key=lambda x: int(x[1:]))
    if missing:
        die(f"requirements with no gate: {','.join(missing)}")


def is_regex(exp: str) -> bool:
    return len(exp) >= 2 and exp.startswith("/") and exp.endswith("/")


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


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def run_gate(g: Gate) -> dict:
    cwd = Path(g.f.get("CWD", ".")).resolve()
    timeout = int(g.f.get("TIMEOUT", "300"))
    retries = int(g.f.get("RETRIES", "0"))
    attempts = []
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            p = subprocess.run(
                g.f["CHECK"], shell=True, cwd=str(cwd), timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            out, code, timed_out = p.stdout.decode("utf-8", "replace"), p.returncode, False
        except subprocess.TimeoutExpired as e:
            out = (e.output or b"").decode("utf-8", "replace") + "\n[NID TIMEOUT]"
            code, timed_out = -1, True
        except OSError as e:
            out, code, timed_out = f"[NID EXEC ERROR] {e}", -1, False
        ok = (code == 0) and (not timed_out) and expect_match(g.f["EXPECT"], out)
        attempts.append({
            "attempt": attempt, "exit": code, "timeout": timed_out,
            "expect_match": expect_match(g.f["EXPECT"], out),
            "sha256": sha(out.encode()), "bytes": len(out.encode()),
            "seconds": round(time.time() - t0, 2), "pass": ok,
        })
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_DIR / f"{g.id}.out").write_text(out, encoding="utf-8")
        if ok:
            break
    last = attempts[-1]
    return {"id": g.id, "pass": last["pass"], "exit": last["exit"],
            "expect_match": last["expect_match"], "sha256": last["sha256"],
            "bytes": last["bytes"], "attempts": attempts,
            "flaky": last["pass"] and len(attempts) > 1}


# --------------------------------------------------------------------------
# Freeze
# --------------------------------------------------------------------------
def freeze_targets(ledger: Path, gates: list[Gate]) -> list[Path]:
    paths = {ledger.resolve(), SELF}
    for g in gates:
        for f in g.files:
            p = Path(f)
            if not p.exists():
                die(f"{g.id}: FILES entry missing: {f}")
            paths.add(p.resolve())
    return sorted(paths)


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


def read_freeze() -> tuple[dict[str, str], dict[str, str]]:
    if not FREEZE_FILE.exists():
        die("FREEZE.sha256 missing")
    files, reds = {}, {}
    for line in FREEZE_FILE.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "RED":
            reds[parts[1]] = parts[2]
        elif len(parts) == 2:
            files[parts[1]] = parts[0]
    if not files:
        die("FREEZE.sha256 has no file hashes")
    return files, reds


def git_head_blob(path: Path) -> str | None:
    try:
        r = subprocess.run(["git", "show", f"HEAD:{rel(path)}"], capture_output=True)
    except OSError:
        return None
    return sha(r.stdout) if r.returncode == 0 else None


def verify_freeze(quiet: bool = False) -> bool:
    files, _ = read_freeze()
    ok = True
    for f, h in files.items():
        p = Path(f)
        if not p.exists():
            ok = False; print(f"FREEZE MISMATCH: missing {f}")
        elif sha_file(p) != h:
            ok = False; print(f"FREEZE MISMATCH: modified {f}")
    # FREEZE.sha256 itself must be committed and identical to HEAD, so that
    # re-hashing in the working tree is detectable.
    in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            capture_output=True).returncode == 0
    if in_git:
        head = git_head_blob(FREEZE_FILE)
        if head is None:
            ok = False; print("FREEZE MISMATCH: FREEZE.sha256 not committed at HEAD")
        elif head != sha_file(FREEZE_FILE):
            ok = False; print("FREEZE MISMATCH: FREEZE.sha256 differs from HEAD (re-hash detected)")
    if not quiet:
        print("FREEZE: match" if ok else "FREEZE: mismatch")
    return ok


def cmd_freeze(ledger: Path) -> None:
    gates = parse_ledger(ledger)
    lines = [f"{sha_file(p)}  {rel(p)}" for p in freeze_targets(ledger, gates)]
    reds = []
    if FREEZE_FILE.exists():
        _, old = read_freeze()
        reds = [f"RED {k} {v}" for k, v in old.items()]
    NID_DIR.mkdir(exist_ok=True)
    FREEZE_FILE.write_text("\n".join(lines + reds) + "\n")
    print(f"froze {len(lines)} files -> {FREEZE_FILE}; now: git add + commit it")


def cmd_red(ledger: Path) -> None:
    gates = parse_ledger(ledger)
    bad = []
    reds = []
    for g in gates:
        if g.kind != "cmd":
            continue
        r = run_gate(g)
        if r["pass"] and g.f.get("RED", "required") == "required":
            bad.append(g.id)
        reds.append(f"RED {g.id} {r['sha256']}")
        print(f"{g.id}: {'GREEN (not RED!)' if r['pass'] else 'RED'} exit={r['exit']}")
    if bad:
        die(f"gates already pass before implementation: {','.join(bad)} "
            f"(mark RED: pass-ok only for regression gates)")
    lines = [f"{sha_file(p)}  {rel(p)}" for p in freeze_targets(ledger, gates)]
    NID_DIR.mkdir(exist_ok=True)
    FREEZE_FILE.write_text("\n".join(lines + reds) + "\n")
    print(f"RED recorded + froze {len(lines)} files -> {FREEZE_FILE}; now: git add + commit it")


# --------------------------------------------------------------------------
# STATE.md (E column is machine-owned; B column is implementer-owned)
# --------------------------------------------------------------------------
STATE_ROW = re.compile(r"^\|\s*(G\d+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|(.*)\|\s*$")


def read_state() -> dict[str, dict]:
    st = {}
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text().splitlines():
            m = STATE_ROW.match(line)
            if m:
                st[m.group(1)] = {"E": m.group(2), "B": m.group(3), "note": m.group(4).strip()}
    return st


def write_state(gates: list[Gate], results: dict[str, dict], iteration: int, stall: int) -> None:
    old = read_state()
    rows = ["| id | E (evidence) | B (belief) | note |", "|----|----|----|----|"]
    for g in gates:
        prev = old.get(g.id, {"B": "Unaddress", "note": ""})
        if g.kind == "llm-judge":
            e = "CI-only"
        else:
            r = results[g.id]
            e = "Satisfied" if r["pass"] else "Refuted"
        note = f"exit={results[g.id]['exit']} sha={results[g.id]['sha256'][:12]}" if g.id in results else ""
        rows.append(f"| {g.id} | {e} | {prev['B']} | {note} |")
    hdr = f"iteration: {iteration}\nstall: {stall}\n\n"
    STATE_FILE.write_text(hdr + "\n".join(rows) + "\n")


def read_state_meta() -> tuple[int, int]:
    it, stall = 0, 0
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text().splitlines():
            if line.startswith("iteration:"):
                it = int(line.split(":")[1])
            elif line.startswith("stall:"):
                stall = int(line.split(":")[1])
    return it, stall


def cmd_run(ledger: Path) -> None:
    gates = parse_ledger(ledger)
    if not verify_freeze():
        die("refusing --run: freeze mismatch")
    prev_state = read_state()
    it, stall = read_state_meta()
    results = {}
    for g in gates:
        if g.kind != "cmd":
            continue
        r = run_gate(g)
        results[g.id] = r
        flag = " (flaky: passed on retry)" if r["flaky"] else ""
        print(f"{g.id}: {'PASS' if r['pass'] else 'FAIL'} exit={r['exit']} "
              f"expect={r['expect_match']} sha={r['sha256'][:12]} bytes={r['bytes']}{flag}")
    prev_e = {k: v["E"] for k, v in prev_state.items()}
    new_e = {k: ("Satisfied" if v["pass"] else "Refuted") for k, v in results.items()}
    stall = stall + 1 if prev_e and {k: prev_e.get(k) for k in new_e} == new_e else 0
    it += 1
    write_state(gates, results, it, stall)
    unmet = [g.id for g in gates if g.kind == "cmd" and not results[g.id]["pass"]]
    unmet += [g.id for g in gates if g.kind == "llm-judge"]  # runnable-only ALL MET
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps({
        "ledger_sha": sha_file(ledger), "iteration": it, "stall": stall,
        "results": results, "unmet_cmd": [u for u in unmet if u in results],
        "llm_judge": [g.id for g in gates if g.kind == "llm-judge"],
    }, indent=2))
    cmd_unmet = [u for u in unmet if u in results]
    if cmd_unmet:
        print(f"UNMET: {','.join(cmd_unmet)}")
        sys.exit(1)
    judge = [g.id for g in gates if g.kind == "llm-judge"]
    print("ALL MET" + (f" (llm-judge pending CI: {','.join(judge)})" if judge else ""))
    sys.exit(0)


# --------------------------------------------------------------------------
# CI.md parsing
# --------------------------------------------------------------------------
CI_KEYS = ["CI", "STAGE_A", "STAGE_B", "PROCESS", "OUTCOME", "UNMET", "EVIDENCE"]
CI_ALLOWED = {
    "CI": {"merge-ok", "reject", "inconclusive"},
    "STAGE_A": {"pass", "fail"},
    "STAGE_B": {"pass", "fail", "skipped"},
    "PROCESS": {"pass", "fail"},
    "OUTCOME": {"pass", "fail"},
}


def parse_ci(path: Path) -> dict:
    if not path.exists() or not path.read_text().strip():
        die("CI.md missing or empty -> reject")
    vals = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z_]+):\s*(.*?)\s*$", line)
        if m and m.group(1) in CI_KEYS and m.group(1) not in vals:
            vals[m.group(1)] = m.group(2)
    for k in CI_KEYS:
        if k not in vals:
            die(f"CI.md missing field {k} -> reject")
    for k, allowed in CI_ALLOWED.items():
        if vals[k] not in allowed:
            die(f"CI.md bad value {k}={vals[k]!r} -> reject")
    return vals


PTR_RE = re.compile(r"^([GH]\d+):\s*(pass|fail)\s*(.*?)\s*$")
FILE_PTR = re.compile(r"^@\s*(\S+?)(?::(\d+)(?:-(\d+))?)?\s+sha=([0-9a-f]{12,64})$")
CMD_PTR = re.compile(r"^\$\s*(.+?)\s+sha=([0-9a-f]{12,64})$")


def verify_pointer(hid: str, ptr: str) -> str | None:
    """Return None if the pointer checks out, else a reason."""
    m = FILE_PTR.match(ptr)
    if m:
        p, l1, l2, h = Path(m.group(1)), m.group(2), m.group(3), m.group(4)
        if not p.exists():
            return f"{hid}: pointer file missing {p}"
        if l1:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            a, b = int(l1), int(l2 or l1)
            if a < 1 or b > len(lines) or a > b:
                return f"{hid}: line range {a}-{b} out of bounds for {p}"
            actual = sha("\n".join(lines[a - 1:b]).encode())
        else:
            actual = sha_file(p)
        return None if actual.startswith(h) else f"{hid}: file hash mismatch for {p} (grader did not read this version)"
    m = CMD_PTR.match(ptr)
    if m:
        cmd, h = m.group(1), m.group(2)
        try:
            r = subprocess.run(cmd, shell=True, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"{hid}: pointer command failed to run ({e})"
        actual = sha(r.stdout.decode("utf-8", "replace").encode())
        return None if actual.startswith(h) else f"{hid}: command output hash mismatch (grader did not run this)"
    return f"{hid}: pass without a verifiable pointer (@ path sha=.. | $ cmd sha=..)"


def check_ci_pointers(path: Path, required: list[str]) -> tuple[dict[str, str], list[str]]:
    verdicts, problems = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PTR_RE.match(line.strip())
        if not m:
            continue
        hid, res, ptr = m.groups()
        if hid in verdicts:
            problems.append(f"{hid}: duplicate verdict line"); continue
        verdicts[hid] = res
        if res == "pass":
            why = verify_pointer(hid, ptr)
            if why:
                problems.append(why); verdicts[hid] = "fail"
    for hid in required:
        if hid not in verdicts:
            problems.append(f"{hid}: no verdict line in CI.md")
    return verdicts, problems


def cmd_ci(path: Path) -> None:
    v = parse_ci(path)
    fz = verify_freeze(quiet=True)
    if not LAST_RUN.exists():
        die("no last-run.json: Stage A never ran -> reject")
    last = json.loads(LAST_RUN.read_text())
    stage_a_ok = not last["unmet_cmd"]
    problems = []
    ledger = path.parent / "LEDGER.md"
    _, highs = parse_plan(ledger)
    required = list(highs) + last.get("llm_judge", [])
    verdicts, ptr_problems = check_ci_pointers(path, required)
    failed = [k for k in required if verdicts.get(k) != "pass"]
    if v["CI"] == "merge-ok":
        problems += ptr_problems
        if failed: problems.append(f"HIGH-LEVEL/llm-judge not passed with verified pointer: {','.join(failed)}")
        if not fz: problems.append("freeze mismatch")
        if v["STAGE_A"] != "pass" or not stage_a_ok: problems.append("Stage A not pass on record")
        if v["PROCESS"] != "pass": problems.append("process fail")
        if v["OUTCOME"] != "pass": problems.append("outcome fail")
        if v["STAGE_B"] == "fail": problems.append("Stage B fail")
        if v["UNMET"].strip().lower() not in ("none", "", "-"): problems.append(f"UNMET non-empty: {v['UNMET']}")
    if problems:
        die("CI.md claims merge-ok but: " + "; ".join(problems) + " -> reject")
    print(f"CI: {v['CI']}")
    sys.exit(0 if v["CI"] == "merge-ok" else 1)


# --------------------------------------------------------------------------
# Final report (machine-generated; the agent must not hand-write it)
# --------------------------------------------------------------------------
def cmd_report() -> None:
    fz = FREEZE_FILE.exists() and verify_freeze(quiet=True)
    last = json.loads(LAST_RUN.read_text()) if LAST_RUN.exists() else None
    ci = None
    if CI_FILE.exists() and CI_FILE.read_text().strip():
        try:
            ci = parse_ci(CI_FILE)
        except SystemExit:
            ci = {"CI": "reject(parse-fail)"}
    stage_a = bool(last) and not last["unmet_cmd"]
    unmet = (last["unmet_cmd"] if last else ["<never run>"])
    ci_verdict = ci["CI"] if ci else "not-run"
    if ci_verdict == "merge-ok" and stage_a and fz:
        verdict = "merge-ok"
    elif ci_verdict in ("reject", "reject(parse-fail)"):
        verdict = "reject"
    elif ci_verdict == "inconclusive":
        verdict = "inconclusive"
    else:
        verdict = "not-verified"
    ev = "; ".join(f"{k} → exit {r['exit']}, expect {r['expect_match']}, {r['sha256'][:12]}"
                   for k, r in (last or {}).get("results", {}).items()) or "none"
    it, _ = read_state_meta()
    print(f"VERDICT: {verdict}")
    print(f"STAGE_A: {'pass' if stage_a else 'fail'}")
    print(f"CI: {ci_verdict}")
    print(f"UNMET: {','.join(unmet) if unmet else 'none'}")
    print(f"ORACLE: nid_check.py --run → {'0' if stage_a else '1'}")
    print(f"FREEZE: {'match' if fz else 'mismatch'}")
    print(f"ITER: {it}")
    print(f"EVIDENCE: {ev}")
    print(f"CI.md: {CI_FILE if ci else 'missing'}")
    sys.exit(0 if verdict == "merge-ok" else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", metavar="LEDGER")
    g.add_argument("--red", metavar="LEDGER")
    g.add_argument("--freeze", metavar="LEDGER")
    g.add_argument("--verify-freeze", action="store_true")
    g.add_argument("--run", metavar="LEDGER")
    g.add_argument("--ci", metavar="CI_MD")
    g.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.status:
        gates = parse_ledger(Path(a.status))
        print(f"OK: {len(gates)} gates ({sum(g.kind=='cmd' for g in gates)} runnable)")
    elif a.red:
        cmd_red(Path(a.red))
    elif a.freeze:
        cmd_freeze(Path(a.freeze))
    elif a.verify_freeze:
        sys.exit(0 if verify_freeze() else 1)
    elif a.run:
        cmd_run(Path(a.run))
    elif a.ci:
        cmd_ci(Path(a.ci))
    elif a.report:
        cmd_report()


if __name__ == "__main__":
    main()
