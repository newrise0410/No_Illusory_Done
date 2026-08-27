# No Illusory Done

[![checker](https://github.com/newrise0410/No_Illusory_Done/actions/workflows/ci.yml/badge.svg)](https://github.com/newrise0410/No_Illusory_Done/actions/workflows/ci.yml)

**Done is a checker verdict, not a sentence.**

A completion-discipline skill for coding agents (Claude Code, Codex, or any agent that can run shell commands), plus a single-file Python checker (stdlib only, needs `git` and `bash`) that makes "done" a state on disk rather than a claim in chat.

| | |
|---|---|
| [`SKILL.md`](SKILL.md) | the skill: roles, artifacts, gate contract, stop hook |
| [`scripts/nid_check.py`](scripts/nid_check.py) | the checker: `--status`, `--red`, `--run`, `--mutate`, `--ci`, `--report`, `--verify-freeze`, `--hook` |
| [`tests/test_nid_check.py`](tests/test_nid_check.py) | 54 cases against real git fixtures: happy path, every refusal rule, every bypass from six adversarial review rounds, every doc example |
| [`.no-illusory-done/`](.no-illusory-done/) | this repository's own frozen ledger — CI dogfoods the checker on every push |

**Quick start** (lite mode, the default):

```bash
cp scripts/nid_check.py <repo>/scripts/
# write .no-illusory-done/PLAN.md (R clauses, PRODUCT) and LEDGER.md (gates), plus failing tests — see §5.3
python3 scripts/nid_check.py --red .no-illusory-done/LEDGER.md    # every gate must fail; writes FREEZE.sha256
git add -A && git commit -m "nid: freeze" && git push               # the freeze needs a remote witness
# ...implement...
python3 scripts/nid_check.py --run .no-illusory-done/LEDGER.md    # ALL MET is the claim
python3 scripts/nid_check.py --report                             # VERDICT: merge-ok is the only "done"
```

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

The checker refuses gates that observe nothing: titles that are activities ("run tests"), `exit 0`, `--passWithNoTests`, redirection, a CHECK that contains its own EXPECT — and, under `strictness: strict` (§3.10), the whole shell-syntax class (`echo`, `||`, `$`, control flow). The blacklist is a convenience; the real guards are the next mechanisms.

### 3.2 RED before code (`--red`)

Before any implementation, the test-writer runs every gate. **Each must fail.** A gate that passes before the code exists observes nothing about the code (or is a regression gate: `RED: pass-ok`, allowed only when its `FILES` were already tracked in git). `--red` is the only command that writes `FREEZE.sha256`; `--run` refuses any gate without a RED record. This proves the oracle *failed* at time zero — not that it failed for the right reason (see §7).

### 3.3 Freeze (replaces "human approves the tests")

`--red` writes `FREEZE.sha256` with the hashes of `LEDGER.md`, `PLAN.md`, every `FILES:` entry, **and the checker itself**. You commit it, once. From then on `--run`:

- refuses if any hashed file differs — checked **before and after** running the gates, so a CHECK that rewrites a frozen file mid-run is caught;
- refuses if `FREEZE.sha256` differs from `git HEAD`, or if more commits touched it than the `SUPERSEDE` lines it declares — a re-freeze must be declared with `--red --supersede "<reason>"` and stays in the file forever;
- refuses outside a git repository (no witness → no verdict);
- refuses if a CHECK names an existing non-PRODUCT file that is not in `FILES` (PRODUCT files may be read with `grep`/`diff` but never passed to an interpreter — including via `-r`/`-S`/preload options; a path that does not exist yet is product output); `FILES` must be non-empty and inside the repo; the ledger must be `<git toplevel>/.no-illusory-done/LEDGER.md` (no alternate ledgers); submodules and nested repos are refused;
- refuses if any file **outside `PRODUCT:`** (declared in the frozen `PLAN.md`) changed since the freeze — a loader hook, a post-freeze symlink, a `bin/`, a test helper — the implementer may write only the product (`EXPECTED_NEW:` for product files that carry a runner-config name);
- refuses if any changed PRODUCT file contains a gate's EXPECT marker — the marker is the oracle's word, never the product's;
- runs every CHECK in a clean environment: PATH with relative and repo-internal entries removed, HOME/LANG/TMPDIR, toolchain homes only when outside the repo, `PYTHONNOUSERSITE=1`, npm/pip/git user config at `/dev/null`; a gate's `ENV:` adds frozen literals, never PATH/PYTHONPATH/NODE_PATH/LD_PRELOAD. "Clean" means *the implementer's shell cannot reach the oracle*, not hermetic — a toolchain inside the checkout will not be found;
- refuses `--run` if a runner-influencing file (`conftest.py`, `sitecustomize.py`, `*.pth`, `pytest.ini`, `pyproject.toml`, `package.json`, jest/vitest/babel config, `tsconfig`, `.env`, `Makefile`, `__init__.py`, …) was added or changed since the freeze without being frozen or declared in `PLAN.md` `EXPECTED_NEW:`;
- refuses more than `max_supersedes` declared re-freezes (default 3);
- with a remote configured, refuses unless the freeze commit is reachable from a ref the remote *actually* reports (`git ls-remote`; local tracking refs, `git replace`, shallow clones and grafts are all ignored or refused). `witness: local` opts out for offline work.

What the remote cannot witness — a force-push to the remote itself — is outside the checker.

### 3.4 Traceability (R clauses and `COVERS:`)

`PLAN.md` decomposes the user request into atomic clauses `R1..Rn`. Every gate must declare which clauses it observes; every clause must be observed by at least one gate. This is checked by **id only**. It catches a requirement that reached no oracle at all; it cannot tell whether a gate that claims `COVERS: R2` actually observes R2. That is what `--mutate` is for.

### 3.5 Falsifiable HIGH-LEVEL outcomes (H lines)

Some outcomes genuinely cannot be a command (game rules hold, UI state is sensible, no credentials in the diff). These go in `PLAN.md` as `H1..Hn`, and each must name its **falsifier** — what observation would make it false:

```text
H1: no credentials in the diff | FALSIFIER: a string shaped like an API token appears in the diff | SUBJECT: src/auth.ts, $ git diff main --stat
```

If the falsifier is a command (contains `$`, `|`, `&&`, `./`, or starts with any executable on `PATH`), the checker refuses: that is a runnable gate, put it in the ledger. `SUBJECT` names the **exact** regular files and **exact** `$ commands` a CI pointer for this H may cite — directories, prefixes, symlinks out of the repo, `/dev/null`, `.no-illusory-done/`, and prose/data files (`.md/.txt/.rst/.html/.json/.yaml/.toml/.csv/.xml`) are all rejected — cite code, or a `$ command` that observes behaviour, not a document claiming it. Vague phrases are refused.

### 3.6 Two-stage CI, with verifiable pointers

**Stage A** — a clean checkout runs `nid_check.py --run`. No LLM. Fail → reject, and the grader is never invoked.

**Stage B** — an LLM, in an empty conversation, grades only the H lines and `KIND: llm-judge` gates. A `pass` needs a pointer the checker can re-derive:

```text
H1: pass @ src/pricing.ts:41-58 sha=3f9a1c0e77b2    # sha256 of exactly those lines
H2: pass $ git diff main --stat sha=b81d0c4e55aa     # sha256 of that command's output
H3: fail tier badge missing on /pricing
```

`--ci` first re-runs Stage A and `--mutate` itself (it never trusts a recorded result), then rehashes each file range or reruns each command. The pointer must be exactly one of the H line's `SUBJECT` entries, must be a non-frozen file changed since the freeze, and a command must exit 0, produce output and change nothing (the repo is snapshotted before and after). Any of these downgrades the verdict to reject. `--ci` also runs the plan's `SETUP:` lines first (which may only touch gitignored paths) and counts rejected attempts against `max_ci_attempts`. This proves the grader cited the real artifact in its current state — not that it reasoned correctly about it.

### 3.7 Machine-generated verdict and stop hook

`--report` re-runs Stage A and the `--ci` validation, then prints `VERDICT:`. The agent pastes it; it must not hand-write one. A `Stop` hook (`--hook`) re-runs the gates and blocks the agent from ending its turn while any is unmet — the one point where the host actually enforces anything.

### 3.8 Caps, stall, handoff

`--run` tracks `iteration` and `stall` (consecutive unmet runs with an unchanged evidence vector; reset on ALL MET) and **enforces** the caps from `PLAN.md`: while gates are unmet and a cap is hit it prints `HANDOFF REQUIRED: <ids>` and exits 3. `ABANDON: G3 <reason>` is a handoff, not a pass.

### 3.9 Mutation (`--mutate`)

`--red` proves an oracle fails without the code; `--mutate` proves it fails with *slightly wrong* code. For every python source file changed since the freeze, the checker generates AST mutants (comparison/arithmetic/boolean operator swaps, constant shifts, `if` negation, `return → return None`), applies each in a throwaway git worktree, and re-runs all gates. All mutants run (an optional `max_mutants_per_file` cap makes the result `inconclusive`, never `pass`). In `--ci`, an inconclusive mutation yields `CI: inconclusive`; `mutation_required: 0` in the frozen `PLAN.md` waives only the "no python changed" case, never a capped or node-less run. A mutant counts as killed only when a gate failed **with an assertion** (`AssertionError`, `FAILED`, `expected`, `not ok`…); a mutant that merely crashes the module is a survivor. A mutant no gate kills is printed as `VACUOUS ORACLE` and the command fails. Zero mutants (no changed python, or python with nothing to mutate) is `inconclusive` — exit 1, shown in `--report`, not silently a pass. v1 is python-only.

### 3.10 Modes: lite (default) and strict

Every guard above has a cost in false refusals. `PLAN.md` chooses, at freeze time:

| | `strictness: lite` (default) | `strictness: strict` |
|---|---|---|
| Freeze, RED, `PRODUCT` scope, remote witness, product-execution ban, influence files, caps | on | on |
| Shell-syntax bans in CHECK (`$`, `||`, `echo`, control flow, nested shells, heredocs) | **off** — only `exit 0`, `passWithNoTests`, redirection and file-mutating commands are refused | on |
| Mutation required for `merge-ok` | off (`mutation_required: 0`) | on unless the plan waives it |

Lite keeps everything that is cheap and structural; strict additionally closes the shell-syntax bypass classes. What lite actually reopens is narrower than "any shell": a `||` fallback, an `if/else` branch or a nested `sh -c` can only satisfy a gate if *something* prints the marker after the real test fails — and `--red` refuses gates that are green before implementation, `echo`ing the marker trips the self-fulfilling rule, and **no PRODUCT file may contain a gate's marker** (checked after every run), so `cat src/x` cannot be the fallback either. The residue is the sentinel class (§7), which strict does not close either. Use strict for CI-gated work; use lite when the shell rules keep refusing legitimate gates.

Other frozen switches: `witness: local` skips the remote query (offline work; the witness is then local history only, and `--report` says so); `regression_only: 1` allows a ledger where every gate is `RED: pass-ok` (nothing proves *new* behaviour — for characterising existing code); `MUTATE: <cmd>` + `MUTATE_EXPECT: <marker>` delegates mutation to an external tool (Stryker, mutmut, cargo-mutants) — the command must exit 0 and print the marker last, so wrap the tool in a repo-owned script that checks the score; `max_supersedes` defaults to 3.

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
PRODUCT: src
mutation_required: 0     # TypeScript project: python mutation cannot apply

H1: tier order matches marketing spec (Basic, Pro, Team) | FALSIFIER: any other order or a fourth card is visible on /pricing | SUBJECT: src/pages/pricing.tsx

SETUP: npm ci            # run by --ci in the clean env; may only write gitignored paths
strictness: strict       # this is CI-gated work; see §3.10 for what lite drops
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
  CHECK: git diff main -- src | grep -Ev '^-' | grep -Eq '(sk|ghp|AKIA)\w{16,}' && exit 1; cat tests/nid/G3.marker
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
python3 scripts/nid_check.py --run .no-illusory-done/LEDGER.md      # Stage A preview; exit != 0 -> reject, stop
python3 scripts/nid_check.py --mutate .no-illusory-done/LEDGER.md   # survivors -> VACUOUS ORACLE -> reject
```

(`--ci` below repeats both itself, after running `SETUP:`; nothing recorded here is trusted.) Stage B grades H1 with a pointer, then writes `CI.md`:

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
| `--status LEDGER` | parse ledger + PLAN; traceability, H-line, FILES/PRODUCT rules | well-formed |
| `--red LEDGER` | run all gates, require RED, write `FREEZE.sha256` (`--supersede "<reason>"` to re-freeze, recorded) | all required gates failed |
| `--verify-freeze` | hashes vs working tree; FREEZE vs HEAD; commit count vs declared supersedes; RED records present | match |
| `--run LEDGER` | freeze → run gates → freeze again; update `STATE.md`; enforce caps | ALL MET (exit 1 unmet, 3 handoff) |
| `--mutate LEDGER` | AST mutants of changed python (or the plan's `MUTATE:` tool), each must be killed by an assertion | no survivors (0 mutants = inconclusive, exit 1) |
| `--ci CI.md` | run `SETUP`, re-run Stage A + mutation; validate pointers against exact SUBJECT; count attempts | `CI: merge-ok` consistent |
| `--report` | re-run Stage A + CI validation; print verdict | `VERDICT: merge-ok` |
| `--hook` | Stop-hook entry (no ledger or no freeze yet → exit 0; else `--run`, exit 2 blocks) | ALL MET |

Gate fields: `CHECK` (run under `bash -o errexit -o pipefail -o nounset` in a clean env), `EXPECT` (literal ≥3 chars or a `/regex/` with ≥3 literal alphanumerics that matches none of the failure probes, **last non-empty line** of stdout+stderr), `CWD`, `TIMEOUT` (300 s), `FILES` (frozen, non-empty), `ENV` (frozen literal `KEY=value` list), `KIND` (`cmd` | `llm-judge`; judges may not outnumber runnable gates), `RED` (`required` | `pass-ok`), `COVERS` (every R needs a runnable gate). No `RETRIES`: a flaky oracle is refused.

### 5.5 PLAN.md reference

| Line | Meaning | Default |
|---|---|---|
| `Rn: <clause>` | atomic requirement; every R needs a runnable gate | required |
| `Hn: <state> \| FALSIFIER: <prose> \| SUBJECT: <code file>, $ <command>` | outcome graded by LLM CI; pointer targets | optional |
| `PRODUCT: <paths>` | the only paths the implementer may change after the freeze | required |
| `EXPECTED_NEW: <files>` | product files with runner-config names the implementation will create | — |
| `SETUP: <cmd>` | run by `--ci` before Stage A; may only write gitignored paths | — |
| `strictness: lite\|strict` | shell-syntax bans + mutation requirement | `lite` |
| `witness: remote\|local` | query the remote for the freeze commit, or trust local history | `remote` |
| `regression_only: 0\|1` | allow a ledger where every gate is `RED: pass-ok` | `0` |
| `mutation_required: 0\|1` | waive the "no python changed" case only | `0` (lite) / `1` (strict) |
| `MUTATE: <cmd>` + `MUTATE_EXPECT: <marker>` | external mutation tool as the verdict | — |
| `max_iterations` / `stall_iters` | implementer caps → `HANDOFF REQUIRED` (exit 3) | 8 / 3 |
| `max_ci_attempts` | rejected `--ci` runs before handoff | 3 |
| `max_supersedes` | declared re-freezes before a human must intervene | 3 |
| `max_gates_per_r` | traceability dilution bound | 4 |
| `max_mutants_per_file` | 0 = all; a cap makes mutation `inconclusive`, never `pass` | 0 |

### 5.6 Artifacts

```text
.no-illusory-done/
  PLAN.md            R/H lines, PRODUCT, SETUP, modes, caps (frozen)
  LEDGER.md          gates (frozen)
  FREEZE.sha256      file hashes + RED records + SUPERSEDE log (checker-written, committed once)
  STATE.md           E column checker-owned, B column implementer-owned — output only, never evidence
  evidence/          Gn.out per gate — output only
  CI.md              written only by the CI role, validated by --ci (which re-runs Stage A)
```

## 6. Testing the checker

`python3 -m unittest discover -s tests -v` — 61 cases in four layers against real git fixture repos (temp dir, bare remote, no mocks):

1. **happy path** in lite and strict: `status → hook → red → run(RED) → run(GREEN) → mutate → ci → report → hook`, plus caps, supersede, `EXPECTED_NEW`/`SETUP`, `regression_only`, the external `MUTATE:` hook;
2. **one case per refusal rule**, asserting the exact refusal text;
3. **red team**: every bypass found in seven adversarial review rounds (forged evidence, mid-run freeze mutation, `CI.md` written by product code, re-hash-and-commit, shallow clone, `git replace`, forged tracking refs, env/`PYTHONPATH`, repo-internal `PATH`, influence files, loader hooks, symlinks, ignored python, alternate ledgers, `STATE.md` forgery, mutating pointer commands, product-writing `SETUP`, control-flow masking, llm-judge ratios, output cap, path-prefix confusion) — each must stay refused;
4. **documentation and coverage**: every `CHECK:` in this README and SKILL.md is run through the checker's own rules, the walkthrough's PLAN/LEDGER blocks are parsed with `--status`, and the `errexit` semantics the negative-check example relies on are executed in bash. This layer caught a README example that the checker itself refused. A round-7 audit also mutated five guards in a copy of the checker to see which tests noticed; two did not (another guard fired first), and those fixtures were moved inside `PRODUCT` so only the named guard stands between them and a pass.

GitHub Actions runs the suite on Ubuntu and macOS (Python 3.11/3.12), then **dogfoods**: this repository carries its own `.no-illusory-done/` ledger (`PRODUCT: scripts`, strict, regression-only, mutation waived explicitly in the frozen plan), and CI runs `--verify-freeze` (witnessed by `origin`) and `--run` on every push. The friction is real and intended: any change outside `PRODUCT` — this README included — fails the dogfood job until the ledger is re-frozen with `--red --supersede "<reason>"`, which is recorded permanently.

## 7. What this does not solve

- **An agent that never loads the skill**, or deletes `.no-illusory-done/`. Git history shows it; nothing prevents it.
- **Force-push.** With a remote configured, the freeze commit must be reachable from a ref the remote *actually* reports (`git ls-remote`, not local tracking refs), so a local history rewrite, a forged tracking ref, or an unpushed freeze is caught; a force-push to the remote is not. Branch protection or a human is the bound. With no remote at all, the witness is local history only and `--report` says so. Remotes are queried live (30 s timeout each); one reachable remote holding the freeze commit suffices, and if none is reachable the verdict fails closed — **verification needs network access to the remote**.
- **A requirement omitted from R.** Traceability is by id; nothing checks that R1..Rn is the whole request.
- **A product that decoys.** An implementation that reads its own config to pick a module returning the expected value satisfies the oracle observably; a stateful oracle that passes on its Nth call is a sentinel variant. Mutation (python) or a human tells a real implementation from a decoy.
- **A frozen test that asks the product whether it is correct** (`assert util.check()`, where `util` is in PRODUCT). The implementer writes `check = lambda: True`. Assertions must be on observable behaviour; the checker cannot distinguish a verdict-helper from a real one.
- **A frozen oracle that reads what the implementer writes.** An oracle must read the product; a "product" that is just a pass flag (`test -f sentinel`, `{"pass": true}`) is mechanically indistinguishable from a real one. `--red` cannot tell "no implementation" from "no sentinel". For changed python, `--mutate` flags the gate as vacuous; for other languages it is `inconclusive` and a human must read the ledger.
- **`COVERS` semantics.** One gate claiming three Rs while testing one passes the id check; `max_gates_per_r` bounds dilution, mutation catches it for python, nothing else does.
- **Caps are advisory against an adversary.** Counters live in `STATE.md` and git refs (`refs/nid/*`, the larger wins); `git update-ref -d` resets them. They stop accidental loops, not deliberate ones.
- **`--supersede` reasons are free text.** The cap and the permanent log make re-freezes visible; they do not judge them.
- **A grader that reads the right file and reasons wrongly.** Pointers + SUBJECT prove access to the relevant artifact, not judgment.
- **A CHECK that deliberately daemonizes.** TIMEOUT kills the process group; a child that calls `setsid` and ignores SIGHUP survives. It lives in a frozen, repo-owned script, so it is reviewable, not preventable.
- **Auth, payments, production merges.** Human review and host CI remain the right bound there.

## 8. Lineage

Unlazy-style gates, Ralph-style fresh-context retry loops, information-barrier TDD, epistemic ledgers (belief ≠ evidence), outcome graders, and the process/outcome split from universal-verifier work. The contribution here is only the mechanical part: putting each of those into a checker with a fail-closed exit code.

## License

MIT
