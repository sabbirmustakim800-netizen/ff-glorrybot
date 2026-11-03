# FF Glory Bot — Telegram bot + payment verification service

Production-ready FastAPI service that runs as a Telegram bot on a VPS **or**
Vercel with inline-keyboard menus, two order tiers (automated API + manual
premium approval), dynamic admin-controlled pricing, and a password-free
admin control panel built on `ADMIN_IDS`. Payments are verified automatically
and credited to user balances in MongoDB.

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
| `/start` | create the account and open the main menu |
| `/menu` | main menu again |
| `/help` | usage |
| `/balance` | current balance |
| `/admin` | admin control panel (**ADMIN_IDS only**) |
| `/bkash <TrxID> <sender_number> [amount]` | e.g. `/bkash BFD90JRLST 01712345678 500` |
| `/binance <COIN> <txId> [amount]` | e.g. `/binance USDT 98A3EA56... 10` |
| `/ffbalance` | FF Glory account credits (basic/premium) |
| `/ffregions` | regions & credit prices |
| `/fflaunch <region> <clan_id>` | launch a glory group (1 credit) |
| `/ffgroups` | your glory groups |
| `/ffglory <group_id>` | current glory of a group |
| `/ffcoupons` | your coupons |
| `/ffredeem <code>` | redeem a coupon code |
| `/ffnotifs` | pending FF Glory notifications |

## Inline menus, tiers and the admin panel

Every screen is an inline keyboard with plain-text buttons (no emoji):

```
Basic Services (Auto API)
Premium / Secret Bulk
Deposit / Top-up (bKash / Crypto)
My Account / Balance
Settings / API Key
Admin Panel            <- only rendered for ADMIN_IDS
```

**Basic tier — automated.** `app/glory_api.py` calls
`https://ffglory.pro/api/client/auto-clan-group` with the master key in
`x-api-key` and an account key in `api_key`. The price is checked against the
balance, the API response is checked, and the balance is charged **only after
a successful launch** (atomic `balance >= price` debit, so two taps cannot
overdraw). The user's own API key, if linked from *Settings / API Key*, is used
instead of the platform key. The order is written to `orders` as `completed`.

**Premium tier — manual approval.** The user describes the bulk/custom order in
one message. The price is held (debited) when the order is placed, every admin
id in `ADMIN_IDS` receives the details plus buttons
`Approve & Complete` / `Reject & Refund` / `Attach Proof`, and only an admin can
decide. Approve keeps the held funds and notifies the user; reject refunds the
full amount and notifies the user. Order data is sent **only** to admins and is
never echoed back to other users. Proof media the admin sends (photo, video or
document) is forwarded to the buyer only.

**Dynamic pricing.** `final = base + base × margin/100 + fixed markup`. Admins
change the margin, the fixed markup, or either base price from
*Profit Margin / Rates*; values are stored in the `settings` document in Mongo
and applied on the next update, no redeploy. Defaults come from
`PROFIT_MARGIN_PERCENT`, `FIXED_MARKUP_BDT`, `BASIC_CREDIT_BDT`,
`PREMIUM_CREDIT_BDT`.

**API Settings (everything pluggable from the panel).** `FFGLORY_API_KEY`,
`FFGLORY_MASTER_KEY`, `FFGLORY_BASE_URL`, `BKASH_API_BASE_URL`, `BKASH_API_KEY`,
`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BDT_RATES`, `USDT_TRC20`,
`USDT_BEP20`. Tap a key, send the new value, and it overrides the environment
value immediately; send `-` to clear the override. *API Console* runs the FF
Glory calls themselves (account, pricing, groups, coupons, redeemed coupons,
transactions, activity log, notifications) so admins can verify a key on the
spot.

**Authorization.** `ADMIN_IDS` is read from the environment only. Non-admins
never see the admin button, and `/admin` or any `a:*` / `o:*` callback answers
`Access denied. This command and panel are for administrators only.` Admin
wizards re-check the sender on every text input.

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
- **Admin panel**: `ADMIN_IDS` — comma-separated Telegram user ids. This is the
  only source of admin rights; it is never editable from the bot.
- **Pricing**: `PROFIT_MARGIN_PERCENT`, `FIXED_MARKUP_BDT`, `BASIC_CREDIT_BDT`,
  `PREMIUM_CREDIT_BDT`, `USD_BDT_RATE` — initial values only, the panel wins.
- **Crypto wallets**: `USDT_TRC20_ADDRESS`, `USDT_BEP20_ADDRESS`,
  `SUPPORT_CONTACT`.
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
- **FF Glory API**: `FFGLORY_API_KEY` + `FFGLORY_MASTER_KEY` (both required for
  the `/ff*` commands), `FFGLORY_BASE_URL` (defaults to `https://ffglory.pro`).
  Every call sends the master key as an `x-api-key` header **and** the account
  key as an `api_key` query parameter, as the FFGlory docs require. Rotate the
  account key if it was ever shared.

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
```

`api/index.py` exports the FastAPI `app`; Vercel detects it with **zero config** and routes every incoming request to it, so FastAPI's own router matches `/`. Do **not** add a catch-all rewrite (`{"source":"/(.*)","destination":"/api/index"}`) — it collapses the request path to `/api/index` before the function runs and every path answers FastAPI's 404 `Not Found`. `vercel.json` only caps the function at 60 s per invocation (bKash retries run after the Telegram response).

Vercel also **ignores `.env` files** — set the variables in *Project → Settings →
Environment Variables* (or `vercel env add`), then **Redeploy**, because env
vars only apply to new deployments.

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

Covers: number/masked-sender matching, all four provider response envelopes,
HTTP error mapping, timestamp parsing + freshness, Binance signature
(encode-then-sign) and deposit status classification, config parsing, and app
import wiring.

## Custom APIs — FF Glory Reseller API

`app/glory_api.py` is the async httpx client for the FF Glory Reseller API
(`FFGLORY_BASE_URL`, default `https://ffglory.pro`): account, pricing, group
launch, group list/glory, coupons, redeem, transactions, activity log and
notifications. Each call sends the master key as `x-api-key` **and** the
account key as `api_key`, so either can be swapped from the admin panel.

`app/ffglory.py` is kept as a compatibility shim that re-exports the same
names, so older imports keep working. No key is hardcoded anywhere — set
`FFGLORY_API_KEY` and `FFGLORY_MASTER_KEY` in the environment (Vercel:
*Project → Settings → Environment Variables*, then **Redeploy**) or from the
admin panel. Until both are set, the `/ff*` commands reply with a "not
configured" message instead of calling the API.

## Layout

```
app/
  config.py     env settings + Pricing model (margin/markup/base prices)
  db.py         Motor client, indexes, users/payments/orders/settings/state
  telegram.py   async sendMessage, editMessage, answerCallback, media
  keyboards.py  inline keyboards (plain-text buttons, no emoji)
  glory_api.py  FF Glory Reseller API client (master key + account key)
  ffglory.py    compatibility shim re-exporting glory_api
  bkash.py      bKash TrxID verification adapter (retries, sender & age checks)
  binance.py    signed Binance deposit lookup + status classification
  handlers.py   FastAPI app: webhook, menus, tiers, admin panel
api/index.py    Vercel entrypoint
main.py         VPS entrypoint (uvicorn)
tests/          unit tests for the verification logic
STATE.md        short handoff note: current commit, pending env vars, next steps
```
