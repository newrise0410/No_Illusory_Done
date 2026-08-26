---
name: no-illusory-done
description: Completion discipline for substantial agent work. Done is a checker verdict, not a sentence. Use when the user names a goal, a todo list, or says "don't stop until it's done." Skip for trivial edits (see §0).
---

# No Illusory Done

**Done is a verdict produced by `scripts/nid_check.py` re-running the oracles right now, not a sentence written by an agent — and not a file an agent wrote earlier.**

*Illusory completion* = stopping while belief says "finished" and evidence says Unknown or Refuted. The agent that has the illusion is an LLM; every file an LLM writes inherits it. So the checker **trusts no file as evidence**: `--run`, `--ci`, `--report`, `--mutate` all re-execute the gates in the same invocation. `STATE.md` and `evidence/` are outputs for humans, never inputs to a verdict.

## 0. Scope

Apply when any of: ≥3 files touched; a new test is needed; the user gave a goal/todo list; the user said "until it's done". Once a ledger exists, the task may not be downgraded to "trivial".

## 1. Rule zero

1. `PLAN.md` + `LEDGER.md` + failing tests **before** implementation.
2. `--red` (every gate must fail before code) writes `FREEZE.sha256`. Commit it in **one** commit. Never touch frozen files afterward; re-freezing needs `--supersede "<reason>"` and is permanently recorded.
3. Not done: last message "finished", checkboxes, `<promise>` tags, a prior run's logs, `STATE.md`, `evidence/`, "tests should pass", any LLM summary, a hand-written report.
4. Fail closed: parse error, missing gate, freeze mismatch, undeclared re-freeze, checker crash, CI parse failure → not done.
5. Commands are graded by the checker. An LLM grades only what no command can observe, and every such pass must carry a pointer the checker can re-derive.
6. Without subagents, run the roles as phases and reread only files + command output between them. That barrier is weak; `--red`, the committed freeze, `--mutate`, and the pointer checks are the strong ones.

## 2. Artifacts

```text
.no-illusory-done/
  PLAN.md          R clauses, H lines (FALSIFIER + SUBJECT), SETUP, caps   — frozen
  LEDGER.md        gates                                                   — frozen
  FREEZE.sha256    file hashes + RED output hashes + SUPERSEDE log; checker-written, committed once
  STATE.md         E column (checker) / B column (implementer); iteration, stall  — output only
  evidence/        Gn.out per gate                                          — output only
  CI.md            written only by the CI role; validated by --ci
scripts/nid_check.py                                                        — frozen
```

`PLAN.md`:

```markdown
R1: /pricing renders exactly three tier cards
R2: annual toggle shows a 20.00% discount
H1: tier order matches marketing spec | FALSIFIER: any order other than Basic, Pro, Team is visible | SUBJECT: src/pages/pricing.tsx, $ npx playwright test tiers
SETUP: npm ci
max_iterations: 8        stall_iters: 3
max_supersedes: 1        max_gates_per_r: 4
```

- `R1..Rn` — atomic clauses of the **current** request. Every gate lists the Rs it observes (`COVERS:`); every R must be covered. Checked by id only — this catches omitted requirements, not lying gates.
- `H1..Hn` — outcomes no command can observe. `FALSIFIER` names the observation that would make it false; if it *is* a command (contains `$ | && ./`, or starts with any executable on PATH) it is refused — put it in the ledger. `SUBJECT` lists the **exact regular files** and/or **exact `$ commands`** a CI pointer may cite for this H — no directories, no prefixes, no symlinks out of the repo. Anything else is rejected.
- Vague words (looks good, correctly, properly, as expected) are refused in R and H lines.

## 3. Gate contract

```markdown
- [ ] G1: pricing page renders three tier cards          # a state, not an activity
  CHECK: npx vitest run tests/pricing.spec.ts && cat tests/nid/G1.marker
  EXPECT: NID G1                                          # literal or /regex/, last non-empty line
  FILES: tests/pricing.spec.ts, tests/nid/G1.marker       # frozen; at least one required
  ENV: PRICING_MODE=test                                  # optional literal env; frozen with the ledger
  COVERS: R1
  CWD: .        TIMEOUT: 300        RETRIES: 0            # RETRIES 0..2; pass-on-retry flagged flaky
  KIND: cmd     RED: required                             # llm-judge → no CHECK; pass-ok → regression gate only
```

Checker enforces: CHECK runs under `bash -o errexit -o pipefail -o nounset` (a `;`-masked failure fails); forbidden: `echo printf true false : command eval exec source env xargs nohup nice time sh -c exit 0 passWithNoTests || true python -c touch cp mv rm tee sed -i >` and any `$`, backtick, heredoc or `VAR=` (use a repo-owned script); CHECK may not contain EXPECT; **every existing file a CHECK names must be in FILES** (a path that does not exist yet is product output), FILES non-empty, all inside the repo, no symlinks out; EXPECT regexes that match `""`/`FAIL`/arbitrary text are refused; CHECK runs in a **clean environment** (PATH/HOME/LANG/TMPDIR only, `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1`; `ENV:` may add literal values but never PATH/PYTHONPATH/NODE_PATH/LD_PRELOAD); `--run` refuses if a **runner-influencing file** (conftest.py, sitecustomize.py, *.pth, pytest.ini, pyproject.toml, package.json, jest/vitest/babel config, tsconfig, .env, Makefile, __init__.py…) was added or changed since the freeze without being frozen; no two gates with identical CHECK; at most `max_gates_per_r` gates per R; `RED: pass-ok` only for gates whose FILES were committed at HEAD before the freeze and that pass at `--red`; at least one `RED: required` gate.

## 4. Roles

**Test-writer** — reads request + code as spec; writes tests, `PLAN.md`, `LEDGER.md`; `--status`; `--red`; commits. No production code.

**Implementer** — one leaf at a time; `--run` after each; on fail, pastes the assertion text from `evidence/Gn.out` into the next turn. May edit only the `B` column of `STATE.md`. Cannot touch frozen files (wrong gate → `HANDOFF: ledger-defect`; a human or a fresh test-writer re-freezes with `--supersede "<reason>"`, at most `max_supersedes` times — beyond that the freeze is a mismatch until a human resets it). `ALL MET` is the claim, not acceptance. Caps are enforced by the checker: `iteration ≥ max_iterations` or `stall ≥ stall_iters` while unmet → `HANDOFF REQUIRED` (exit 3).

**LLM CI** — new worktree, empty conversation, forbidden inputs: implementer chat, PR summary, checkboxes, `STATE.md`.
- Stage A: `SETUP`, then `--run`. Fail → `CI: reject`, stop.
- `--mutate` (python v1): AST mutants of every source file changed since the freeze; each must be killed by some gate. Survivors → `VACUOUS ORACLE`; zero mutants or no python → `inconclusive` (exit 1, printed in `--report`, not a reject by itself). `--ci` runs this internally; survivors reject.
- Stage B: grade only H lines and `llm-judge` gates, each against its FALSIFIER, citing a pointer within its SUBJECT:
  `H1: pass @ src/pages/pricing.tsx:22-40 sha=<≥12 hex of those lines>` or `H1: pass $ npx playwright test tiers sha=<≥12 hex of output>`. `--ci` rehashes / reruns; a pointer outside SUBJECT, outside the repo, with a wrong hash, no output, or non-zero exit downgrades the pass to fail.
- Write `CI.md` (`CI / STAGE_A / STAGE_B / PROCESS / OUTCOME / UNMET / EVIDENCE:` + one verdict line per H/llm-judge id), then `--ci`. `--ci` re-runs Stage A and mutation itself; it never reads a prior run's record. A gate that passed only on retry is a process failure → reject.

## 5. Stop hook

```json
{ "hooks": { "Stop": [{ "hooks": [{ "type": "command",
  "command": "python3 scripts/nid_check.py --hook" }] }] } }
```

`--hook` exits 0 when no ledger exists, otherwise behaves as `--run`. Exit 2 blocks the stop.

## 6. Final report

`python3 scripts/nid_check.py --report` — re-runs Stage A and `--ci` validation, prints `VERDICT:`. Paste verbatim. "Done" only on `VERDICT: merge-ok` (exit 0).

## 7. What this does not solve

- Deleting `.no-illusory-done/`, rewriting git history, or never loading the skill. Visible in history; not preventable here.
- A test-writer who omits a requirement from R, or writes a gate that a mutant of the *wrong* file cannot reach. `--mutate` (python only in v1) catches vacuous oracles for changed python; other languages are `inconclusive`.
- A frozen oracle that reads a file the implementer writes (`test -f x && …`, a JSON flag, product output that is really a pass token). An oracle *must* read the product; a product that is just a pass flag is indistinguishable mechanically. `--red` proves the oracle failed at time zero, not that it fails for the right reason. For changed python, `--mutate` flags such a gate as vacuous (mutants survive); for other languages it is `inconclusive`, and a human must read the ledger.
- `COVERS` is by id. One gate claiming `R1, R2, R3` while testing R1 passes `--status`; only mutation (python) or a human catches it. A FALSIFIER that is well-formed prose but meaningless passes too — Stage B must refuse to grade it.
- Iteration/stall counters are kept in `STATE.md` and in git refs (`refs/nid/iteration`, `refs/nid/stall`; the larger wins). Deleting the refs resets the caps. Caps bound accidental looping, not adversarial looping.
- A `--supersede` reason is free text. The cap and the permanent log make abuse visible; they do not judge the reason.
- Stage B reasoning wrongly about a file it did read. Pointers prove access, not judgment. SUBJECT + FALSIFIER shrink the surface.
- Auth, payments, production merges: human review and host CI remain the bound.
