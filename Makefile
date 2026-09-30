PY ?= python3

.PHONY: test check

# Never pipe these targets: a pipe reports the last command's exit code.
test:
	env -u PYTHONPATH $(PY) tests/test_inflight.py

check: test
	sh -n install.sh
	sh -n uninstall.sh
	sh -n adapters/hermes/inflight-trim.sh
