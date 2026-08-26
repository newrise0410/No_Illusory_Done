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

Three principles drive everything else — and one consequence: **the checker treats nothing written by an LLM as evidence.** `--run`, `--ci`, `--mutate` and `--report` all re-execute the oracles in the same invocation. `STATE.md`, `evidence/` and `CI.md`'s own `STAGE_A:` line are outputs for humans, never inputs to a verdict.

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

Before any implementation, the test-writer runs every gate. **Each must fail.** A gate that passes before the code exists observes nothing about the code (or is a regression gate: `RED: pass-ok`, allowed only when its `FILES` were already tracked in git). `--red` is the only command that writes `FREEZE.sha256`; `--run` refuses any gate without a RED record. This proves the oracle *failed* at time zero — not that it failed for the right reason (see §6).

### 3.3 Freeze (replaces "human approves the tests")

`--red` writes `FREEZE.sha256` with the hashes of `LEDGER.md`, `PLAN.md`, every `FILES:` entry, **and the checker itself**. You commit it, once. From then on `--run`:

- refuses if any hashed file differs — checked **before and after** running the gates, so a CHECK that rewrites a frozen file mid-run is caught;
- refuses if `FREEZE.sha256` differs from `git HEAD`, or if more commits touched it than the `SUPERSEDE` lines it declares — a re-freeze must be declared with `--red --supersede "<reason>"` and stays in the file forever;
- refuses outside a git repository (no witness → no verdict);
- refuses if a CHECK names an existing file that is not in `FILES` (a path that does not exist yet is product output); `FILES` must be non-empty and inside the repo;
- runs every CHECK in a clean environment (PATH/HOME/LANG/TMPDIR only, `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1`; a gate's `ENV:` adds frozen literals, never PATH/PYTHONPATH/NODE_PATH/LD_PRELOAD), so nothing inherited from the implementer's shell reaches the oracle;
- refuses `--run` if a runner-influencing file (`conftest.py`, `sitecustomize.py`, `*.pth`, `pytest.ini`, `pyproject.toml`, `package.json`, jest/vitest/babel config, `tsconfig`, `.env`, `Makefile`, `__init__.py`, …) was added or changed since the freeze without being frozen;
- refuses more than `max_supersedes` declared re-freezes (default 1).

What git cannot witness — history rewrite, force-push — is outside the checker.

### 3.4 Traceability (R clauses and `COVERS:`)

`PLAN.md` decomposes the user request into atomic clauses `R1..Rn`. Every gate must declare which clauses it observes; every clause must be observed by at least one gate. This is checked by **id only**. It catches a requirement that reached no oracle at all; it cannot tell whether a gate that claims `COVERS: R2` actually observes R2. That is what `--mutate` is for.

### 3.5 Falsifiable HIGH-LEVEL outcomes (H lines)

Some outcomes genuinely cannot be a command (game rules hold, UI state is sensible, no credentials in the diff). These go in `PLAN.md` as `H1..Hn`, and each must name its **falsifier** — what observation would make it false:

```text
H1: no credentials in the diff | FALSIFIER: a string shaped like an API token appears in the diff | SUBJECT: src/auth.ts, $ git diff main --stat
```

If the falsifier is a command (contains `$`, `|`, `&&`, `./`, or starts with any executable on `PATH`), the checker refuses: that is a runnable gate, put it in the ledger. `SUBJECT` names the **exact** regular files and **exact** `$ commands` a CI pointer for this H may cite — directories, prefixes, symlinks out of the repo, `/dev/null`, `.no-illusory-done/` are all rejected. Vague phrases are refused.

### 3.6 Two-stage CI, with verifiable pointers

**Stage A** — a clean checkout runs `nid_check.py --run`. No LLM. Fail → reject, and the grader is never invoked.

**Stage B** — an LLM, in an empty conversation, grades only the H lines and `KIND: llm-judge` gates. A `pass` needs a pointer the checker can re-derive:

```text
H1: pass @ src/pricing.ts:41-58 sha=3f9a1c0e77b2    # sha256 of exactly those lines
H2: pass $ git diff main --stat sha=b81d0c4e55aa     # sha256 of that command's output
H3: fail tier badge missing on /pricing
```

`--ci` first re-runs Stage A and `--mutate` itself (it never trusts a recorded result), then rehashes each file range or reruns each command. The pointer must be exactly one of the H line's `SUBJECT` entries; the command must exit 0 and produce output; a gate that passed only on retry is a process failure. Any of these downgrades the verdict to reject. This proves the grader cited the real artifact in its current state — not that it reasoned correctly about it.

### 3.7 Machine-generated verdict and stop hook

`--report` re-runs Stage A and the `--ci` validation, then prints `VERDICT:`. The agent pastes it; it must not hand-write one. A `Stop` hook (`--hook`) re-runs the gates and blocks the agent from ending its turn while any is unmet — the one point where the host actually enforces anything.

### 3.8 Caps, stall, handoff

`--run` tracks `iteration` and `stall` (consecutive unmet runs with an unchanged evidence vector; reset on ALL MET) and **enforces** the caps from `PLAN.md`: while gates are unmet and a cap is hit it prints `HANDOFF REQUIRED: <ids>` and exits 3. `ABANDON: G3 <reason>` is a handoff, not a pass.

### 3.9 Mutation (`--mutate`)

`--red` proves an oracle fails without the code; `--mutate` proves it fails with *slightly wrong* code. For every python source file changed since the freeze, the checker generates AST mutants (comparison/arithmetic/boolean operator swaps, constant shifts, `if` negation, `return → return None`), applies each in a throwaway git worktree, and re-runs all gates. All mutants run (an optional `max_mutants_per_file` cap makes the result `inconclusive`, never `pass`). A mutant no gate kills is printed as `VACUOUS ORACLE` and the command fails. Zero mutants (no changed python, or python with nothing to mutate) is `inconclusive` — exit 1, shown in `--report`, not silently a pass. v1 is python-only.

## 4. Roles

| Role | Reads | Writes | May not |
|---|---|---|---|
| **Test-writer** | request, existing code (as spec) | tests, `LEDGER.md`, `PLAN.md`, runs `--red`, commits freeze | write production code |
| **Implementer** | ledger, tests, `evidence/*.out` | production code, `B` column of `STATE.md` | touch any frozen file; re-freeze; declare done |
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
        "command": "python3 scripts/nid_check.py --hook >/dev/null 2>&1 || { echo 'NID: unmet gates — see .no-illusory-done/evidence/'; exit 2; }"
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

H1: tier order matches marketing spec (Basic, Pro, Team) | FALSIFIER: any other order or a fourth card is visible on /pricing | SUBJECT: src/pages/pricing.tsx
max_supersedes: 1
max_gates_per_r: 4

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
  CHECK: git diff main -- . ':!*.lock' | grep -Ev '^-' | grep -Eq '(sk|ghp|AKIA)[A-Za-z0-9_-]{16,}' && exit 1; cat tests/nid/G3.marker
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
python3 scripts/nid_check.py --run .no-illusory-done/LEDGER.md      # Stage A; exit != 0 -> reject, stop
python3 scripts/nid_check.py --mutate .no-illusory-done/LEDGER.md   # survivors -> VACUOUS ORACLE -> reject
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
| `--status LEDGER` | parse ledger + PLAN; traceability, H-line, FILES/MUTABLE rules | well-formed |
| `--red LEDGER` | run all gates, require RED, write `FREEZE.sha256` (`--supersede "<reason>"` to re-freeze, recorded) | all required gates failed |
| `--verify-freeze` | hashes vs working tree; FREEZE vs HEAD; commit count vs declared supersedes; RED records present | match |
| `--run LEDGER` | freeze → run gates → freeze again; update `STATE.md`; enforce caps | ALL MET (exit 1 unmet, 3 handoff) |
| `--mutate LEDGER` | AST mutants of changed python, each must be killed by a gate | no survivors (0 mutants = inconclusive, exit 1) |
| `--ci CI.md` | re-run Stage A + mutation; validate pointers against exact SUBJECT; flaky = process fail | `CI: merge-ok` consistent |
| `--report` | re-run Stage A + CI validation; print verdict | `VERDICT: merge-ok` |
| `--hook` | Stop-hook entry (no ledger → exit 0; else `--run`) | ALL MET |

Gate fields: `CHECK` (run under `bash -o errexit -o pipefail -o nounset` in a clean env), `EXPECT` (literal ≥3 chars or a non-vacuous `/regex/`, **last non-empty line** of stdout+stderr), `CWD`, `TIMEOUT` (300 s), `RETRIES` (0–2; pass-on-retry = process fail in CI), `FILES` (frozen, non-empty), `ENV` (frozen literal `KEY=value` list), `KIND` (`cmd` | `llm-judge`), `RED` (`required` | `pass-ok`), `COVERS`.

### 5.5 Artifacts

```text
.no-illusory-done/
  PLAN.md            R clauses, H lines with FALSIFIER, SETUP, caps
  LEDGER.md          gates (frozen)
  FREEZE.sha256      file hashes + RED records + SUPERSEDE log (checker-written, committed once)
  STATE.md           E column checker-owned, B column implementer-owned — output only, never evidence
  evidence/          Gn.out per gate — output only
  CI.md              written only by the CI role, validated by --ci (which re-runs Stage A)
```

## 6. What this does not solve

- **An agent that never loads the skill**, or deletes `.no-illusory-done/`. Git history shows it; nothing prevents it.
- **Force-push.** With a remote configured, the freeze commit must be reachable from a remote ref, so a local history rewrite is caught; a force-push to the remote is not. Branch protection or a human is the bound. With no remote at all, the witness is local history only and `--report` says so.
- **A requirement omitted from R.** Traceability is by id; nothing checks that R1..Rn is the whole request.
- **A frozen oracle that reads what the implementer writes.** An oracle must read the product; a "product" that is just a pass flag (`test -f sentinel`, `{"pass": true}`) is mechanically indistinguishable from a real one. `--red` cannot tell "no implementation" from "no sentinel". For changed python, `--mutate` flags the gate as vacuous; for other languages it is `inconclusive` and a human must read the ledger.
- **`COVERS` semantics.** One gate claiming three Rs while testing one passes the id check; `max_gates_per_r` bounds dilution, mutation catches it for python, nothing else does.
- **Caps are advisory against an adversary.** Counters live in `STATE.md` and git refs (`refs/nid/*`, the larger wins); `git update-ref -d` resets them. They stop accidental loops, not deliberate ones.
- **`--supersede` reasons are free text.** The cap and the permanent log make re-freezes visible; they do not judge them.
- **A grader that reads the right file and reasons wrongly.** Pointers + SUBJECT prove access to the relevant artifact, not judgment.
- **A CHECK that deliberately daemonizes.** TIMEOUT kills the process group; a child that calls `setsid` and ignores SIGHUP survives. It lives in a frozen, repo-owned script, so it is reviewable, not preventable.
- **Auth, payments, production merges.** Human review and host CI remain the right bound there.

## 7. Lineage

Unlazy-style gates, Ralph-style fresh-context retry loops, information-barrier TDD, epistemic ledgers (belief ≠ evidence), outcome graders, and the process/outcome split from universal-verifier work. The contribution here is only the mechanical part: putting each of those into a checker with a fail-closed exit code.

## License

MIT
