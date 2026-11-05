# Fire Bot workspace

    firebot-infra/   THE PRODUCT: reusable software infrastructure (state quality, messaging, recording, analytics, security,
                     sync, storage, diagnostics, ...). Start with firebot-infra/README.md and RELEASE_NOTES.md.
    firebot-app/     a TEST WORKLOAD: a small simulated robot that exercises the infrastructure end to end. Not the focus.

## Install (Python 3.10+)
    python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install "./firebot-infra[crypto]" ./firebot-app       # drop [crypto] for a core-only install
    firebot-infra selftest                                    # 15 quick checks on this machine
    make test                                                 # or: python -m unittest discover -s firebot-infra/tests

See `CHANGELOG.md` for what changed and where each part came from.
