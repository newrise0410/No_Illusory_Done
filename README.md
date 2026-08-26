# No Illusory Done

Completion discipline for substantial agent work. **Done is a checker verdict, not a sentence.**

- `SKILL.md` — the skill (three roles: test-writer → implementer → LLM CI; freeze; STATE.md belief≠evidence; stop hook)
- `scripts/nid_check.py` — Stage A oracle: `--status`, `--red`, `--freeze`, `--verify-freeze`, `--run`, `--ci`, `--report`

```text
python scripts/nid_check.py --status .no-illusory-done/LEDGER.md   # parse
python scripts/nid_check.py --red    .no-illusory-done/LEDGER.md   # oracle must fail first; writes FREEZE.sha256
git add -A && git commit -m "freeze"                               # freeze must be committed
python scripts/nid_check.py --run    .no-illusory-done/LEDGER.md   # ALL MET / UNMET
python scripts/nid_check.py --ci     .no-illusory-done/CI.md       # validate CI verdict
python scripts/nid_check.py --report                               # machine-generated final report
```
