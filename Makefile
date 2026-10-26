# Offline targets: no API keys, no network, no GPU, no cost.
PYTHON ?= python
OUT    ?= repro_out

.PHONY: test offline-repro offline-repro-quick contamination impact cost protocol-check

test:                      ## unit + regression tests (pytest)
	env -u PYTHONPATH $(PYTHON) -m pytest -q tests

offline-repro:             ## everything: self-tests, analyze x4, E1-E6, figures, claims, regression guard (~30 min)
	PYTHON=$(PYTHON) ./reproduce.sh $(OUT)

offline-repro-quick:       ## same without the slow E6 step (~3 min)
	SKIP_E6=1 PYTHON=$(PYTHON) ./reproduce.sh $(OUT)

contamination:             ## prompt-vs-AgentDojo overlap for every prompt variant
	env -u PYTHONPATH $(PYTHON) tools/check_contamination.py --out $(OUT)/contamination.json

impact:                    ## offline replay impact of the bug fixes
	env -u PYTHONPATH $(PYTHON) tools/replay_impact.py --out $(OUT)/impact.json

cost:                      ## cost estimate for the live reruns, from the logged usage
	env -u PYTHONPATH $(PYTHON) tools/estimate_rerun_cost.py

protocol-check:            ## the frozen clean-protocol artefacts are unchanged
	env -u PYTHONPATH $(PYTHON) tools/protocol_hashes.py --check
