# Reproducible offline environment

Upstream: https://github.com/pudo/dataset
Upstream SHA: db592cc041014762bba55fd544e982170831f227
Runtime: 3.14.4 (main, Aug 20 2026, 10:41:58) [GCC 15.2.0]
Platform: linux

Create an isolated virtual environment, then run from this checkout:
```
python -m pip install -r requirements-domain.lock
python -m pip install -e .
```
Set `PYTHONUTF8=1` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` before tests.
```
python -m pytest -q test
```
Measured selected pytest process: 0.936 seconds.
Use Linux (Ubuntu 26.04 tested). This Windows host blocks the SQLAlchemy extension; no policy change is needed.

Tests use synthetic in-memory or temporary-file inputs under the upstream license.
No account, cloud service, browser or external database is needed. Recreate the
environment from this immutable commit to reset the starting state.
