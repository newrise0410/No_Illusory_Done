"""Test suite for scripts/nid_check.py.

Three layers, all against real git fixture repos in a temp dir (no mocking):
  1. happy path          — lite and strict, red → run → mutate → ci → report → hook
  2. refusal rules       — one case per rule, asserting the exact refusal text
  3. red-team            — bypass attempts found by adversarial review rounds 1–6; each must be refused
  4. documentation       — every CHECK/PLAN example in README.md and SKILL.md must pass the checker's own
                           rules, and the errexit semantics the examples rely on must hold in bash.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "nid_check.py"
PY = sys.executable

IMPL = "def discount(price, annual):\n    if annual:\n        return price * 0.8\n    return float(price)\n"
TEST = ("import sys; sys.path.insert(0, 'src')\nfrom calc import discount\n"
        "assert discount(100, True) == 80.0\nassert discount(100, False) == 100.0\nassert discount(0, True) == 0.0\n"
        "print('NID G1')\n")
PLAN = ("R1: discount(price, annual) returns 20 percent off when annual, else price\n"
        "H1: no credential-shaped strings in calc | FALSIFIER: a token like sk-XXXX appears in the calc module"
        " | SUBJECT: src/calc.py, $ git diff HEAD --stat\n"
        "PRODUCT: src\nmax_iterations: 3\nstall_iters: 2\n")
LEDGER = ("- [ ] G1: annual discount is exactly 20 percent\n  CHECK: python3 tests/test_calc.py\n"
          "  EXPECT: NID G1\n  FILES: tests/test_calc.py\n  COVERS: R1\n")


def sha(p: Path, n=16) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:n]


class Fixture:
    """A git repo with a bare remote, the checker copied in, and a minimal ledger/plan."""

    def __init__(self, plan=PLAN, ledger=LEDGER, test=TEST, extra_plan="", strict=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="nid-test-"))
        self.remote = self.tmp / "remote.git"
        self.root = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        for d in ("scripts", ".no-illusory-done", "tests", "src"):
            (self.root / d).mkdir(parents=True)
        shutil.copy2(CHECKER, self.root / "scripts" / "nid_check.py")
        self.w("tests/test_calc.py", test)
        self.w(".no-illusory-done/PLAN.md", plan + ("strictness: strict\n" if strict else "") + extra_plan)
        self.w(".no-illusory-done/LEDGER.md", ledger)
        self.w("src/calc.py", "# placeholder\n")
        self.git("init", "-q")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.git("remote", "add", "origin", str(self.remote))
        self.commit("base")

    # -- helpers
    def w(self, rel, content):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=check)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)

    def push(self):
        self.git("push", "-q", "origin", "HEAD")

    def nid(self, *args, env=None, cwd=None):
        e = {**os.environ, **(env or {})}
        return subprocess.run([PY, str(self.root / "scripts" / "nid_check.py"), *args],
                              cwd=str(cwd or self.root), capture_output=True, text=True, env=e)

    def status(self):
        return self.nid("--status", ".no-illusory-done/LEDGER.md")

    def red(self, *extra):
        return self.nid("--red", ".no-illusory-done/LEDGER.md", *extra)

    def run(self):
        return self.nid("--run", ".no-illusory-done/LEDGER.md")

    def freeze(self):
        r = self.red()
        assert r.returncode == 0, r.stdout + r.stderr
        self.commit("freeze")
        self.push()
        return r

    def implement(self):
        self.w("src/calc.py", IMPL)

    def ci_md(self, pointer=None, verdict="merge-ok"):
        ptr = pointer or f"H1: pass @ src/calc.py sha={sha(self.root / 'src/calc.py')}"
        self.w(".no-illusory-done/CI.md",
               f"CI: {verdict}\nSTAGE_A: pass\nSTAGE_B: pass\nPROCESS: pass\nOUTCOME: pass\nUNMET: none\nEVIDENCE:\n{ptr}\n")
        return self.nid("--ci", ".no-illusory-done/CI.md")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def out(r):
    return r.stdout + r.stderr


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = None

    def tearDown(self):
        if self.fx:
            self.fx.cleanup()

    def fixture(self, **kw):
        self.fx = Fixture(**kw)
        return self.fx

    def assertRefused(self, r, needle, msg=None):
        self.assertNotEqual(r.returncode, 0, msg or out(r))
        self.assertIn(needle, out(r), msg or out(r))

    def assertOk(self, r, needle=None):
        self.assertEqual(r.returncode, 0, out(r))
        if needle:
            self.assertIn(needle, out(r), out(r))


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------
class HappyPath(Base):
    def _flow(self, strict):
        fx = self.fixture(strict=strict)
        self.assertOk(fx.status(), "OK: 1 gates")
        self.assertEqual(fx.nid("--hook").returncode, 0, "hook must allow stopping before the freeze")
        fx.freeze()
        r = fx.run()
        self.assertEqual(r.returncode, 1); self.assertIn("UNMET: G1", r.stdout)
        fx.implement()
        self.assertOk(fx.run(), "ALL MET")
        self.assertOk(fx.nid("--mutate", ".no-illusory-done/LEDGER.md"), "MUTATION: pass")
        self.assertOk(fx.ci_md(), "CI: merge-ok")
        rep = fx.nid("--report")
        self.assertOk(rep, "VERDICT: merge-ok")
        self.assertIn("MUTATION: pass", rep.stdout)
        self.assertEqual(fx.nid("--hook").returncode, 0)
        self.assertEqual(fx.nid("--hook", cwd=fx.root / "src").returncode, 0, "hook from a subdirectory")

    def test_lite_flow(self):
        self._flow(strict=False)

    def test_strict_flow(self):
        self._flow(strict=True)

    def test_hook_without_ledger_exits_zero(self):
        fx = self.fixture()
        self.assertEqual(fx.nid("--hook", cwd=fx.tmp).returncode, 0)

    def test_caps_handoff_exit_3_survives_state_reset(self):
        fx = self.fixture()
        fx.freeze()
        codes = []
        for _ in range(3):
            (fx.root / ".no-illusory-done/STATE.md").unlink(missing_ok=True)
            codes.append(fx.run().returncode)
        self.assertEqual(codes, [1, 1, 3])

    def test_report_without_ci_is_not_verified(self):
        fx = self.fixture()
        fx.freeze(); fx.implement(); fx.run()
        r = fx.nid("--report")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("VERDICT: not-verified", r.stdout)

    def test_expected_new_and_setup(self):
        fx = self.fixture(extra_plan="EXPECTED_NEW: package.json\nSETUP: mkdir -p deps\n")
        fx.w(".gitignore", "deps/\n"); fx.commit("ignore")
        fx.freeze(); fx.implement()
        fx.w("package.json", "{}\n")
        self.assertOk(fx.run(), "ALL MET")
        self.assertOk(fx.ci_md(), "CI: merge-ok")

    def test_supersede_path(self):
        fx = self.fixture()
        fx.freeze()
        r = fx.red()
        self.assertRefused(r, "Re-freezing requires --supersede")
        r = fx.red("--supersede", "ledger-defect: marker name changed")
        self.assertOk(r)
        fx.commit("refreeze"); fx.push()
        self.assertOk(fx.nid("--verify-freeze"), "FREEZE: match")

    def test_regression_only_ledger(self):
        fx = self.fixture(extra_plan="regression_only: 1\n")
        fx.w("src/calc.py", IMPL); fx.commit("impl")
        led = LEDGER.replace("  COVERS: R1\n", "  COVERS: R1\n  RED: pass-ok\n")
        fx.w(".no-illusory-done/LEDGER.md", led)
        self.assertOk(fx.status())
        self.assertOk(fx.red())


# ---------------------------------------------------------------------------
# 2. Refusal rules (one case per rule)
# ---------------------------------------------------------------------------
LEDGER_CASES = [  # (name, ledger-transform, needle, strict?)
    ("activity_title", lambda L: L.replace("G1: annual discount is exactly 20 percent", "G1: run the tests"), "activity, not a state", False),
    ("missing_covers", lambda L: L.replace("  COVERS: R1\n", ""), "missing COVERS", False),
    ("unknown_r", lambda L: L.replace("COVERS: R1", "COVERS: R9"), "COVERS unknown requirement", False),
    ("empty_files", lambda L: L.replace("  FILES: tests/test_calc.py\n", ""), "FILES is empty", False),
    ("exit_zero", lambda L: L.replace("CHECK: python3 tests/test_calc.py", "CHECK: python3 tests/test_calc.py && exit 0"), "exit 0", False),
    ("pass_with_no_tests", lambda L: L.replace("python3 tests/test_calc.py", "npx jest --passWithNoTests"), "skip/soften", False),
    ("redirect", lambda L: L.replace("python3 tests/test_calc.py", ">/tmp/x; python3 tests/test_calc.py"), "redirection", False),
    ("echo_strict", lambda L: L.replace("python3 tests/test_calc.py", "echo NID"), "shell no-op", True),
    ("or_fallback_strict", lambda L: L.replace("python3 tests/test_calc.py", "python3 tests/test_calc.py || cat marker"), "'||' fallback", True),
    ("control_flow_strict", lambda L: L.replace("python3 tests/test_calc.py", "if python3 tests/test_calc.py; then cat m; else cat m; fi"), "control flow", True),
    ("expansion_strict", lambda L: L.replace("python3 tests/test_calc.py", "X=py; ${X}thon3 tests/test_calc.py"), "expansion", True),
    ("nested_shell_strict", lambda L: L.replace("python3 tests/test_calc.py", "sh -c 'python3 tests/test_calc.py'"), "nested shell", True),
    ("check_contains_expect", lambda L: L.replace("python3 tests/test_calc.py", "python3 tests/test_calc.py NID G1"), "self-fulfilling", False),
    ("vacuous_regex", lambda L: L.replace("EXPECT: NID G1", "EXPECT: /.*/"), "vacuous", False),
    ("regex_no_literal", lambda L: L.replace("EXPECT: NID G1", "EXPECT: /^.{16,}$/"), "vacuous", False),
    ("expect_too_short", lambda L: L.replace("EXPECT: NID G1", "EXPECT: ok"), "too short", False),
    ("retries", lambda L: L + "  RETRIES: 2\n", "RETRIES is not supported", False),
    ("timeout_cap", lambda L: L + "  TIMEOUT: 100000\n", "TIMEOUT must be 1..3600", False),
    ("env_pythonpath", lambda L: L + "  ENV: PYTHONPATH=/tmp/x\n", "ENV entry not allowed", False),
    ("gate_whitespace", lambda L: L.replace("- [ ] G1", "-  [ ] G1"), "not exactly '- [ ] Gn: title'", False),
    ("fullwidth_id", lambda L: L.replace("G1:", "G１:"), "fullwidth", False),
    ("llm_judge_only", lambda L: "- [ ] G1: judged\n  KIND: llm-judge\n  COVERS: R1\n", "at least one runnable gate", False),
    ("exec_product", lambda L: L.replace("python3 tests/test_calc.py", "python3 src/calc.py"), "product path", False),
    ("exec_product_module", lambda L: L.replace("python3 tests/test_calc.py", "python3 -m src.calc"), "product path", False),
    ("exec_product_preload", lambda L: L.replace("python3 tests/test_calc.py", "python3 -S src/calc.py"), "product path", False),
    ("glob", lambda L: L.replace("python3 tests/test_calc.py", "python3 tests/*.py"), "globs/tilde", False),
]


class RefusalRules(Base):
    def test_ledger_rules(self):
        for name, tf, needle, strict in LEDGER_CASES:
            with self.subTest(rule=name):
                fx = Fixture(ledger=tf(LEDGER), strict=strict)
                try:
                    self.assertRefused(fx.status(), needle, f"{name}: {out(fx.status())}")
                finally:
                    fx.cleanup()

    def test_lite_allows_shell_syntax_strict_refuses(self):
        led = LEDGER.replace("python3 tests/test_calc.py", "python3 tests/test_calc.py || cat tests/test_calc.py")
        fx = Fixture(ledger=led, strict=False)
        try:
            self.assertOk(fx.status())
        finally:
            fx.cleanup()

    def test_plan_rules(self):
        cases = [
            ("no_product", PLAN.replace("PRODUCT: src\n", ""), "must declare PRODUCT"),
            ("no_r", PLAN.replace("R1:", "X1:"), "no R1.. requirement"),
            ("h_no_falsifier", PLAN.replace(" | FALSIFIER: a token like sk-XXXX appears in the calc module", ""), "missing '| FALSIFIER"),
            ("h_no_subject", PLAN.replace(" | SUBJECT: src/calc.py, $ git diff HEAD --stat", ""), "missing '| SUBJECT"),
            ("falsifier_is_command", PLAN.replace("a token like sk-XXXX appears in the calc module", "grep -r sk- src"), "FALSIFIER is a command"),
            ("falsifier_one_word_exe", PLAN.replace("a token like sk-XXXX appears in the calc module", "hostname"), "FALSIFIER is a command"),
            ("subject_dir", PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: src/"), "must be an existing regular file"),
            ("subject_constant_cmd", PLAN.replace("$ git diff HEAD --stat", "$ git log -1 --format=%H"), "does not depend on the change"),
            ("subject_prose", PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: README.md"), "prose/data file"),
            ("vague_phrase", PLAN.replace("returns 20 percent off when annual, else price", "works correctly as expected"), "too vague"),
            ("zero_width_id", PLAN.replace("R1:", "R1​:"), "invisible/format character"),
            ("bad_strictness", PLAN + "strictness: medium\n", "strictness must be"),
            ("mutate_without_expect", PLAN + "MUTATE: npx stryker run\n", "must be given together"),
        ]
        for name, plan, needle in cases:
            with self.subTest(rule=name):
                fx = Fixture(plan=plan)
                try:
                    fx.w("README.md", "# doc\n")
                    self.assertRefused(fx.status(), needle, f"{name}: {out(fx.status())}")
                finally:
                    fx.cleanup()

    def test_cjk_clause_accepted_fullwidth_punct_in_prose_ok(self):
        fx = self.fixture(plan=PLAN.replace("R1: discount(price, annual) returns 20 percent off when annual, else price",
                                            "R1: 연간 결제：20퍼센트 할인"))
        self.assertOk(fx.status())

    def test_undeclared_existing_input(self):
        fx = self.fixture()
        fx.w("secret file", "x")
        fx.w(".no-illusory-done/LEDGER.md", LEDGER.replace("python3 tests/test_calc.py", "cat 'secret file' && python3 tests/test_calc.py"))
        self.assertRefused(fx.status(), "not in FILES")

    def test_directory_arg_outside_product_needs_all_files_frozen(self):
        fx = self.fixture()
        fx.w("tests/helper.py", "x=1\n")
        fx.w(".no-illusory-done/LEDGER.md", LEDGER.replace("python3 tests/test_calc.py", "python3 -m pytest -q tests"))
        self.assertRefused(fx.status(), "every file under it must be in FILES")

    def test_reading_product_file_is_allowed(self):
        fx = self.fixture()
        fx.w(".no-illusory-done/LEDGER.md", LEDGER.replace("python3 tests/test_calc.py", "grep -q placeholder src/calc.py && python3 tests/test_calc.py"))
        self.assertOk(fx.status())

    def test_red_refuses_green_gate(self):
        fx = self.fixture()
        fx.implement()
        self.assertRefused(fx.red(), "already pass before implementation")

    def test_run_refuses_uncommitted_freeze(self):
        fx = self.fixture()
        self.assertOk(fx.red())
        self.assertRefused(fx.run(), "not committed at HEAD")

    def test_run_refuses_unpushed_freeze(self):
        fx = self.fixture()
        self.assertOk(fx.red()); fx.commit("freeze")
        self.assertRefused(fx.run(), "not reachable from any ref on any reachable remote")

    def test_witness_local_skips_remote(self):
        fx = self.fixture(extra_plan="witness: local\n")
        self.assertOk(fx.red()); fx.commit("freeze")
        r = fx.nid("--verify-freeze")
        self.assertOk(r, "FREEZE: match"); self.assertIn("witness: local", r.stdout)

    def test_stall_and_max_supersedes(self):
        fx = self.fixture(extra_plan="max_supersedes: 1\n")
        fx.freeze()
        self.assertOk(fx.red("--supersede", "first legitimate reason here")); fx.commit("s1"); fx.push()
        self.assertRefused(fx.red("--supersede", "second reason should be refused"), "exceeds max_supersedes")

    def test_ci_rules(self):
        fx = self.fixture(extra_plan="max_ci_attempts: 99\n")
        fx.freeze(); fx.implement(); fx.run()
        self.assertRefused(fx.ci_md("H1: pass from memory"), "without a verifiable pointer")
        self.assertRefused(fx.ci_md("H1: pass @ /dev/null sha=e3b0c44298fc"), "outside")
        self.assertRefused(fx.ci_md("H1: pass @ src/calc.py sha=deadbeefdead"), "hash mismatch")
        self.assertRefused(fx.ci_md("H1: pass @ tests/test_calc.py sha=" + sha(fx.root / "tests/test_calc.py")), "not one of the SUBJECT files")
        self.assertRefused(fx.ci_md("H1: pass $ git diff HEAD --stat -- README.md sha=deadbeefdead"), "must equal a '$ ' SUBJECT command exactly")
        self.assertRefused(fx.ci_md("H1: pass $ git diff HEAD --stat sha=deadbeefdead"), "command output hash mismatch")
        fx.w(".no-illusory-done/CI.md", "CI: merge-ok\n")
        self.assertRefused(fx.nid("--ci", ".no-illusory-done/CI.md"), "missing field")
        self.assertRefused(fx.nid("--ci", "alt/CI.md"), "must be <repo>/.no-illusory-done")

    def test_ci_attempt_cap(self):
        fx = self.fixture(extra_plan="max_ci_attempts: 2\n")
        fx.freeze(); fx.implement(); fx.run()
        outs = [out(fx.ci_md("H1: fail no", verdict="reject")) for _ in range(3)]
        self.assertIn("HANDOFF REQUIRED: 2 CI attempts", outs[2])

    def test_flaky_is_refused_at_ledger_level(self):
        fx = self.fixture(ledger=LEDGER + "  RETRIES: 1\n")
        self.assertRefused(fx.status(), "RETRIES")

    def test_mutation_detects_vacuous_oracle(self):
        weak = "import sys; sys.path.insert(0,'src')\nfrom calc import discount\nassert discount(100, True) is not None\nprint('NID G1')\n"
        fx = self.fixture(test=weak)
        fx.freeze(); fx.implement(); fx.run()
        r = fx.nid("--mutate", ".no-illusory-done/LEDGER.md")
        self.assertNotEqual(r.returncode, 0); self.assertIn("VACUOUS ORACLE", r.stdout)

    def test_mutation_crash_is_not_a_kill(self):
        test = TEST.replace("print('NID G1')", "import x\nprint('NID G1')")
        fx = self.fixture(test=test)
        fx.freeze(); fx.implement(); fx.w("src/x.py", 'VALUE = ["ready"][0]\n'); fx.run()
        r = fx.nid("--mutate", ".no-illusory-done/LEDGER.md")
        self.assertIn("crash-only", r.stdout)

    def test_mutation_inconclusive_no_python_strict_requires_waiver(self):
        test = "import sys\nsys.exit(0 if open('src/data.txt').read().strip() == 'ready' else 1)\n" \
               "print('NID G1')\n"
        test = "import sys\nif open('src/data.txt').read().strip() != 'ready': sys.exit(1)\nprint('NID G1')\n"
        plan = PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: src/data.js")
        fx = self.fixture(test=test, plan=plan, strict=True)
        fx.w("src/data.txt", "not ready\n"); fx.w("src/data.js", "# code subject\n"); fx.commit("data")
        fx.freeze()
        fx.w("src/data.txt", "ready\n"); fx.w("src/data.js", "# touched\n")
        self.assertOk(fx.run(), "ALL MET")
        r = fx.ci_md(f"H1: pass @ src/data.js sha={sha(fx.root / 'src/data.js')}")
        self.assertIn("CI: inconclusive", r.stdout)
        # explicit waiver in the frozen plan is allowed
        fx2 = Fixture(test=test, plan=plan, strict=True, extra_plan="mutation_required: 0\n")
        try:
            fx2.w("src/data.txt", "not ready\n"); fx2.w("src/data.js", "# code subject\n"); fx2.commit("data")
            fx2.freeze(); fx2.w("src/data.txt", "ready\n"); fx2.w("src/data.js", "# touched\n"); fx2.run()
            self.assertIn("CI: merge-ok", fx2.ci_md(f"H1: pass @ src/data.js sha={sha(fx2.root / 'src/data.js')}").stdout)
        finally:
            fx2.cleanup()

    def test_mutation_cap_never_waived(self):
        fx = self.fixture(extra_plan="max_mutants_per_file: 1\nmutation_required: 0\n")
        fx.freeze(); fx.implement(); fx.run()
        r = fx.ci_md()
        self.assertIn("CI: inconclusive", r.stdout); self.assertIn("cannot be waived", r.stdout)

    def test_external_mutate_hook(self):
        fx = self.fixture(extra_plan="MUTATE: python3 tests/mut.py\nMUTATE_EXPECT: NID MUTATION OK\n")
        fx.w("tests/mut.py", "print('score 100%')\nprint('NID MUTATION OK')\n")
        fx.commit("mut")
        fx.freeze(); fx.implement(); fx.run()
        self.assertOk(fx.nid("--mutate", ".no-illusory-done/LEDGER.md"), "MUTATION: pass")
        fx.w("tests/mut.py", "print('survived 3')\n")
        # tests/mut.py is not frozen (not in FILES) but is outside PRODUCT -> scope refusal, which is the right answer
        self.assertRefused(fx.run(), "outside PRODUCT")


class MoreRefusalPaths(Base):
    """Negative tests for die() branches not covered above (round-7 coverage audit)."""

    def test_plan_level(self):
        cases = [
            ("plan_missing", lambda fx: (fx.root / ".no-illusory-done/PLAN.md").unlink(), "PLAN.md missing"),
            ("bad_witness", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN + "witness: cloud\n"), "witness must be"),
            ("dup_r", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN + "R1: duplicate clause here\n"), "duplicate R1"),
            ("dup_h", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN + "H1: again | FALSIFIER: something else | SUBJECT: src/calc.py\n"), "duplicate H1"),
            ("product_root", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN.replace("PRODUCT: src", "PRODUCT: .")), "may not be the repo root"),
            ("product_checker", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN.replace("PRODUCT: src", "PRODUCT: .no-illusory-done")), "may not be the repo root"),
            ("h_vague", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN.replace("no credential-shaped strings in calc", "it works correctly")), "forbidden vague phrase"),
            ("subject_outside", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: /etc/hosts")), "must be an existing regular file"),
            ("subject_nid", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: .no-illusory-done/PLAN.md")), "may not be inside .no-illusory-done"),
            ("subject_cmd_short", lambda fx: fx.w(".no-illusory-done/PLAN.md", PLAN.replace("$ git diff HEAD --stat", "$ ls")), "too short or forbidden"),
            ("expected_new_exists_at_red", lambda fx: (fx.w(".no-illusory-done/PLAN.md", PLAN + "EXPECTED_NEW: package.json\n"), fx.w("package.json", "{}")), "already exists at freeze time"),
        ]
        for name, setup, needle in cases:
            with self.subTest(rule=name):
                fx = Fixture()
                try:
                    setup(fx)
                    r = fx.red() if name == "expected_new_exists_at_red" else fx.status()
                    self.assertRefused(r, needle, f"{name}: {out(r)}")
                finally:
                    fx.cleanup()

    def test_ledger_level(self):
        cases = [
            ("ledger_empty", "", "ledger missing or empty"),
            ("zero_gates", "# nothing here\n", "zero gates"),
            ("dup_gate", LEDGER + LEDGER.replace("python3 tests/test_calc.py", "python3 tests/test_calc.py -v"), "duplicate gate id"),
            ("dup_field", LEDGER + "  COVERS: R1\n", "duplicate field COVERS"),
            ("dup_check", LEDGER + LEDGER.replace("G1:", "G2:"), "identical CHECK"),
            ("bad_kind", LEDGER + "  KIND: human\n", "bad KIND"),
            ("missing_check", LEDGER.replace("  CHECK: python3 tests/test_calc.py\n", ""), "missing CHECK or EXPECT"),
            ("judge_with_check", LEDGER + "  KIND: llm-judge\n", "must not have CHECK/EXPECT"),
            ("bad_red", LEDGER + "  RED: maybe\n", "bad RED value"),
            ("regex_invalid", LEDGER.replace("EXPECT: NID G1", "EXPECT: /NID (G1/"), "EXPECT regex invalid"),
            ("timeout_nonint", LEDGER + "  TIMEOUT: soon\n", "TIMEOUT must be an integer"),
            ("bad_cwd", LEDGER + "  CWD: ../\n", "CWD not a dir inside repo"),
            ("unparseable_check", LEDGER.replace("python3 tests/test_calc.py", "python3 'tests/test_calc.py"), "not parseable"),
            ("files_missing", LEDGER.replace("FILES: tests/test_calc.py", "FILES: tests/test_calc.py, tests/nope.py"), "FILES entry missing"),
            ("no_required_red", LEDGER + "  RED: pass-ok\n", "at least one gate must be RED: required"),
            ("gates_per_r", "".join(LEDGER.replace("G1:", f"G{i}:").replace("CHECK: python3", f"CHECK: python3 -X opt{i}") for i in range(1, 7)), "traceability dilution"),
        ]
        for name, ledger, needle in cases:
            with self.subTest(rule=name):
                fx = Fixture(ledger=ledger)
                try:
                    r = fx.status()
                    self.assertRefused(r, needle, f"{name}: {out(r)}")
                finally:
                    fx.cleanup()

    def test_freeze_and_state_level(self):
        fx = self.fixture()
        fx.freeze()
        fz = fx.root / ".no-illusory-done/FREEZE.sha256"
        good = fz.read_text()
        fz.write_text(good + "garbage line\n"); self.assertRefused(fx.nid("--verify-freeze"), "malformed")
        fz.write_text(good + good.splitlines()[0] + "\n"); self.assertRefused(fx.nid("--verify-freeze"), "duplicate file")
        fz.write_text("RED G1 " + "a" * 64 + " 1\n"); self.assertRefused(fx.nid("--verify-freeze"), "no file hashes")
        fz.write_text(good)
        fx.w(".no-illusory-done/STATE.md", "iteration: many\n"); self.assertRefused(fx.run(), "STATE.md malformed")
        (fx.root / ".no-illusory-done/STATE.md").unlink()
        fx.git("update-ref", "refs/nid/iteration", fx.git("hash-object", "-w", "--stdin", input="abc").stdout.strip() if False else fx.git("rev-parse", "HEAD").stdout.strip())
        self.assertRefused(fx.run(), "refs/nid/iteration is malformed")

    def test_pass_ok_requires_committed_files_and_green(self):
        fx = self.fixture(ledger=LEDGER + "  RED: pass-ok\n", extra_plan="regression_only: 1\n")
        self.assertRefused(fx.red(), "RED: pass-ok but the gate fails now")
        fx.implement()
        fx.w("tests/other.py", "x")  # uncommitted extra file is irrelevant; FILES itself is committed -> ok
        self.assertOk(fx.red())

    def test_ci_and_mutate_misc(self):
        fx = self.fixture()
        fx.freeze()
        self.assertRefused(fx.nid("--mutate", ".no-illusory-done/LEDGER.md"), "baseline not ALL MET")
        fx.implement(); fx.run()
        self.assertRefused(fx.ci_md("H1: pass $ git diff HEAD --stat -- nothing-here sha=deadbeefdead"), "must equal")
        self.assertRefused(fx.nid("--ci", ".no-illusory-done/OTHER.md"), "expects <repo>/.no-illusory-done/CI.md")
        self.assertRefused(fx.nid("--run", ".no-illusory-done/LEDGER.md", cwd=fx.tmp), "must sit at the git toplevel")

    def test_setup_failure_and_subject_symlink(self):
        fx = self.fixture(extra_plan="SETUP: python3 -c 'import sys; sys.exit(3)'\n")
        fx.freeze(); fx.implement(); fx.run()
        self.assertRefused(fx.ci_md(), "SETUP failed")
        fx2 = Fixture()
        try:
            os.symlink("/etc/hosts", fx2.root / "src/link.py")
            fx2.w(".no-illusory-done/PLAN.md", PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: src/link.py"))
            self.assertRefused(fx2.status(), "must be an existing regular file")
        finally:
            fx2.cleanup()


# ---------------------------------------------------------------------------
# 3. Red team — every bypass found in adversarial rounds 1-6 must stay refused
# ---------------------------------------------------------------------------
class RedTeam(Base):
    def _frozen(self, **kw):
        fx = self.fixture(**kw)
        fx.freeze()
        fx.implement()
        return fx

    def test_forged_last_run_json_is_ignored(self):
        fx = self._frozen()
        fx.w("src/calc.py", "# placeholder\n")
        fx.w(".no-illusory-done/evidence/last-run.json", '{"unmet_cmd": [], "llm_judge": []}')
        self.assertRefused(fx.ci_md(), "Stage A fails on THIS run")

    def test_check_mutating_frozen_file_during_run(self):
        fx = self.fixture(ledger=LEDGER.replace("python3 tests/test_calc.py", "python3 tests/test_calc.py && python3 tests/evil.py"))
        fx.w("tests/evil.py", "open('tests/test_calc.py','a').write('#\\n')\nprint('NID G1')\n")
        fx.w(".no-illusory-done/LEDGER.md", fx.root.joinpath(".no-illusory-done/LEDGER.md").read_text().replace(
            "FILES: tests/test_calc.py", "FILES: tests/test_calc.py, tests/evil.py"))
        fx.commit("evil"); fx.freeze(); fx.implement()
        self.assertRefused(fx.run(), "mutated a frozen file during the run")

    def test_check_writing_ci_md(self):
        fx = self._frozen()
        fx.w("src/calc.py", IMPL + "open('.no-illusory-done/CI.md','w').write('CI: merge-ok\\n')\n")
        self.assertRefused(fx.run(), "wrote into .no-illusory-done")

    def test_rehash_and_commit_is_detected(self):
        fx = self._frozen()
        fx.w("tests/test_calc.py", TEST + "#\n")
        fz = fx.root / ".no-illusory-done/FREEZE.sha256"
        txt = fz.read_text()
        h = hashlib.sha256((fx.root / "tests/test_calc.py").read_bytes()).hexdigest()
        fz.write_text(re.sub(r"^[0-9a-f]{64}(  tests/test_calc.py)$", h + r"\1", txt, flags=re.M))
        fx.commit("rehash"); fx.push()
        self.assertRefused(fx.run(), "undeclared re-freeze")

    def test_shallow_clone_refused(self):
        fx = self._frozen()
        clone = fx.tmp / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{fx.remote}", str(clone)], check=True)
        r = subprocess.run([PY, str(clone / "scripts/nid_check.py"), "--verify-freeze"], cwd=str(clone), capture_output=True, text=True)
        self.assertIn("shallow", out(r))

    def test_git_replace_refused(self):
        fx = self._frozen()
        orig = fx.git("rev-parse", "HEAD").stdout.strip()
        fx.w("tests/test_calc.py", TEST + "#\n"); fx.git("add", "-A")
        tree = fx.git("write-tree").stdout.strip()
        new = subprocess.run(["git", "-C", str(fx.root), "commit-tree", tree, "-m", "r"], capture_output=True, text=True, input="").stdout.strip()
        fx.git("replace", orig, new); fx.git("reset", "-q", "--soft", orig)
        self.assertRefused(fx.run(), "FREEZE")

    def test_forged_tracking_ref_without_push(self):
        fx = self.fixture()
        self.assertOk(fx.red()); fx.commit("freeze")
        fx.git("update-ref", "refs/remotes/origin/forged", "HEAD")
        self.assertRefused(fx.nid("--verify-freeze"), "not reachable from any ref on any reachable remote")

    def test_env_var_and_pythonpath_do_not_reach_oracle(self):
        test = "import os,sys\nif not os.environ.get('NID_A_PASS'): sys.exit(1)\nprint('NID G1')\n"
        fx = self.fixture(test=test)
        fx.freeze()
        r = fx.nid("--run", ".no-illusory-done/LEDGER.md", env={"NID_A_PASS": "1", "PYTHONPATH": "/tmp/nowhere"})
        self.assertEqual(r.returncode, 1); self.assertIn("UNMET: G1", r.stdout)

    def test_repo_internal_path_entry_is_stripped(self):
        """bin/ lives INSIDE PRODUCT so the scope guard is silent; only PATH sanitisation can stop the hijack."""
        fx = self._frozen()
        (fx.root / "src/bin").mkdir(); fx.w("src/bin/python3", "#!/bin/sh\nprintf 'NID %s\\n' G1\n"); os.chmod(fx.root / "src/bin/python3", 0o755)
        fx.w("src/calc.py", "# placeholder\n")  # no implementation: only the hijacked interpreter could make G1 pass
        r = fx.nid("--run", ".no-illusory-done/LEDGER.md", env={"PATH": f"{fx.root}/src/bin:./src/bin:{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 1, out(r)); self.assertIn("UNMET: G1", r.stdout)

    def test_influence_files_after_freeze(self):
        """Placed INSIDE PRODUCT so the scope guard is silent; only the influence-file guard can refuse."""
        for name in ("conftest.py", "sitecustomize.py", "hack.pth", "pytest.ini"):
            with self.subTest(file=name):
                fx = Fixture(); fx.freeze(); fx.implement()
                try:
                    fx.w("src/" + name, "x\n")
                    self.assertRefused(fx.run(), "runner-influencing files")
                finally:
                    fx.cleanup()
        fx = Fixture(); fx.freeze(); fx.implement()
        try:
            fx.w("conftest.py", "x\n")
            self.assertRefused(fx.run(), "outside PRODUCT")
        finally:
            fx.cleanup()

    def test_influence_file_inside_product_needs_expected_new(self):
        fx = self._frozen()
        fx.w("src/__init__.py", "")
        self.assertRefused(fx.run(), "runner-influencing files")

    def test_loader_hook_symlink_bin_outside_product(self):
        fx = self._frozen()
        fx.w("tests/loader.py", "hijack\n")
        self.assertRefused(fx.run(), "outside PRODUCT")
        (fx.root / "tests/loader.py").unlink()
        os.symlink("/etc/hosts", fx.root / "link")
        self.assertRefused(fx.run(), "outside PRODUCT")

    def test_symlink_inside_product_pointing_outside(self):
        fx = self._frozen()
        os.symlink("/etc/hosts", fx.root / "src/module.py")
        self.assertRefused(fx.run(), "points outside the repo")

    def test_ignored_python_reaches_mutation(self):
        fx = self._frozen()
        fx.w("src/.gitignore", "hidden.py\n"); fx.w("src/hidden.py", "FLAG = True\n")
        fx.run()
        r = fx.nid("--mutate", ".no-illusory-done/LEDGER.md")
        self.assertIn("hidden.py", r.stdout)

    def test_alt_ledger_refused(self):
        fx = self._frozen()
        (fx.root / "alt").mkdir(); shutil.copy2(fx.root / ".no-illusory-done/LEDGER.md", fx.root / "alt/LEDGER.md")
        self.assertRefused(fx.nid("--run", "alt/LEDGER.md"), "ledger must be <repo>/.no-illusory-done/LEDGER.md")

    def test_state_forgery_ignored(self):
        fx = self.fixture(); fx.freeze()
        fx.w(".no-illusory-done/STATE.md", "iteration: 0\nstall: 0\n\n| id | E | B | note |\n|--|--|--|--|\n| G1 | Satisfied | Affirm | E: Satisfied |\n")
        r = fx.run(); self.assertIn("UNMET: G1", r.stdout)

    def test_pointer_to_unchanged_or_frozen_file(self):
        fx = self._frozen()
        fx.w("src/other.py", "# untouched\n"); fx.commit("other"); fx.push()
        fx.w(".no-illusory-done/PLAN.md", PLAN.replace("SUBJECT: src/calc.py", "SUBJECT: src/other.py"))
        # PLAN is frozen -> mismatch (that is the right answer); the rule itself is covered by test_ci_rules
        self.assertRefused(fx.ci_md("H1: pass @ src/other.py sha=" + sha(fx.root / "src/other.py")), "freeze mismatch")

    def test_pointer_command_must_be_observational(self):
        plan = PLAN.replace("$ git diff HEAD --stat", "$ python3 tests/pointer.py")
        fx = self.fixture(plan=plan)
        fx.w("tests/pointer.py", "open('src/calc.py','a').write('# mutated\\n')\nprint('observed')\n"); fx.commit("ptr")
        fx.freeze(); fx.implement(); fx.run()
        h = hashlib.sha256(b"observed\n").hexdigest()[:16]
        self.assertRefused(fx.ci_md(f"H1: pass $ python3 tests/pointer.py sha={h}"), "must be observational")

    def test_setup_may_not_touch_product(self):
        fx = self.fixture(extra_plan="SETUP: cp tests/impl.py src/calc.py\n")
        fx.w("tests/impl.py", IMPL); fx.commit("impl-file")
        fx.freeze()
        self.assertRefused(fx.ci_md("H1: pass @ src/calc.py sha=000000000000"), "SETUP changed non-ignored files")

    def test_control_flow_swallowing_failure_strict(self):
        led = LEDGER.replace("python3 tests/test_calc.py", "if python3 tests/fail.py; then python3 tests/mark.py; else python3 tests/mark.py; fi")
        fx = self.fixture(ledger=led, strict=True)
        self.assertRefused(fx.status(), "control flow")

    def test_llm_judge_cannot_outnumber_or_cover_alone(self):
        led = LEDGER + "- [ ] G2: judged a\n  KIND: llm-judge\n  COVERS: R1\n- [ ] G3: judged b\n  KIND: llm-judge\n  COVERS: R1\n"
        fx = self.fixture(ledger=led)
        self.assertRefused(fx.status(), "judgment may not outnumber observation")
        plan = PLAN.replace("R1:", "R2: second requirement clause here\nR1:")
        led = LEDGER + "- [ ] G2: judged\n  KIND: llm-judge\n  COVERS: R2\n"
        fx2 = Fixture(plan=plan, ledger=led)
        try:
            self.assertRefused(fx2.status(), "no RUNNABLE gate")
        finally:
            fx2.cleanup()

    def test_output_cap_and_timeout_group_kill(self):
        fx = self.fixture(ledger=LEDGER.replace("python3 tests/test_calc.py", "python3 tests/big.py").replace("FILES: tests/test_calc.py", "FILES: tests/big.py"))
        fx.w("tests/big.py", "import sys\nsys.stdout.write('x'*9000000+'\\nNID G1\\n')\n")
        fx.commit("big"); self.assertOk(fx.red()); fx.commit("freeze"); fx.push()
        r = fx.run(); self.assertIn("UNMET: G1", r.stdout)
        self.assertIn("OUTPUT TOO LARGE", (fx.root / ".no-illusory-done/evidence/G1.out").read_text())

    def test_lite_masked_fallback_reading_product_marker(self):
        """lite allows `||`; the structural guard is that no PRODUCT file may contain the marker."""
        for c in ("python3 tests/test_calc.py || cat src/calc.py",
                  "if python3 tests/test_calc.py; then cat src/calc.py; else cat src/calc.py; fi",
                  "sh -c 'cat src/calc.py'"):
            with self.subTest(check=c):
                fx = Fixture(ledger=LEDGER.replace("python3 tests/test_calc.py", c))
                try:
                    fx.freeze()
                    fx.w("src/calc.py", "NID G1\n")
                    self.assertRefused(fx.run(), "contains the success marker")
                finally:
                    fx.cleanup()

    def test_nid_prefix_boundary(self):
        fx = self.fixture()
        fx.w(".no-illusory-done-evil/secret.txt", "x")
        fx.w(".no-illusory-done/LEDGER.md", LEDGER.replace("python3 tests/test_calc.py", "cat .no-illusory-done-evil/secret.txt && python3 tests/test_calc.py"))
        self.assertRefused(fx.status(), "not in FILES")


# ---------------------------------------------------------------------------
# 4. Documentation examples must obey the checker's own rules
# ---------------------------------------------------------------------------
class DocumentationExamples(Base):
    def _checks_from(self, text):
        return re.findall(r"^\s*CHECK:\s*(.+?)\s*$", text, flags=re.M)

    def test_readme_and_skill_check_lines_pass_strict_blacklist(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("nid", CHECKER)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        checks = self._checks_from((ROOT / "README.md").read_text()) + self._checks_from((ROOT / "SKILL.md").read_text())
        self.assertTrue(checks, "no CHECK examples found in docs")
        for c in checks:
            if c.startswith("<"):
                continue  # template placeholder
            with self.subTest(check=c):
                hits = [name for rx, name in mod.BAD_CHECK_ALWAYS + mod.BAD_CHECK if rx.search(c)]
                self.assertEqual(hits, [], f"doc example violates checker rules: {c!r} -> {hits}")

    def test_readme_negative_check_semantics_under_errexit(self):
        """`grep -Eq SECRET … && exit 1; cat marker`: under errexit the pipeline failure inside an && list must NOT
        abort, and a hit must exit 1 before the marker."""
        with tempfile.TemporaryDirectory() as td:
            Path(td, "marker").write_text("NID G3\n")
            Path(td, "clean.txt").write_text("nothing here\n")
            Path(td, "dirty.txt").write_text("+ token AKIAABCDEFGHIJKLMNOPQR\n")
            cmd = "cat {f} | grep -Ev '^-' | grep -Eq '(sk|ghp|AKIA)\\w{{16,}}' && exit 1; cat marker"
            clean = subprocess.run(["bash", "-o", "errexit", "-o", "pipefail", "-c", cmd.format(f="clean.txt")], cwd=td, capture_output=True, text=True)
            dirty = subprocess.run(["bash", "-o", "errexit", "-o", "pipefail", "-c", cmd.format(f="dirty.txt")], cwd=td, capture_output=True, text=True)
            self.assertEqual((clean.returncode, clean.stdout.strip()), (0, "NID G3"))
            self.assertEqual(dirty.returncode, 1); self.assertNotIn("NID G3", dirty.stdout)

    def test_readme_walkthrough_plan_and_ledger_parse(self):
        """Extract the PLAN.md and LEDGER.md blocks from the README walkthrough and run --status on them."""
        readme = (ROOT / "README.md").read_text()
        blocks = re.findall(r"```markdown\n(.*?)```", readme, flags=re.S)
        plan = next(b for b in blocks if b.startswith("R1:"))
        ledger = next(b for b in blocks if b.startswith("- [ ] G1:") and "G3:" in b)
        fx = self.fixture(plan=plan, ledger=ledger)
        # create every file the example references so --status can validate structure
        (fx.root / "src/pages").mkdir(parents=True); fx.w("src/pages/pricing.tsx", "// tiers\n")
        for f in ("tests/pricing.spec.ts", "tests/discount.spec.ts", "tests/nid/G1.marker", "tests/nid/G2.marker", "tests/nid/G3.marker", "fixtures/pricing.json"):
            fx.w(f, "NID\n")
        fx.commit("example")
        self.assertOk(fx.status(), "3 gates")


if __name__ == "__main__":
    unittest.main()
