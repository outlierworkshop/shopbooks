"""Shared test check helper: same PASS/FAIL output the per-file lambdas printed, but failures are
remembered and force a NONZERO EXIT CODE, so a failing test can never look green to run_tests.py,
CI, or a shell `&&` chain. (The old `ok = lambda ...` pattern printed FAIL and still exited 0.)

Usage in a test file (safe to import before db/app — this module imports nothing from the repo):

    from testutil import ok
    ok(balance == 12345, "balance survives a round-trip")

The exit hook prints a summary of failed checks to stderr and exits 1. Tests that raise or assert
still fail on their own; this only adds the missing failure path for `ok()`-style checks.
"""
import atexit
import os
import sys

_failures = []


def ok(cond, what):
    """Print PASS/FAIL like the legacy lambdas; remember failures for the exit code."""
    print(("PASS" if cond else "FAIL"), what)
    if not cond:
        _failures.append(str(what))
    return bool(cond)


@atexit.register
def _fail_exit():
    if _failures:
        print(f"\n{len(_failures)} FAILED check(s):", file=sys.stderr)
        for f in _failures:
            print(f"  - {f}", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)  # atexit can't set the exit code any other way; buffers flushed above
