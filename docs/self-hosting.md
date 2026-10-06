# Run your own server

Throughout these docs, `<server>` is the address of an OpenMob server: the dashboard, the phones'
connections, the REST API (`/v1`), the MCP server (`/mcp`) and these docs all live there. In the
developer console, `<server>` is already replaced with the server you're on.

There is no public hosted OpenMob service yet. To use the API, you run a server yourself (or use
one that someone runs for you). One server can hold many accounts, each with its own phones and
API keys.

## Quick start

You need Python 3.10 or newer and an API key for at least one AI model provider.

```sh
git clone https://github.com/andkhong/openphone.git openmob && cd openmob
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml          # edit it: see Configuration below
export ANTHROPIC_API_KEY=…                  # or another provider's key: see Model providers

openmob run --accounts --no-adb --public
```

| Flag | Does |
|---|---|
| `--accounts` | Accounts mode: people sign in, phones pair to their owner's account, and the developer API and MCP server are on. **Required for `/v1` and `/mcp`** |
| `--no-adb` | Only use phones that connect in with the OpenMob app, not phones plugged into this computer |
| `--public` | Also publish the server over HTTPS through a Cloudflare quick tunnel (needs `cloudflared` installed), and print the public link |
| `--quiet` | Don't speak replies aloud on this computer |

The server listens on port 8765 by default (`server_port` and `server_host` in `config.yaml`).

## Accounts

Create the first account and sign in from the server's command line:

```sh
openmob account add you@example.com --password-file ~/.openmob-password   # generates one if the file is missing
openmob account link you@example.com     # prints a one-time sign-in link
openmob account admin you@example.com    # optional: the /admin page (kill switches, audit log)
openmob key create you@example.com --name my-agent   # an API key, without the web console
```

People can also create an account from the OpenMob app when they pair a phone. Anyone who can
reach the server can do that, so don't expose a server you don't want others to sign up on.

Admin rights can only be granted from the server's command line. An admin can switch off any API
key or a whole account's API access from `/admin`.

## HTTPS and reachability

Phones, webhooks and AI providers all need to reach or be reached over HTTPS:

- **Phones** connect from anywhere, so the server needs a public address. The pairing QR code
  doesn't work from a `localhost` address.
- **Webhooks** are only delivered to public `https://` URLs.
- **The Claude API's MCP connector and the OpenAI API's MCP tool** call `https://<server>/mcp` from
  the provider's own servers, so `<server>` must be reachable from the internet over HTTPS.
  Local clients (Claude Code, Cursor, `openmob mcp`) only need to reach it from your machine.

`--public` gives you an HTTPS address without any setup, at a random `trycloudflare.com` name that
stays the same across restarts of the server while the tunnel keeps running. For a permanent
address, put the server behind your own HTTPS reverse proxy or host it (below).

## Model providers

The server's operator chooses which AI model providers run tasks, and which model serves each tier
(`fast`, `balanced`, `best`). Provider keys go in the server's **environment**, never in
`config.yaml`:

| Variable | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI (`pip install -e '.[openai]'`) |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Google (`pip install -e '.[gemini]'`) |
| `OPENAI_COMPATIBLE_API_KEY` | Any OpenAI-compatible endpoint, including local models |

Map the tiers to models with `api_tiers` in `config.yaml` (see `config.example.yaml`). API
callers only ever see the tier names, never which model or provider runs them.

## Configuration

`config.yaml` is read from the current directory, then `~/.config/openmob/config.yaml` (or pass
`openmob -c path/to/config.yaml run …`). The settings that shape the developer API:

| Setting | Default | Meaning |
|---|---|---|
| `api_tiers` | `fast` 1, `balanced` 3, `best` 10 credits per step | Tier name → `{model, credits_per_step}` |
| `api_default_tier` | `balanced` | Tier for tasks that don't name one |
| `api_rate_per_minute` | 60 | Requests per API key per minute |
| `api_max_concurrent_tasks` | 2 | Unfinished tasks per key |
| `api_daily_tasks` | 200 | Tasks per key per UTC day |
| `api_daily_credits` | 0 (no limit) | Credits per key per UTC day |
| `api_max_steps` | 40 | The most steps a task may take |
| `api_max_running_orchestrations` | 3 | Multi-phone runs in progress per key |
| `api_max_running_orchestrations_per_account` | 10 | … per account |
| `question_timeout_seconds` | 300 | A task whose question nobody answers ends after this long (0: never) |
| `sensitive_apps` | banking, payment, wallet, crypto, password and authenticator apps | Package-name patterns API tasks may not open unless the phone's owner allows it |
| `confirm_actions` | `unrequested` | Ask before risky taps the instruction didn't ask for (`always`: every risky tap; `never`: none) |
| `confirm_keywords` | buy, pay, send, delete, … | Words that make a tap risky |
| `default_permission_mode` | `auto` | A phone's [permission mode](safety.md#permission-modes) until its owner picks one: `ask`, `plan` or `auto` (looser modes need the phone's own approval) |
| `money_keywords` | buy, pay, order, checkout, … | Payment words: they ask even when requested and are refused under Don't ask (unless the owner allowed purchases with it) |
| `confirm_skip_phrases` | add to cart, proceed to checkout, … | Steps on the way to a commit point, never asked about |
| `settings_refuse_terms`, `settings_everyday_terms`, `settings_ask_terms` | see `openmob/permissions.py` | The Settings tiers: refused, never asked, asked |
| `app_categories` | messaging, email, social, notes, photos | Categories a trusted entry may name (package-name parts) |
| `history_retention_days` | 0 (keep forever) | Delete chats (task text and outcomes) not used for this many days |

The developer console's **Overview** page shows each account the tiers and limits in effect.

## The phone app

The server offers the Android app at `/download/openmob.apk`. Build it from `android/` in the
repository (`./gradlew assembleDebug`); the server finds the debug build there, or set
`OPENMOB_APK` to the APK's path. See [Set up a phone](phone-setup.md) for what phone owners do.

## Storage and backups

Everything lives in one data directory: `~/.config/openmob`, or `OPENMOB_DATA_DIR` if set.

| File | Holds |
|---|---|
| `accounts.json` | Accounts, paired phones, API key hashes, webhook URLs and signing secrets, settings |
| `ledger.sqlite` | API tasks, credits, the audit log, the webhook outbox, idempotency keys, orchestrations |
| `history.sqlite` | Chat history: the text of tasks and their outcomes |
| `memory.sqlite` | Memory notes |

Back up the whole directory, with the server stopped or using SQLite's online backup
(`sqlite3 ledger.sqlite ".backup ledger.bak"`). `accounts.json` contains secrets (webhook signing
secrets, phone keys): keep backups private.

## One server at a time

Run **one server process per data directory**. Phones hold a live connection to the process they
connected to, running tasks live in its memory, and per-key limits are counted in memory, so
OpenMob doesn't support several servers behind a load balancer yet. Restarting the server ends the
tasks that were running (they fail with `outcome.reason: "server_restarted"` and cost nothing);
phones reconnect by themselves.

## Hosting

The repository includes a `Dockerfile` and a `fly.toml` for [Fly.io](https://fly.io): one always-on
machine with a volume mounted at `/data` (`OPENMOB_DATA_DIR`), running
`openmob run --no-adb --quiet --accounts` on port 8080. Set the provider keys as secrets
(`fly secrets set ANTHROPIC_API_KEY=…`), and don't let the platform stop idle machines: phones
keep a WebSocket open to it.
