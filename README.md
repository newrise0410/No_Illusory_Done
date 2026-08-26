# No Illusory Done

**Done is a checker verdict, not a sentence.**

A completion-discipline skill for coding agents (Claude Code, Codex, or any agent that can run shell commands), plus a small Python checker that makes "done" a state on disk rather than a claim in chat.

- [`SKILL.md`](SKILL.md) — the skill: roles, artifacts, gate contract, stop hook
- [`scripts/nid_check.py`](scripts/nid_check.py) — the checker (`--status`, `--red`, `--run`, `--ci`, `--report`, …)

---

## 1. The problem: illusory completion

An agent that works on a multi-step task eventually says "done". That sentence is produced by the same process that did the work, using the same context, with a strong prior toward finishing. Common ways it goes wrong:

| Symptom | What actually happened |
|---|---|
| "All tests pass" | Tests were run three turns ago, before the last edit |
| "Implemented as requested" | Half of the request never got an oracle; nothing could fail |
| A todo list with every box ticked | Ticking is free; nothing checked the box against reality |
| "Fixed the failing test" | The test was edited to match the code |
| An LLM reviewer says LGTM | It read the implementer's summary, not the artifacts |

We call the underlying state **illusory completion**: *belief* says finished while *evidence* says Unknown or Refuted.

## 2. The idea: belief ≠ evidence, and only commands set evidence

Three principles drive everything else.

1. **Separate belief from evidence.** Every gate has two columns in `STATE.md`: `E` (evidence: Satisfied / Refuted / Unknown) and `B` (belief: Affirm / Deny / Unaddress). The agent may write `B`. Only the checker writes `E`. "Done" is `E = Satisfied` for every gate — `B` never counts.
2. **The implementer cannot declare done.** It produces a *claim* (`ALL MET` from the checker). Acceptance comes from a separate, isolated **LLM CI** role — and even that verdict is validated by the checker for internal consistency.
3. **An LLM judges only what a command cannot observe.** Anything expressible as a command with an expected output is a runnable gate, graded mechanically. The judgment surface left to an LLM is deliberately made as small as possible, and each judgment must carry a machine-verifiable pointer.

Everything in the skill is a mechanism for making these three principles hard to violate *by accident*, and visible in git history when violated on purpose.

## 3. Mechanisms

### 3.1 Gates (the ledger)

`LEDGER.md` is a list of observable end states, each with a command and an expected last line:

```markdown
- [ ] G1: pricing fixtures render three tiers
  CHECK: npm test -- pricing.spec.ts && node scripts/nid-mark.js G1
  EXPECT: NID G1
  FILES: tests/pricing.spec.ts, fixtures/pricing.json
  COVERS: R1, R3
```

The checker refuses gates that observe nothing: titles that are activities ("run tests"), `echo`/`printf`/`true`/`exit 0`/`--passWithNoTests`, a CHECK that contains its own EXPECT. The blacklist is a convenience; the real guard is the next mechanism.

### 3.2 RED before code (`--red`)

Before any implementation, the test-writer runs every gate. **Each must fail.** A gate that passes before the code exists observes nothing about the code (or is a regression gate and must say so with `RED: pass-ok`). The output hash of each RED run is recorded — proof that this oracle, in this form, was red at time zero.

### 3.3 Freeze (replaces "human approves the tests")

`--red` writes `FREEZE.sha256` containing the hashes of `LEDGER.md`, every `FILES:` entry, **and the checker itself**. You commit it. From then on:

- `--run` refuses if any hashed file differs from the freeze.
- `--run` refuses if `FREEZE.sha256` in the working tree differs from `git HEAD` — so re-hashing to make a gate pass is detected, not just forbidden.
- Editing the checker is caught the same way.

The implementer's only legal move on a wrong gate is `HANDOFF: ledger-defect`, never "fix the test".

### 3.4 Traceability (R clauses and `COVERS:`)

`PLAN.md` decomposes the user request into atomic clauses `R1..Rn`. Every gate must declare which clauses it observes; every clause must be observed by at least one gate. This catches the most common quiet failure — *part of the request never reached any oracle* — without asking an LLM whether "the feature is covered".

### 3.5 Falsifiable HIGH-LEVEL outcomes (H lines)

Some outcomes genuinely cannot be a command (game rules hold, UI state is sensible, no credentials in the diff). These go in `PLAN.md` as `H1..Hn`, and each must name its **falsifier** — what observation would make it false:

```text
H1: no credentials in the diff | FALSIFIER: a string shaped like an API token appears in the diff
```

If the falsifier is a command (`$ grep …`, or starts with grep/curl/git/npm/…), the checker refuses: that is a runnable gate, put it in the ledger. Vague phrases ("looks good", "works correctly") are refused. What survives is the small residue an LLM truly has to judge.

### 3.6 Two-stage CI, with verifiable pointers

**Stage A** — a clean checkout runs `nid_check.py --run`. No LLM. Fail → reject, and the grader is never invoked.

**Stage B** — an LLM, in an empty conversation, grades only the H lines and `KIND: llm-judge` gates. A `pass` needs a pointer the checker can re-derive:

```text
H1: pass @ src/pricing.ts:41-58 sha=3f9a1c0e77b2    # sha256 of exactly those lines
H2: pass $ git diff main --stat sha=b81d0c4e55aa     # sha256 of that command's output
H3: fail tier badge missing on /pricing
```

`--ci` rehashes the file range or reruns the command. A mismatch means the grader did not read this version / did not run this command, and the pass is downgraded to fail. This does not prove the grader reasoned correctly; it proves it looked at the real artifact rather than answering from memory.

### 3.7 Machine-generated verdict and stop hook

`--report` derives the final report from the files on disk (`FREEZE`, `last-run.json`, `STATE.md`, `CI.md`). The agent pastes it; it must not hand-write `VERDICT:`. A `Stop` hook runs `--run` and blocks the agent from ending its turn while any gate is unmet — the one point where the host actually enforces anything.

### 3.8 Caps, stall, handoff

The checker tracks `iteration` and `stall` (consecutive runs with no change in the evidence vector). Reaching a cap produces `HANDOFF REQUIRED: <unmet ids>` — never a summary that reads like success. `ABANDON: G3 <reason>` is a handoff, not a pass.

## 4. Roles

| Role | Reads | Writes | May not |
|---|---|---|---|
| **Test-writer** | request, existing code (as spec) | tests, `LEDGER.md`, `PLAN.md`, runs `--red`, commits freeze | write production code |
| **Implementer** | ledger, tests, `evidence/*.out` | production code, `B` column of `STATE.md` | touch any frozen file; declare done |
| **LLM CI** | clean checkout only | `CI.md` | read implementer chat, PR summary, or checkboxes; change code or oracles |

If you cannot spawn isolated agents, run the roles as phases and reread only files and command output between them. That is a weak barrier; `--red`, the committed freeze, and the checker are the strong ones.

## 5. Usage

### 5.1 Install

```bash
# copy into your project (or add as a submodule)
cp -r scripts/nid_check.py <your-repo>/scripts/
cp SKILL.md <your-repo>/.claude/skills/no-illusory-done/SKILL.md   # Claude Code
```

Stop hook, `.claude/settings.json`:

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

### 5.2 When to use

Any of: the change touches ≥3 files; a new test is needed; the user gave a goal or todo list; the user said "until it's done". Skip for typos, one-liners, renames. Once a ledger exists the task may not be downgraded to "trivial".

### 5.3 Walkthrough

**Phase 1 — Test-writer**

```text
.no-illusory-done/PLAN.md
```
```markdown
R1: /pricing renders exactly three tiers
R2: annual toggle shows 20% discount to two decimals
R3: no new secrets in the diff

H1: tier order matches marketing spec (Basic, Pro, Team) | FALSIFIER: any other order or a fourth card is visible on /pricing

SETUP: npm ci
max_iterations: 8
stall_iters: 3
max_ci_attempts: 3
```

```text
.no-illusory-done/LEDGER.md
```
```markdown
- [ ] G1: pricing page renders three tier cards
  CHECK: npx vitest run tests/pricing.spec.ts && cat tests/nid/G1.marker
  EXPECT: NID G1
  FILES: tests/pricing.spec.ts, tests/nid/G1.marker
  COVERS: R1

- [ ] G2: annual toggle discount is 20.00%
  CHECK: npx vitest run tests/discount.spec.ts && cat tests/nid/G2.marker
  EXPECT: NID G2
  FILES: tests/discount.spec.ts, tests/nid/G2.marker
  COVERS: R2

- [ ] G3: diff introduces no token-shaped strings
  CHECK: git diff main -- . ':!*.lock' | grep -Ev '^-' | grep -Eq '(sk|ghp|AKIA)[A-Za-z0-9_-]{16,}' && exit 1 || cat tests/nid/G3.marker
  EXPECT: NID G3
  FILES: tests/nid/G3.marker
  COVERS: R3
```

```bash
python3 scripts/nid_check.py --status .no-illusory-done/LEDGER.md   # parses; checks R/H/COVERS rules
python3 scripts/nid_check.py --red    .no-illusory-done/LEDGER.md   # every gate must FAIL; writes FREEZE.sha256
git add .no-illusory-done scripts/nid_check.py tests && git commit -m "nid: freeze oracles"
```

**Phase 2 — Implementer** (one gate at a time)

```bash
# ...write code...
python3 scripts/nid_check.py --run .no-illusory-done/LEDGER.md
# G1: PASS exit=0 expect=True sha=a2de7723749f bytes=7
# G2: FAIL exit=1 expect=False sha=14ff040eae43 bytes=1832
# UNMET: G2
cat .no-illusory-done/evidence/G2.out      # paste the assertion text into the next turn, not "try harder"
```

Repeat until `ALL MET`. The checker updates the `E` column, `iteration`, and `stall` in `STATE.md`; the implementer may only edit `B` and notes. `ALL MET` is the claim, not acceptance.

**Phase 3 — LLM CI** (new worktree, empty conversation)

```bash
git worktree add ../nid-ci HEAD && cd ../nid-ci
npm ci                                                           # SETUP from PLAN.md
python3 scripts/nid_check.py --run .no-illusory-done/LEDGER.md   # Stage A; exit != 0 -> reject, stop
```

Stage B grades H1 with a pointer, then writes `CI.md`:

```text
CI: merge-ok
STAGE_A: pass
STAGE_B: pass
PROCESS: pass
OUTCOME: pass
UNMET: none
EVIDENCE:
H1: pass @ src/pages/pricing.tsx:22-40 sha=9c41d0b7e3aa
```

```bash
python3 scripts/nid_check.py --ci .no-illusory-done/CI.md   # rehashes the pointer; exit 0 only if merge-ok is consistent
python3 scripts/nid_check.py --report                       # paste verbatim; "done" only on VERDICT: merge-ok
```

### 5.4 Command reference

| Command | Does | Exit 0 iff |
|---|---|---|
| `--status LEDGER` | parse ledger + PLAN; traceability and H-line rules | well-formed |
| `--red LEDGER` | run all gates, require RED, write `FREEZE.sha256` | all required gates failed |
| `--freeze LEDGER` | rewrite file hashes (keeps RED lines) | — |
| `--verify-freeze` | working tree vs FREEZE, and FREEZE vs `git HEAD` | match |
| `--run LEDGER` | rerun all gates, write `evidence/`, update `STATE.md` | ALL MET (runnable gates) |
| `--ci CI.md` | parse verdict; verify every `pass` pointer; cross-check Stage A record and freeze | `CI: merge-ok` and consistent |
| `--report` | derive final report from disk | `VERDICT: merge-ok` |

Gate fields: `CHECK`, `EXPECT` (literal or `/regex/`, matched against the **last non-empty line** of stdout+stderr), `CWD` (default `.`), `TIMEOUT` (default 300 s), `RETRIES` (0–2; a pass on retry is flagged flaky), `FILES`, `KIND` (`cmd` | `llm-judge`), `RED` (`required` | `pass-ok`), `COVERS`.

### 5.5 Artifacts

```text
.no-illusory-done/
  PLAN.md            R clauses, H lines with FALSIFIER, SETUP, caps
  LEDGER.md          gates (frozen)
  FREEZE.sha256      file hashes + RED output hashes (checker-written, committed)
  STATE.md           E column checker-owned, B column implementer-owned, iteration/stall
  evidence/          Gn.out per gate, last-run.json
  CI.md              written only by the CI role, validated by --ci
```

## 6. What this does not solve

- **An agent that never loads the skill**, or deletes `.no-illusory-done/`. Git history shows it; nothing prevents it.
- **Vacuous tests that still claim coverage.** `--red` proves an oracle observes *something*; `COVERS:` proves every clause has *an* oracle. Neither proves the oracle observes the *right* thing. The planned next guard is `--mutate`: a good test must also fail on near-miss implementations.
- **A grader that reads the right file and reasons wrongly.** Pointer verification proves the LLM looked at the real artifact; it cannot prove the judgment. FALSIFIER lines shrink that surface; they do not remove it.
- **Auth, payments, production merges.** Human review and host CI remain the right bound there.

## 7. Lineage

Unlazy-style gates, Ralph-style fresh-context retry loops, information-barrier TDD, epistemic ledgers (belief ≠ evidence), outcome graders, and the process/outcome split from universal-verifier work. The contribution here is only the mechanical part: putting each of those into a checker with a fail-closed exit code.

## License

MIT
