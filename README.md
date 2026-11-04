# FF Glory Bot — Telegram bot + payment verification service

Production-ready FastAPI service that runs as a Telegram bot on a VPS **or**
Vercel with inline-keyboard menus, two order tiers (automated API + manual
premium approval), dynamic admin-controlled pricing, and a password-free
admin control panel built on `ADMIN_IDS`. Payments are verified automatically
and credited to token wallets in MongoDB.

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
| `/balance` | current token balance |
| `/referral` | your invite link, reward and goal progress |
| `/coupons` | your token coupons |
| `/redeem <CODE>` | redeem a token coupon |
| `/admin` | admin control panel (**ADMIN_IDS only**) |
| `/bkash <TrxID> <sender_number> [amount]` | optional shortcut for the bKash form |
| `/binance <COIN> <txId> [amount]` | optional shortcut for crypto verification |
| `/ffbalance` | FF Glory account credits (basic/premium) |
| `/ffregions` | regions & credit prices |
| `/fflaunch <region> <clan_id>` | launch a glory group (1 credit) |
| `/ffgroups` | your glory groups |
| `/ffglory <group_id>` | current glory of a group |
| `/ffcoupons` | your coupons |
| `/ffredeem <code>` | redeem a coupon code |
| `/ffnotifs` | pending FF Glory notifications |

## Inline menus, tiers and the admin panel

Every screen is an inline keyboard; each label leads with one emoji and then
plain English. A **permanent reply keyboard** sits at the bottom of the chat so
nobody has to type a command:

```
🎮 Basic Services   💎 Premium
💳 Deposit          👤 Account
🎁 Referral         🎟 Coupons
🌍 Categories       🔑 APIs
🏠 Menu             ✖️ Cancel
```

The labels are matched after stripping the emoji, so `Coupons`, `🎟 Coupons`
and `🏠 MENU` all navigate the same way (and a wizard in progress is cancelled
by tapping the keyboard).

Inline screens:

```
🎮 Basic Services (Auto API)
💎 Premium / Secret Bulk
💳 Deposit / Top-up (bKash / Crypto)
👤 My Account / Balance
⚙️ Settings / API Key
🛡 Admin Panel          <- only rendered for ADMIN_IDS
```

**Basic tier — automated and API-driven.** The plan list comes from
`GET /api/pricing` (`app/glory_api.py`); each plan is shown with the token cost
the admin set for it (`🪙 150 — Middle East`). Launching calls
`https://ffglory.pro/api/client/auto-clan-group` with the master key in
`x-api-key` and an account key in `api_key`. The token cost is checked against
the balance, the API response is checked, and the balance is charged **only
after a successful launch** (atomic `tokens >= cost` debit, so two taps cannot
overdraw). The user's own API key, if linked from *Settings / API Key*, is used
instead of the platform key. The order is written to `orders` as `completed`.

Per-user keys are a switch the admin owns: *Admin Panel → API Settings → User
API Keys* toggles them for everyone. When it is off the link option disappears
from every account screen, keys already saved are ignored and every launch runs
on the platform account — no redeploy and nothing lost, because turning it back
on restores the saved keys.

**Premium tier — manual approval.** The user describes the bulk/custom order in
one message. The price is held (debited) when the order is placed, every admin
id in `ADMIN_IDS` receives the details plus buttons
`Approve & Complete` / `Reject & Refund` / `Attach Proof`, and only an admin can
decide. Approve keeps the held funds and notifies the user; reject refunds the
full amount and notifies the user. Order data is sent **only** to admins and is
never echoed back to other users. Proof media the admin sends (photo, video or
document) is forwarded to the buyer only.

**Pricing lives in tokens.** The admin sets the token cost of each plan (or a
default for all Basic plans) and of a premium order; values are stored in the
`settings` document in Mongo and applied on the next update, no redeploy. The
older BDT markup fields (`PROFIT_MARGIN_PERCENT`, `FIXED_MARKUP_BDT`,
`BASIC_CREDIT_BDT`, `PREMIUM_CREDIT_BDT`) are kept only so an existing install
still boots — the bot menu no longer shows them, *Profit Margin / Rates* simply
opens Token Settings.

**API Settings (everything pluggable from the panel).** `FFGLORY_API_KEY`,
`FFGLORY_MASTER_KEY`, `FFGLORY_BASE_URL`, `BKASH_API_BASE_URL`, `BKASH_API_KEY`,
`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BDT_RATES`, `USDT_TRC20`,
`USDT_BEP20`. Tap a key, send the new value, and it overrides the environment
value immediately; send `-` to clear the override. *API Console* runs the FF
Glory calls themselves (account, pricing, groups, coupons, redeemed coupons,
transactions, activity log, notifications) so admins can verify a key on the
spot.

## Deposits: guided forms, no commands

Users never type a raw command to top up. Both methods are button-driven forms
that validate every field and keep the internal state in Mongo.

**bKash (interactive form).** *Deposit → bKash* shows the receiving number and a
`Start bKash Deposit` button, then asks for three things one at a time:

1. the sender number (normalised to `01XXXXXXXXX`; `+880…` is accepted),
2. the TrxID from the confirmation SMS (8–20 letters/digits),
3. the amount (a number or a quick-amount button; the minimum is configurable).

A summary with `Confirm & Verify` follows. If a TrxID verification provider is
configured (`BKASH_API_*`), the payment is checked live and credited
immediately; if not, the request is stored as `needs_review`, every admin gets
the details with `Credit Balance` / `Decline` buttons, and the user receives a
reference number. Declining notifies the buyer; crediting is atomic and can
only happen once per payment.

**bKash number from the panel.** *Admin Panel → Payment Settings → Set bKash
Number* stores the receiving number in the `settings` document. Users see the
new number on the next screen — no redeploy. The same screen sets the minimum
deposit and shows whether bKash/Binance auto-verification is on.

**Crypto (per-session address).** *Deposit → Crypto* asks for a network
(TRC20/BEP20) and an amount in BDT, then opens a deposit session:

- the address comes from `GET /sapi/v1/capital/deposit/address` when Binance
  keys are configured (falling back to the wallet saved in *API Settings*);
- because TRC20 has no memo field, the session gets a **unique fractional
  amount** (e.g. `10.37 USDT` instead of `10.00`), so two users' transfers to
the same address can never be confused;
- `I have paid - check now` reads the deposit ledger and credits the balance
  when a matching, confirmed deposit appears. Each txId can be credited once
  (unique index on `payments(kind, ref)`), and the session is closed on success;
- without Binance keys the same session asks for the transaction hash and goes
to the admin review queue instead.

Sessions expire after 60 minutes and are scoped to the Telegram user who
opened them — a callback with another user's session id is rejected.

**Taka only where money actually changes hands.** The bKash form only accepts
amounts in Bangladeshi Taka (৳): the receiving screen, the amount step, the
summary and the quick-amount buttons all show ৳, and a dollar or crypto amount
(`$5`, `5 USDT`) is refused instead of converted. Crypto deposits stay
separate: there the user still picks a Taka amount but the exact coin amount is
shown for the transfer. Every confirmation also states the tokens it buys
(`You pay ৳500 → You receive 🪙 500`).

## Tokens — the only currency in the UI

Users never see a cash balance, a Taka price or a dollar amount. Balances,
plans, premium orders, order history and admin summaries are all in **tokens**
(`app/tokens.py`). The admin owns every number from *Admin Panel → 🪙 Token
Settings*:

| Setting | Meaning |
|---|---|
| Taka → Token Rate | how many ৳ buy one token (e.g. `৳1.5 = 1 token`) |
| Basic Plan Cost | default token cost of a plan |
| Premium Cost | token cost of a manual premium order |
| Per-Plan Costs | override per plan id from the FF Glory API |
| Referral Reward | tokens paid per joined invite |
| Referral Goal | invites needed to earn one coupon |
| Referral Coupon | token value of that coupon |

A deposit is converted once, at the rate in force when it is verified:
`tokens = floor(paid_taka / taka_per_token)`. Plans without an override use the
default basic cost, so a newly added FF Glory plan is sellable immediately and
can be re-priced in two taps.

## Referrals and coupons

Every user gets a personal link (`https://t.me/<bot>?start=ref_<id>`, built from
`BOT_USERNAME` or `getMe`). When a **new** user opens it:

1. the pair `(referrer, invited)` is recorded — the unique index makes a second
   `/start` or a forwarded link unable to pay twice,
2. the referrer receives the admin-set token reward,
3. every time the invite count reaches the admin-set goal, one coupon is
   issued for that milestone (`🎟 REF-…`, admin-set token value).

Coupons are single-use and owned by one account: redeeming checks the owner and
flips `active → redeemed` atomically before the tokens are added, so a double
tap cannot pay twice. `🎟 Coupons` lists them; `/redeem <CODE>` works too.

**Nothing internal reaches users.** Provider errors, HTTP statuses, payloads,
API keys and configuration variable names are never shown in the chat. A failed
operation returns a short reference (`ERR-1A2B3C`) that matches a full detail
line in the server log; configuration hints are shown to admins only.

**Authorization.** `ADMIN_IDS` is read from the environment only. Non-admins
never see the admin button, and `/admin` or any `a:*` / `o:*` / `pay:*` callback
answers `Access denied. This command and panel are for administrators only.`
Admin wizards re-check the sender on every text input.

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
- **Deposits**: `BKASH_RECEIVER_NUMBER` (initial bKash number; the admin panel
  value wins), `MIN_DEPOSIT_BDT` (default 50).
- **Tokens**: `BOT_USERNAME` (referral links; empty = ask Telegram once),
  `TAKA_PER_TOKEN` (default 1.0), `BASIC_TOKEN_COST` (100),
  `PREMIUM_TOKEN_COST` (300), `REFERRAL_REWARD_TOKENS` (20),
  `REFERRAL_GOAL` (5), `REFERRAL_COUPON_TOKENS` (100). All are initial values —
  *Admin Panel → 🪙 Token Settings* wins and needs no redeploy.
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
import wiring — plus the guided deposits: bKash form validation, Taka-only
amounts, admin review credit/decline, crypto sessions (dynamic address, unique
reference amount, single credit per txId), the secret-leak guard, the emoji
button labels, the admin user-API switch, the token economy (rate conversion,
per-plan costs, token-only screens), referrals (single payout per invite, goal
coupons, coupon redemption once) and the persistent reply keyboard.

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
  config.py        env settings + Pricing model (margin/markup/base prices)
  tokens.py        token economy: ৳→token rate, plan costs, referral settings
  db.py            Motor client, indexes, users/payments/orders/settings/state,
                   tokens, referrals, coupons
  telegram.py      async sendMessage, editMessage, answerCallback, media
  keyboards.py     reply + inline keyboards (one leading emoji per label)
  deposits.py      deposit rules: bKash form validation, crypto session matching
  glory_api.py     FF Glory Reseller API client (master key + account key)
  ffglory.py       compatibility shim re-exporting glory_api
  bkash.py         bKash TrxID verification adapter (retries, sender & age checks)
  binance.py       signed Binance deposit lookup + status classification
  ui.py            shared Ctx, text helpers, admin notifications
  deposit_flows.py guided bKash form + crypto session screens
  handlers.py      FastAPI app: webhook, callback routing, main menu, account
                   (re-exports the screens below, so app.handlers.* still works)
  basic.py         API-driven plan list, token pricing, launch
  premium.py       manual premium orders with tokens held
  referral.py      invite link, one-time payout, goal coupons
  commands.py      /bkash, /binance, the /ff* commands and coupons
  admin_panel.py   admin panel: orders, payments, API settings, token economy
  state_machine.py every text answer (wizards, admin inputs, broadcast)
api/index.py       Vercel entrypoint
main.py            VPS entrypoint (uvicorn)
tests/             unit tests for the verification logic
STATE.md           short handoff note: current commit, pending env vars, next steps
```
