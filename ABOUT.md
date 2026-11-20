# Gateway Studio KH — what it is

Gateway Studio KH is the owner's personal model gateway: one API in front of local
Studio Hub machines and cloud model providers. Story Studio KH points at this one
endpoint instead of configuring each provider or Studio itself. There are no
customers, credits or billing.

## What it does

- Publishes the GenStudio-compatible model catalogue with all prices pinned to
  zero and the zero settlement fields Story Studio requires.
- Discovers and enables models from configured cloud providers; provider keys are
  stored in a private 0600 credential file.
- Treats local Hub machines as Fleet locations, separate from cloud providers:
  Fleet holds the machines, their running work and live status.
- Runs accepted work on a background worker with an idempotent SQLite job store.
  Jobs show failure reasons and artifact URLs.
- Manages a voice catalogue: voice sources, search, preview and enable, including
  the extra step needed for local narration voices.
- Authors, reviews, shelves and downloads Content Packs; private Topic Pack drafts
  and candidates stay separate.
- Runs a Swift/AppKit menu-bar app with one grouped-sidebar window: Overview,
  Jobs, Voices, Models, Providers, Fleet and Settings.
- Supervises the service from the menu: start, stop, restart, Hub status, data
  folder and log; quitting the menu app leaves the gateway running.
- Bundles a relocatable Python runtime, so target Macs do not need a Python
  install.

## How it is built

Python service (`gateway/`: `api.py`, `admin.py`, `worker.py`, `store.py`,
`fleet.py`, `catalog.py`, provider definitions and adapters), Swift menu-bar shell
(`menubar/`), fixture and end-to-end tests (`tests/`) and operational docs (`docs/`,
`_handoffs/`).

Build and install the menu-bar app with:

    ./menubar/build.sh

## Where it fits

Story Studio reads its live catalogue and sends all generation requests here; this
app never calls sibling studios directly on Story's behalf. Work goes to a local Hub
over `/api/hub/jobs` with polling, or to an authenticated cloud provider. Relay
Studio's connector is a pane in this app's Providers screen, and Gateway remains the
owner of jobs, models, accounts and deployment.

## Current version

0.2.300, from the `VERSION` file at the checkout root (`repository/VERSION`); the
newest `CHANGELOG.md` entry agrees.
