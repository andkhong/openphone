# Side-by-side classifier build

Branch: `experiment/clef-replacement`, based on local `main` at `d053ef6`.
Both lanes run this checkout's application code and shared
`config.classifier-lane.yaml`. Clef replaces the classifier in the real agent,
including screen assessment, fast action selection, tap checks, and app selection.
It uses hosted Cloudflare **clef-flash** with images always enabled for screen
decisions. Jev uses the same questions with text input. The normal capture and OCR
paths remain active. Classifier failures use the existing agent fallback and are
reported in the task feed; a fallback run does not demonstrate Clef success.

From this checkout, run in two terminals:

```sh
scripts/classifier-lane jev
scripts/classifier-lane clef
```

Jev: http://localhost:8766/login. Clef: http://localhost:8767/login.
Each creates its own local test account on first launch. Open
`.classifier-data/jev/login.txt` or `.classifier-data/clef/login.txt` for its login.
The passwords are owner-readable only. Pair a test phone separately from each
dashboard. One phone can connect to one server at a time; simultaneous comparison
requires two test devices. No ADB discovery, installation, or phone actions happen
on server startup. This checkout uses the standard main phone protocol, not the
gatekeeper branch's app. Do not install on the owner's phone without approval.

Accounts, permissions, history, memory, and label caches live in separate
`.classifier-data/{jev,clef}` directories. Session cookies differ too, so both tabs
stay signed in on the same host. Existing servers/data are not reused. Ctrl+C stops
each foreground server; the isolated data remains for the next run.

API credentials are loaded from the shell or the gitignored, owner-only
`.classifier-data/credentials.env` (override with `--credentials PATH`). Supported
entries include `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `TYPESAFE_API_KEY`,
and the main model's `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`.
The loader parses assignments without executing shell commands or printing values.
Run `scripts/classifier-lane clef --check` for an offline configuration check.

Dependencies are installed in this checkout's own `.venv`:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e '.[mac,dev,openai,gemini]'
.venv/bin/python -m pytest -q
```

Live Clef decisions require available Cloudflare Workers AI quota. The hosted
image-classification smoke check returned HTTP 200 after the Workers Paid plan
was enabled. Full phone benchmarks have not yet been run for this build.

This branch uses cloud inference. Local model inference is not implemented here.
