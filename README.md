# FF Glory Bot — Telegram bot + payment verification service

Production-ready FastAPI service that runs as a Telegram bot on a VPS **or**
Vercel, verifies payments automatically, and credits user balances in MongoDB.

- **bKash TrxID verification** — user submits `TrxID + sender number`; the bot
  checks them against your TrxID-check provider, retries transient "not found"
  answers (SMS forwarding is 5–20 s late), cross-checks the sender number
  (exact or masked), rejects stale TrxIDs, and credits the balance exactly once.
- **Binance crypto verification** — signed `GET /sapi/v1/capital/deposit/hisrec`
  with a **read-only** API key, matched by `txId`, credited only when Binance
  reports the deposit as completed (`status = 1`), deduplicated by tx hash.
- Async end-to-end (httpx + Motor + FastAPI): the webhook runs the flow
  **inline** before answering. Required on Vercel — post-response background
  work is not guaranteed there (`waitUntil`/`after` are Node/Edge only), so a
  deferred task dies silently and the user never gets a reply.

## Commands

| Command | What it does |
|---|---|
| `/start` | create/show the account, balance, discount |
| `/help` | usage |
| `/balance` | current balance |
| `/bkash <TrxID> <sender_number> [amount]` | e.g. `/bkash BFD90JRLST 01712345678 500` |
| `/binance <COIN> <txId> [amount]` | e.g. `/binance USDT 98A3EA56... 10` |

## ⚠️ Rotate your old credentials first

The `bot.py` you shared had a **live bot token and MongoDB password hardcoded
in source** (and they were pasted into chat). Before deploying:

1. @BotFather → `/mybots` → *API Token* → **Revoke**, then put the new token in
   `TELEGRAM_BOT_TOKEN`.
2. MongoDB Atlas → *Database Access* → **reset the user's password**, then put
   the new URI in `MONGO_URI`.

Never hardcode secrets again — everything here reads from environment variables.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in the values; .env is gitignored
```

Set the webhook (including a secret token that the bot requires on every call):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://<your-host>/ " \
  -d "secret_token=<LONG_RANDOM_STRING>"
```

…and put the same random string in `TELEGRAM_WEBHOOK_SECRET`. When it is set,
webhook calls without the matching `X-Telegram-Bot-Api-Secret-Token` header get
HTTP 403.

### Environment variables

See [.env.example](.env.example) for the full annotated list:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `MONGO_URI`, `MONGO_DB`
- **bKash provider**: `BKASH_API_BASE_URL`, `BKASH_API_KEY`,
  `BKASH_VERIFY_PATH`, request field names (`BKASH_FIELD_TRX/SENDER/AMOUNT`),
  auth header/scheme, payload mode (`json`/`form`), retry tuning,
  `BKASH_MAX_AGE_HOURS` (replay protection, 0 disables).
- **Binance**: `BINANCE_API_KEY`, `BINANCE_API_SECRET` — create the key with
  **Enable Reading only** (no trading, no withdrawals) and restrict it to your
  VPS IP.
- **Credits**: `BDT_RATES=USDT=110.0` — how much BDT 1 unit of a coin is worth.
  A confirmed deposit in a coin without a rate is stored as `needs_review`
  instead of being auto-credited (safe default: no rate, no credit).

### How bKash verification is wired

bKash's official merchant API can only query payments **you** created (by
paymentID) — it has no endpoint for looking up a customer's arbitrary Send
Money TrxID. So `app/bkash.py` is a configurable adapter: point
`BKASH_API_BASE_URL` at your TrxID-check provider, and set the field names /
auth header to whatever that provider expects. The response parser already
understands the common shapes (`success`/`valid` booleans, `status` +
`data.amount/sender`, legacy `trxStatus` codes, bKash's `statusCode: "0000"`).

Anything the parser does **not** recognize is treated as an error — it never
auto-credits on an ambiguous response. After your provider is configured, test
once with a real small TrxID; if it fails with `unrecognized provider response`,
check the server log for the payload and adjust the field names in `.env`.

Payment records live in the `payments` collection with a **unique index on
(kind, ref)**, so a TrxID or tx hash can only ever be credited once.

## Run

**VPS**

```bash
python main.py                      # 0.0.0.0:$PORT (default 8000)
# or: uvicorn app.handlers:app --host 0.0.0.0 --port 8000 --workers 2
```

Behind systemd/nginx just point the reverse proxy at the port and set the
env vars in the unit file.

**Vercel**

```bash
vercel deploy
````api/index.py` exports the FastAPI `app`; Vercel detects it with **zero config** and routes every incoming request to it, so FastAPI's own router matches `/`. Do **not** add a catch-all rewrite (`{"source":"/(.*)","destination":"/api/index"}`) — it collapses the request path to `/api/index` before the function runs and every path answers FastAPI's 404 `Not Found`. `vercel.json` only caps the function at 60 s per invocation (bKash retries run after the Telegram response).

Vercel also **ignores `.env` files** — set the variables in *Project → Settings →
Environment Variables* (or `vercel env add`), then **Redeploy**, because env
vars only apply to new deployments.
Add the env vars in *Project → Settings → Environment Variables* (don't upload
`.env`).

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

Covers: number/masked-sender matching, all four provider response envelopes,
HTTP error mapping, timestamp parsing + freshness, Binance signature
(encode-then-sign) and deposit status classification, config parsing, and app
import wiring.

## Custom APIs

You mentioned previously shared custom API endpoints — only `bot.py` was
attached to this conversation, so nothing else could be integrated. Once you
share those endpoints (base URL + auth + payload shapes), they can be added as
another module next to `app/bkash.py`/`app/binance.py` following the same
pattern (verify → `db.claim_payment` → `db.credit_payment`).

## Layout

```
app/
  config.py     env-driven settings (no secrets in source)
  db.py         Motor client, indexes, race-safe user/payment state
  telegram.py   async sendMessage
  bkash.py      bKash TrxID verification adapter (retries, sender & age checks)
  binance.py    signed Binance deposit lookup + status classification
  handlers.py   FastAPI app: webhook + command flows
api/index.py    Vercel entrypoint
main.py         VPS entrypoint (uvicorn)
tests/          unit tests for the verification logic
```