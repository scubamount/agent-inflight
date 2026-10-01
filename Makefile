PY ?= python3

.PHONY: test check

# Never pipe these targets: a pipe reports the last command's exit code.
test:
	env -u PYTHONPATH $(PY) tests/test_inflight.py
	env -u PYTHONPATH $(PY) tests/test_hermes_plugin.py
	env -u PYTHONPATH $(PY) tests/test_derived.py
	env -u PYTHONPATH $(PY) tests/test_hooks.py

check: test
	sh -n install.sh
	sh -n uninstall.sh
	sh -n adapters/hermes/inflight-trim.sh
	$(PY) -m py_compile adapters/hermes/plugin/__init__.py scripts/parser-parity.py
