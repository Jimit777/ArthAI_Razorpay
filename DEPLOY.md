# Deploying ArthAI

The app runs on **Render**, unchanged. There is no build step, no container,
and no separate database server — `render.yaml` in the repo root describes
the whole deployment.

## Why not Vercel / Netlify / Lambda

Two things this app does are incompatible with serverless hosting:

1. **The database is a file.** `merchant.db` is SQLite. Serverless gives each
   request a throwaway filesystem, so every write would be lost.
2. **Agent runs are background threads.** Starting an agent spawns a thread
   that reports progress into an in-process dictionary (`RUNS`, `CASH_RUNS`,
   `RECON_RUNS` in `merchant/app.py`), which the browser polls. Serverless
   kills background work when the response is sent, and the next poll may
   reach a different instance entirely. A request timeout (60s on Vercel's
   free plan) would also cut off a 60-record batch part-way.

Render runs one ordinary long-lived process, so neither has to be rebuilt.

---

## First deploy

1. Go to <https://dashboard.render.com/blueprints> and sign in with GitHub.
2. **New Blueprint Instance** → pick `Jimit777/settlement-auditor`.
   Render reads `render.yaml` and proposes one web service, `ledgerline`.
3. It will ask for the values marked `sync: false`:

   | Variable | What it is |
   |---|---|
   | `ANTHROPIC_API_KEY` | powers the agents; without it every run fails |
   | `LEDGERLINE_SECRET_KEY` | encrypts stored gateway credentials |
   | `GOOGLE_CLIENT_ID` | public half of Google sign-in |
   | `GOOGLE_CLIENT_SECRET` | private half — treat like a password |
   | `GOOGLE_REDIRECT_URI` | where Google returns people after sign-in |

   > **The actual values are in `.env.render` in this folder, not here.**
   > This file is committed and the repository is public, so real keys must
   > never be written into it. `.env.render` is gitignored and exists purely
   > to be copied from. Open it, paste each value into Render, and leave it
   > on your machine.

   If you need a fresh `LEDGERLINE_SECRET_KEY`:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. **Create**. The first build takes a few minutes.

You do not know the hostname until the service exists, so `GOOGLE_REDIRECT_URI`
is a chicken-and-egg problem: create the service first, note the URL Render
assigns, then set the variable and let it redeploy.

## Point Google at the deployed app

Google matches the redirect URI **exactly**, so the deployed app needs its own
entry alongside the local one.

In <https://console.cloud.google.com/apis/credentials> → your OAuth client →
**Authorised redirect URIs**, add:

```
https://<your-app>.onrender.com/auth/google/callback
```

Keep `http://127.0.0.1:8000/auth/google/callback` too — that is what local
development uses. Both can be listed at once.

If the consent screen is still in **Testing**, only listed test users can sign
in. Add the Google addresses that will be used on the day.

## Verify

```bash
curl https://<your-app>.onrender.com/healthz
```

Expect `{"ok":true}`. That endpoint opens the database, so a 503 means the app
is running but cannot reach its storage.

---

## Two things to know before demonstrating

### The free plan sleeps

A free instance stops after ~15 minutes of inactivity and takes roughly 40
seconds to wake. **Open the site a few minutes before presenting** so the first
person to click a link is not staring at a spinner.

### The free plan does not keep data

Render offers persistent disks only on paid instances, so on the free plan
`merchant.db` lives on the instance's temporary storage and is **wiped by every
restart, redeploy and sleep**. A demo that creates its own data during the
demonstration is unaffected. Anything expected to still be there tomorrow is
not.

To make it persist, edit `render.yaml`: change `plan: free` to `plan: starter`
(currently $7/month) **and** uncomment the `disk:` block at the bottom. Both
changes are needed together — a disk on a free plan is rejected.

---

## Deploying an update

Push to `master`. Render redeploys automatically and only swaps traffic across
once `/healthz` passes, so a broken build does not replace a working one.

## If a deploy fails

Read the log in the Render dashboard. The usual causes:

- **`ModuleNotFoundError`** — a dependency is missing from `requirements.txt`.
- **`/healthz` returns 503 with `unable to open database file`** — `AUDITOR_DB`
  points somewhere the app cannot write. The response names the path it tried.

  **Removing a variable from `render.yaml` does not remove it from Render.**
  Once a value has been synced, it lives in the service's Environment and stays
  there; deleting the line only stops it being declared. Delete the row in
  **Environment** in the dashboard as well. This cost one deploy: `AUDITOR_DB`
  was set to `/var/data/merchant.db` by the first sync, the disk it refers to
  was never enabled, and the variable outlived its removal from this file.
- **`redirect_uri_mismatch` on sign-in** — `GOOGLE_REDIRECT_URI` and the entry
  in the Google console are not byte-for-byte identical. Check `https` and any
  trailing slash.
- **Health check failing** — hit `/healthz` directly; it reports the underlying
  error rather than only failing.
