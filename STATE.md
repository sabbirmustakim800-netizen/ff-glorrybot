# Project state / handoff

Keep this file short: it is how the next session (or another chat) picks up
without re-exploring the repo.

## Where things stand

- Branch `freebuff/c026b6459f7393cb5dddbe88` (local), pushed to GitHub `main`
  through the GitHub integration commit API — local `git push` needs a token
  the sandbox does not have.
- Tests: `.venv/bin/python -m pytest -q` → 35 passed (18 verification +
  17 deposit/flow/security tests).
- Old bundles in `artifacts/` are from an earlier push; they are not required
  to deploy.

## What is built

- Inline-keyboard menus (no emoji): Basic (auto API), Premium (manual admin
  approval), Deposit, Account, Settings, Admin Panel.
- Premium: funds held at order, admin `Approve & Complete` keeps them,
  `Reject & Refund` returns them; proof media forwarded to the buyer only.
- Guided bKash deposit form (sender → TrxID → amount → confirm) that only
  accepts Taka amounts. With a verification provider it credits automatically;
  without one it goes to the admin review queue with `Credit Balance` /
  `Decline` buttons.
- Admin Panel → Payment Settings: set the receiving bKash number and the
  minimum deposit in Mongo (no redeploy), review pending deposits.
- Crypto deposits: per-session address from Binance
  `/sapi/v1/capital/deposit/address`, unique fractional reference amount for
  memo-less networks, `I have paid - check now` verification, manual txId path
  when Binance keys are absent.
- User-facing text never contains provider payloads, HTTP codes, exception
  strings or env variable names; failures show an `ERR-XXXXXX` reference that
  matches a log line.
- Pricing: `final = base + base*margin/100 + fixed markup`, editable at runtime.

## Environment variables still to set on Vercel

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `MONGO_URI`, `MONGO_DB`,
`ADMIN_IDS`, `FFGLORY_API_KEY`, `FFGLORY_MASTER_KEY`, `BKASH_RECEIVER_NUMBER`,
and optionally `BKASH_API_BASE_URL` / `BKASH_API_KEY`,
`BINANCE_API_KEY` / `BINANCE_API_SECRET`, `BDT_RATES`.
Last known production error: Mongo `bad auth` (wrong `MONGO_URI` credentials).

## Next steps

1. Fix `MONGO_URI` in Vercel, redeploy, re-register the webhook.
2. Rotate the FF Glory master key and account key (shared in chat).
3. Test in production: one bKash form submission (manual review + admin credit),
   one crypto session, one basic launch, one premium approve/reject.
