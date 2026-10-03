# SubTrack — Vercel + Neon + Resend

A login-free Flask subscription renewal tracker that:

- stores subscriptions in PostgreSQL (Neon in production)
- separates visitors with an anonymous browser cookie
- sends renewal reminder emails through Resend
- runs reminder checks daily using Vercel Cron
- avoids duplicate reminder emails with a `reminder_logs` table

## 1. Local setup

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
python main.py
```

Open `http://127.0.0.1:5000`.

## 2. Neon PostgreSQL

Create a Neon project/database and copy its Postgres connection string. It normally looks like:

```text
postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require&channel_binding=require
```

Set that whole value as `DATABASE_URL` in Vercel. The app creates its prototype tables automatically on first startup.

## 3. Resend

Create a Resend API key and set:

```text
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=SubTrack <reminders@your-verified-domain.com>
```

For development you can use Resend's test sender/testing addresses. For real emails to arbitrary users, verify a sending domain in Resend and use an address on that domain.

## 4. Cron secret

Generate a long random value and set:

```text
CRON_SECRET=your-long-random-secret
```

Vercel automatically sends it to cron endpoints as:

```text
Authorization: Bearer <CRON_SECRET>
```

The Flask route rejects calls without the matching header.

## 5. Vercel schedule

`vercel.json` runs:

```text
GET /api/cron/reminders
```

at `07:00 UTC` daily. In Nigeria (WAT, UTC+1) that is about 08:00.

Vercel Cron schedules use UTC. On the Hobby plan, cron jobs are limited to once per day and may execute within the scheduled hour rather than at the exact minute.

## 6. Required Vercel environment variables

Add all four under Project → Settings → Environment Variables:

```text
DATABASE_URL
RESEND_API_KEY
RESEND_FROM_EMAIL
CRON_SECRET
```

Set them for Production. You can also set them for Preview/Development if desired, but Vercel cron jobs only run on Production deployments.

## 7. Test the cron endpoint manually

Local example:

```bash
curl -H "Authorization: Bearer YOUR_CRON_SECRET" http://127.0.0.1:5000/api/cron/reminders
```

A typical result:

```json
{
  "ok": true,
  "date_utc": "2026-10-03",
  "sent": 1,
  "skipped": 3,
  "failed": []
}
```

The endpoint sends one reminder per subscription/renewal cycle once the subscription enters its configured reminder window. The reminder log prevents duplicates.

## 8. Deploy

Push the folder to GitHub, import the repository into Vercel, add the environment variables, and deploy the Production build.

After deployment:

- open `/health` to verify the Flask app is live
- add a test subscription with your email
- temporarily choose a renewal date inside its reminder window
- invoke `/api/cron/reminders` with the protected Authorization header or wait for the production cron run

## Production note

This version uses `db.create_all()` because it is a small prototype. Before making frequent database schema changes, add Alembic/Flask-Migrate migrations.
