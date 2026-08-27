# No Illusory Done — dogfood ledger
# This repository verifies itself. The product is the checker; the oracle is the test suite.
R1: the checker passes its own happy-path, refusal-rule, red-team and documentation tests
PRODUCT: scripts, README.md, SKILL.md, LICENSE, .github, .gitignore
strictness: strict
witness: remote
regression_only: 1
mutation_required: 0     # mutating a 1,200-line checker against a 50 s suite is hours of CI; waived explicitly, visibly
max_iterations: 8
stall_iters: 3
max_supersedes: 3
