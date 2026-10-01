PY ?= python3

.PHONY: test check lint

# Never pipe these targets: a pipe reports the last command's exit code.
test:
	env -u PYTHONPATH $(PY) tests/test_inflight.py
	env -u PYTHONPATH $(PY) tests/test_hermes_plugin.py
	env -u PYTHONPATH $(PY) tests/test_derived.py
	env -u PYTHONPATH $(PY) tests/test_hooks.py
	env -u PYTHONPATH $(PY) tests/test_audit.py
	env -u PYTHONPATH $(PY) tests/test_adapter.py
	env -u PYTHONPATH $(PY) tests/test_extending.py

check: test
	sh -n install.sh
	sh -n uninstall.sh
	sh -n adapters/hermes/inflight-trim.sh
	$(PY) -m py_compile adapters/hermes/plugin/__init__.py scripts/parser-parity.py scripts/bench-hooks.py \
		examples/backend-example/src/inflight_backend_example/__init__.py

# Needs ruff + mypy: pip install --require-hashes --no-deps -r requirements-lint.txt
lint:
	$(PY) -m ruff check .
	$(PY) -m mypy
