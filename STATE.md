# Project state / handoff

Keep this file short: it is how the next session (or another chat) picks up
without re-exploring the repo.

## Where things stand

- Branch `freebuff/c026b6459f7393cb5dddbe88` (local), pushed to GitHub `main`
  through the GitHub integration commit API — local `git push` needs a token
  the sandbox does not have.
- Tests: `.venv/bin/python -m pytest -q` → 57 passed (18 verification +
  23 deposit/flow/security/UI tests + 16 token/referral/keyboard tests), also
  run against a fresh tarball of `main`.
- Old bundles in `artifacts/` are from an earlier push; they are not required
  to deploy.

## Module layout (handlers.py was split)

| file | what lives there |
| --- | --- |
| `app/handlers.py` | FastAPI app, webhook, `process_update`, callback routing, main menu, account, order history, `COMMANDS` |
| `app/basic.py` | API-driven plan list, token pricing, `launch_basic` |
| `app/premium.py` | manual premium orders with tokens held |
| `app/referral.py` | invite link, one-time payout, coupons |
| `app/commands.py` | `/bkash`, `/binance`, `/ff*`, `/referral`, `/coupons`, `/redeem` |
| `app/admin_panel.py` | orders, payment review, API settings, token economy |
| `app/state_machine.py` | every text answer (`handle_state_input`, `handle_media`) |
| `app/ui.py` | `Ctx`, shared helpers, `ADMIN_DENIED` |
| `app/tokens.py` | `TokenConfig`: rate, plan costs, referral numbers |

The sub-modules are re-exported at the bottom of `app/handlers.py`, so
`app.handlers.<name>` keeps working for routes and tests.

## What is built

- **Persistent reply keyboard** at the bottom of the chat (Basic Services,
  Premium, Deposit, Account, Referral, Coupons, Categories, APIs, Menu, Cancel,
  plus Admin Panel for admins). Labels are matched with the emoji stripped, so
  wording alone navigates; inline keyboards still carry every action.
- **Tokens are the only currency in the UI.** `app/tokens.py` holds the
  conversion (`tokens = floor(৳ / taka_per_token)`), per-plan costs, referral
  numbers. Balances, plans, premium, order history and stats are all `🪙`.
  Taka appears only on the deposit form, which also states the tokens it buys.
- **Admin Panel → 🪙 Token Settings**: Taka→token rate, basic plan cost,
  premium cost, per-plan overrides (plans come from the FF Glory API), referral
  reward, referral goal, referral coupon value; reset to env defaults.
- **Basic plans come from the API** (`GET /api/pricing`) and are priced in
  tokens; a plan without an override uses the default basic cost.
- **Referrals**: `https://t.me/<bot>?start=ref_<id>`, one payout per invited
  user (unique `(referrer_id, referred_id)` index), admin-set reward, and one
  coupon per completed goal milestone.
- **Coupons**: single-use, owned by one account, `active → redeemed` before the
  tokens are added; listed under Coupons and redeemable with `/redeem CODE`.
- Per-user FF Glory API keys with an admin master switch: Admin Panel → API
  Settings → *User API Keys: On/Off*.
- Premium: tokens held at order, admin `Approve & Complete` keeps them,
  `Reject & Refund` returns them; proof media forwarded to the buyer only.
- Guided bKash deposit form (sender → TrxID → amount → confirm) accepting Taka
  only; auto-verified with a provider, otherwise admin review with
  `Credit Balance` / `Decline`. Crypto sessions per deposit with a unique
  fractional reference amount.
- User-facing text never contains provider payloads, HTTP codes, exception
  strings or env variable names; failures show an `ERR-XXXXXX` reference that
  matches a log line.

## Environment variables still to set on Vercel

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `MONGO_URI`, `MONGO_DB`,
`ADMIN_IDS`, `FFGLORY_API_KEY`, `FFGLORY_MASTER_KEY`, `BKASH_RECEIVER_NUMBER`,
`BOT_USERNAME`, and optionally `TAKA_PER_TOKEN` / `BASIC_TOKEN_COST` /
`PREMIUM_TOKEN_COST` / `REFERRAL_REWARD_TOKENS` / `REFERRAL_GOAL` /
`REFERRAL_COUPON_TOKENS`, `BKASH_API_BASE_URL` / `BKASH_API_KEY`,
`BINANCE_API_KEY` / `BINANCE_API_SECRET`, `BDT_RATES`.
Last known production error: Mongo `bad auth` (wrong `MONGO_URI` credentials).

## Next steps

1. Fix `MONGO_URI` in Vercel, redeploy, re-register the webhook.
2. Rotate the FF Glory master key and account key (shared in chat).
3. Set `BOT_USERNAME` so referral links work without a `getMe` round-trip.
4. Test in production: one bKash form submission (manual review + admin credit),
   one crypto session, one basic launch, one premium approve/reject, one
   referral join and one coupon redemption.
