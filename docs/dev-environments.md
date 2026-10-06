# Development environments: standard and gatekeeper

Internal. Two local environments that never share code, Python packages or data, so the build without
the phone-side gatekeeper and the build with it can be tested separately and switched on demand.

| | standard | gatekeeper |
|---|---|---|
| Code | `~/Desktop/openmob` on `main` | `~/Desktop/openmob-gatekeeper`, a git worktree detached at `origin/gatekeeper-p1` |
| Python | its own `.venv` | its own `.venv` (an editable install of that folder, so it imports only its own code) |
| Data (accounts, history, memory, ledger) | `~/.config/openmob` | `~/.config/openmob-gatekeeper` |
| Phone app | the standard build | the gatekeeper build (same app id: a phone holds one or the other) |

The server and the app must match: the gatekeeper app speaks a newer protocol (phone-worded approvals,
refusal codes, task approvals) that only the gatekeeper server understands.

## Setup (once)

```sh
git worktree add --detach ~/Desktop/openmob-gatekeeper origin/gatekeeper-p1
cd ~/Desktop/openmob-gatekeeper
python3 -m venv .venv && .venv/bin/pip install -e .
cp ~/Desktop/openmob/config.yaml . && cp ~/Desktop/openmob/android/local.properties android/
# Start the gatekeeper data as a copy of the standard data, so the same account and phone pairing work
# in both (copy SQLite files with the backup API, or while no server is running).
```

## Daily use: `scripts/om`

```sh
om run standard            # start the standard server on port 8765 (one server at a time)
om run gatekeeper          # or the gatekeeper server
om run gatekeeper 8766     # both at once: a second port (pair a second device, e.g. the emulator, to it)
om phone standard          # install the standard app on the USB phone (om phone <env> <serial> for others)
om phone gatekeeper
om build gatekeeper        # rebuild an environment's app from its code
om update gatekeeper       # pull the latest gatekeeper-p1 and refresh its packages
om status                  # what's checked out, which servers run, which app the phone has
```

Switching: stop the running server (Control + C), `om run <other>`, `om phone <other>`, then tap Connect in
the app. Built apps and the record of what's on the phone live in `~/.config/openmob-envs` (not in git).
Paths can be changed with `OPENMOB_STANDARD_DIR`, `OPENMOB_GATEKEEPER_DIR`, `OPENMOB_STANDARD_DATA`,
`OPENMOB_GATEKEEPER_DATA` and `OPENMOB_ENVS_HOME`.

The two data directories diverge once used: pairing a phone again, or changing settings, in one
environment doesn't change the other.
