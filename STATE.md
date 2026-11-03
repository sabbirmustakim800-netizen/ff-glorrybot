# Project state / handoff

Keep this file short: it is how the next session (or another chat) picks up
without re-exploring the repo.

## Where things stand

- Branch `freebuff/c026b6459f7393cb5dddbe88`, HEAD `871d0d8` (merge of the
  remote `main` `7cb6b23` into the v3 inline-menu build).
- 18/18 tests pass: `.venv/bin/python -m pytest -q`
- Pushed to GitHub `main` through the GitHub integration commit API
  (local git push needs a token the sandbox does not have).
- Bundles for manual upload: `artifacts/ff-glorrybot-v3.zip`,
  `artifacts/changes-vs-main.patch`.

## What is built

- Inline-keyboard menus (no emoji): Basic (auto API), Premium (manual admin
  approval), Deposit, Account, Settings, Admin Panel.
- Premium: funds held at order, admin `Approve & Complete` keeps them,
  `Reject & Refund` returns them; proof media forwarded to the buyer only.
- Admin panel (`ADMIN_IDS` only): pending orders, margin/rates, API settings,
  FF Glory console, stats, broadcast.
- Pricing: `final = base + base*margin/100 + fixed markup`, editable at runtime.

## Environment variables still to set on Vercel

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `MONGO_URI`, `MONGO_DB`,
`ADMIN_IDS`, `FFGLORY_API_KEY`, `FFGLORY_MASTER_KEY`.
Last known production error: Mongo `bad auth` (wrong `MONGO_URI` credentials).

## Next steps

1. Fix `MONGO_URI` in Vercel, redeploy, re-register the webhook.
2. Rotate the FF Glory account key and master key (they were shared in chat).
3. Test one basic launch and one premium approve/reject in production.
