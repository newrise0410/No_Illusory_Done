---
name: no-illusory-done
description: Completion discipline for substantial agent work. Done is a checker verdict, not a sentence. Use when the user names a goal, a todo list, or says "don't stop until it's done." Skip for trivial edits (see §0).
---

# No Illusory Done

**Done is a ledger state produced by `scripts/nid_check.py`, not a sentence written by the agent.**
The implementer produces a *claim*. Only **LLM CI** (isolated re-run + high-level outcome grade) may *accept*, and even that acceptance is validated by the checker (`--ci`).

*Illusory completion* = stopping while belief says "finished" and evidence says Unknown or Refuted. Everything below exists to make that state mechanically unreachable, or at least mechanically visible.

## 0. Scope

Apply when **any** of: the change touches ≥3 files; a new test is needed; the user gave a goal/todo list; the user said "until it's done". Otherwise skip. The agent may not downgrade a task to "trivial" once the ledger exists.

## 1. Rule zero

1. Write `PLAN.md` + `LEDGER.md` **before** implementing.
2. Run `--red` (oracle must fail before code), then commit `FREEZE.sha256`. Never touch frozen files afterward.
3. Nothing below counts as done: last message "finished", ticked checkboxes, `<promise>` tags, a prior run's logs, "tests should pass", any LLM rubber-stamp of a summary, a hand-written final report.
4. Fail closed. Parse error, missing gate, skipped check, stale evidence, freeze mismatch, CI parse failure → **not done**.
5. Commands are graded by the checker. The LLM grades only what a command cannot observe.
6. Without subagents, simulate roles in phases and reread **only files and command output** between phases. This is a weak barrier — the strong barriers are `--red`, the git-committed freeze, and the checker.

## 2. Artifacts

```text
.no-illusory-done/
  PLAN.md            # outcomes, HIGH-LEVEL lines (H1..), SETUP, caps
  LEDGER.md          # gates (frozen)
  FREEZE.sha256      # file hashes + RED output hashes (committed; checker-written)
  STATE.md           # E column = checker-owned; B column = implementer-owned
  evidence/          # Gn.out per gate + last-run.json (checker-written)
  CI.md              # written only by the CI role; validated by --ci
scripts/nid_check.py # bundled with this skill; hashed into FREEZE
```

`PLAN.md` must contain:

- One line per independently required outcome of the **current** request (one leaf = one deliverable).
- `SETUP:` commands a clean checkout needs before Stage A (`npm ci`, `pip install -e .`, build).
- `HIGH-LEVEL` outcomes `H1..Hn`: things no CHECK can observe (game rules hold, no secrets in the diff, UI state). Each must be specific, non-overlapping, and point to an inspectable artifact. "Covers the feature" / "looks good" are forbidden.
- Caps: `max_iterations: 8`, `stall_iters: 3`, `max_ci_attempts: 3`.

## 3. Gate contract (checker enforces every line)

```markdown
- [ ] G1: pricing fixtures render three tiers        # a STATE, not an activity
  CHECK: npm test -- pricing.spec.ts && node scripts/nid-mark.js G1
  EXPECT: NID G1                                       # literal, or /regex/
  CWD: .            # default .
  TIMEOUT: 120      # seconds, default 300; timeout = fail
  RETRIES: 0        # 0..2; a pass on retry is recorded as flaky
  FILES: tests/pricing.spec.ts, fixtures/pricing.json   # frozen with the ledger
  KIND: cmd         # or llm-judge (no CHECK; graded only in CI Stage B)
  RED: required     # or pass-ok, only for regression gates that already pass
```

**Match rule:** stdout+stderr combined; the **last non-empty line** must equal EXPECT (or fullmatch the regex). Print the marker after all asserts so a partial run cannot match.

Checker rejects: titles starting with run/test/verify/check; `echo`, `printf`, `true`, `exit 0`, `passWithNoTests`, `python -c print`; CHECK text containing EXPECT; duplicate ids; zero gates; a ledger with only `llm-judge` gates. The blacklist is a convenience — **`--red` is the real proof that the oracle observes something**: a gate that passes before implementation is refused unless `RED: pass-ok`.

## 4. Three roles

### Test-writer (no production code)
1. Read the request and existing code as **spec**.
2. Write failing tests + `LEDGER.md` + `PLAN.md` (HIGH-LEVEL, SETUP, caps).
3. `python scripts/nid_check.py --status .no-illusory-done/LEDGER.md`
4. `python scripts/nid_check.py --red .no-illusory-done/LEDGER.md` — records RED output hashes and writes `FREEZE.sha256`.
5. `git add .no-illusory-done scripts/nid_check.py <FILES>` and **commit**. `--run` refuses if `FREEZE.sha256` is uncommitted or differs from HEAD.

### Implementer (cannot declare done)
- May read ledger/tests. Must not modify any hashed file. If a gate is wrong → `HANDOFF: ledger-defect` in `STATE.md` note; never "fix" the oracle.
- One leaf at a time. Search before assuming "not implemented".
- After each leaf: `python scripts/nid_check.py --run .no-illusory-done/LEDGER.md`. On fail, paste the **assertion text** from `evidence/Gn.out` into the next turn, not "try harder".
- The checker updates E and `iteration`/`stall`. The implementer may only edit the **B** column (Affirm / Deny / Unaddress) and notes.
- Stop is allowed only when `--run` prints `ALL MET` (a stop hook enforces this, §7). That is the *claim*, not acceptance.

### LLM CI (the only accept)
Runs after `ALL MET`, in a **new worktree / clean checkout**, empty conversation. Forbidden inputs: implementer chat, PR summary, checkboxes.

**Stage A (no LLM judgment):** run `SETUP`, then `python scripts/nid_check.py --run .no-illusory-done/LEDGER.md`. Exit ≠ 0 → write `CI: reject` with the UNMET ids and stop; do not grade.

**Stage B (LLM, tools allowed, read-only on code/oracles):** grade **only** `H1..Hn` and any `KIND: llm-judge` gates. Per criterion: `pass|fail` + a pointer (path, command, snippet from **this** run). Split:
- `PROCESS`: hooks ran, no skipped suite, no flaky flags in `last-run.json`, environment was sane.
- `OUTCOME`: would the user consider the requested state true?
Uncontrollable env failure (login wall, missing secret) = `PROCESS: pass`, `OUTCOME: fail`, `CI: inconclusive`. No affirmation from memory.

Write `.no-illusory-done/CI.md` exactly:

```text
CI: merge-ok | reject | inconclusive
STAGE_A: pass | fail
STAGE_B: pass | fail | skipped
PROCESS: pass | fail
OUTCOME: pass | fail
UNMET: none | G3,H2
EVIDENCE: H1 → path:line / command → snippet; H2 → ...
```

Then `python scripts/nid_check.py --ci .no-illusory-done/CI.md`. It exits 0 only if `merge-ok` is consistent with the on-disk Stage A record and the freeze. An inconsistent `merge-ok` is downgraded to reject.

## 5. STATE.md (belief ≠ evidence)

| id | E (evidence, checker-owned) | B (belief, implementer-owned) | note |
|----|------|------|------|
| G1 | Satisfied / Refuted / CI-only | Affirm / Deny / Unaddress | |

Illegal stops (the checker + stop hook block them):
- **Bare assertion**: B=Affirm, E≠Satisfied.
- **Overlooked refutation**: E=Refuted, B≠Deny.
- **Stagnation**: `stall ≥ stall_iters` (checker increments when E vector is unchanged across runs) → outer reset or HANDOFF.
- **Cap**: `iteration ≥ max_iterations` → `HANDOFF REQUIRED: <unmet ids>`. Never summarize as success.

`ABANDON: G3 <reason>` is a handoff, never a pass.

## 6. Outer loop (Ralph layer)

When the session is rotting or `stall` fires: persist only files + git; start a fresh process that reads `PLAN.md`, `LEDGER.md`, `STATE.md`, `evidence/`. Pick the single highest-priority unmet gate. Same freeze, same oracles. After `ALL MET`, run CI in yet another fresh context. `CI: reject` reasons become the next implementer prompt.

## 7. Stop hook (the only enforcement the host gives you)

`.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "test ! -f .no-illusory-done/LEDGER.md || python3 scripts/nid_check.py --run .no-illusory-done/LEDGER.md >/dev/null 2>&1 || { echo 'NID: unmet gates — see .no-illusory-done/evidence/'; exit 2; }"
      }]
    }]
  }
}
```

Exit 2 blocks the stop and feeds the message back. Without this hook, the skill is advice.

## 8. Final report — machine-generated

```text
python scripts/nid_check.py --report
```

Paste its output verbatim. Do not hand-write a `VERDICT:` line. Mark host todos complete and tell the user "done" **only** when `--report` prints `VERDICT: merge-ok` (exit 0).

## 9. What this skill does not claim

- It cannot stop an agent that never loads it, or that deletes `.no-illusory-done/`. Git history will show that.
- The CHECK blacklist is bypassable; `--red` is the real guard, and a vacuous test under a vague spec still passes `--red`. Tighten the spec or HANDOFF.
- Stage B is an LLM. Vague HIGH-LEVEL lines get gamed; `--ci` only checks form, not truth.
- Human review and host CI remain the right bound for auth, payments, and production merges.
