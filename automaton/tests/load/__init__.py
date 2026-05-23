"""Load-test scripts for the engine.

These exist to give the operator a measured ceiling for one host: how
many runs/sec are sustainable, how a burst drains, and whether short
steps starve when long ones are running.

They run as standalone Python scripts and also expose a function each
that the regression test in ``tests/test_load_regression.py`` calls
with tiny parameters so CI still catches a 5x slowdown.

See ``docs/scale.md`` for measured numbers and the recommended cutoffs
for moving to Postgres / multi-worker.
"""
