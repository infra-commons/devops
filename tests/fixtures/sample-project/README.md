# sample-project — fixture for the Python tests reusable

Not a real project, and not part of anything this repository ships. It exists so
`.github/workflows/python-tests-self-test.yml` can run
`.github/workflows/python-tests-reusable.yml` against a genuine Python tree rather than
against a description of one.

It is deliberately **partly covered**: `calc.py` holds statements no test exercises, so
the measured rate sits well above `floor:50` and well below `floor:99`. Both of those
numbers are load-bearing — one self-test job proves a satisfiable floor passes, another
proves an unsatisfiable one actually fails. A fully covered fixture would make the second
impossible to write, and a fixture at 0% would make the first meaningless.

`empty/` is intentionally a directory holding no Python at all. It is the `--cov` target for
the census negative control: a path that exists (so the resolver's pre-flight check is
satisfied) and yet contributes no measured file, which is the one dead-coverage shape only
a post-run census can catch.

If you change the fixture, re-check both floors — see the self-test workflow's header.
