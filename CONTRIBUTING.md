# Contributing

## Setup

```bash
make install
source .venv/bin/activate
make test
```

That is the whole onboarding. If it takes more than two commands to get a
passing test run on a new machine, that is a bug — please report it.

## The one rule

**The test suite must pass without a PSI kernel.**

Most contributors will be on macOS, a container, or an older kernel, none of
which expose `/proc/pressure`. CI runs on macOS specifically to enforce this.

This is why every reader in `procfs.py` takes a `root`, and why analysis
fixtures are recorded runs rather than live captures. If you find yourself
wanting to write a test that needs real PSI, you almost certainly want either
`tests/fixtures/proc/` (a static tree, for parser tests) or
`MutatingProc` in `tests/test_sampler.py` (a tree that advances between
reads, for sampling-loop tests).

## Where things go

| Change | File |
| ------ | ---- |
| a new threshold | `analysis.py` — `Thresholds`, nowhere else |
| a new kernel source | `procfs.py`, with a matching fixture |
| how a verdict is reached | `rightsize.py` |
| new instance types or prices | `catalog.py`, or a JSON catalog |
| anything reading the environment | `config.py`, and only `config.py` |

Two invariants worth stating explicitly, because breaking them is easy and the
result is a tool that lies:

1. **No magic numbers outside `Thresholds` and `Policy`.** When someone asks
   "why did it say that?", there must be exactly one place to look.
2. **A verdict must carry its evidence.** `evaluate()` attaches the numbers it
   reasoned from. A recommendation nobody can audit is worse than no
   recommendation, because someone will act on it.

## Fixtures

```bash
make fixtures
```

Generated from a fixed seed in `tools/make_fixtures.py`, so they are
byte-identical between runs and show up as a reviewable diff. CI fails if a
regeneration produces changes you did not commit.

If you add a scenario, add it to the table in the README and assert its verdict
in `tests/test_verdicts.py`. A fixture nothing asserts against is dead weight.

## Before opening a PR

```bash
make lint
make test
```
