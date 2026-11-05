.PHONY: install test selftest bench

install:
	pip install "./firebot-infra[crypto]" ./firebot-app

test:
	python -m unittest discover -s firebot-infra/tests
	python -m unittest discover -s firebot-app/tests

selftest:
	firebot-infra selftest

bench:
	firebot-infra bench --quick
