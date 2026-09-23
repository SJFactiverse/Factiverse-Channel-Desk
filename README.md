# Channel Desk — setup guide

This dashboard shows YouTube stats automatically, and gives your team a place to log X, LinkedIn and Substack numbers by hand. Everyone opens the same link. No install needed on anyone's machine.

Total setup time: about 20 minutes, once.

## What you're setting up

1. **Neon** — a free database that stores everything permanently
2. **Render** — free hosting that runs the app and gives you a public link
3. A **YouTube API key** — free, lets the app auto-pull subscriber and view counts

## Step 1: Create the database (Neon)

1. Go to [neon.com](https://neon.com) and sign up (free, no card needed).
2. Create a new project. Name it anything, e.g. "factiverse-channel-desk".
3. On the project dashboard, find the **Connection string**. It looks like:
   `postgresql://user:password@ep-something.neon.tech/dbname?sslmode=require`
4. Copy this. You'll paste it into Render in Step 3.

## Step 2: Get a YouTube API key

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a new project (top left, "New Project"). Name it anything.
3. Search for "YouTube Data API v3" in the top search bar and click **Enable**.
4. Go to **Credentials** (left sidebar) → **Create Credentials** → **API key**.
5. Copy the key.
6. Find your channel ID: go to your YouTube channel, click your profile picture → Settings → Advanced settings, or use [this lookup tool](https://commentpicker.com/youtube-channel-id.php). It starts with `UC`.

## Step 3: Deploy the app (Render)

1. Go to [github.com](https://github.com) and sign up if you don't have an account.
2. Create a new repository (name it e.g. `channel-desk`), and upload every file in this folder to it (GitHub lets you drag and drop files on the repo page — click "uploading an existing file").
3. Go to [render.com](https://render.com) and sign up (free, connect your GitHub account when prompted).
4. Click **New** → **Web Service**, and pick the repository you just created.
5. Fill in:
   - **Name**: channel-desk (or anything)
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app`
   - **Instance type**: Free
6. Before clicking create, scroll to **Environment Variables** and add:
   - `DATABASE_URL` → paste the Neon connection string from Step 1
   - `YOUTUBE_API_KEY` → paste your key from Step 2
   - `YOUTUBE_CHANNEL_ID` → paste your channel ID from Step 2
7. Click **Create Web Service**. Wait a few minutes for the first deploy.
8. Render gives you a link like `https://channel-desk.onrender.com`. That's the link for Slack.

## Using it day to day

- **YouTube** updates whenever anyone clicks "Refresh now" on that card. It doesn't pull automatically in the background on the free tier, so make refreshing it a quick weekly habit.
- **X, LinkedIn, Substack** have no public API for this kind of data, so log the numbers by hand from each platform's own dashboard:
  - X: your post analytics, under Analytics on x.com
  - LinkedIn: Page admin → Analytics
  - Substack: your Substack dashboard → Stats
- The dashboard sleeps after 15 minutes with no visitors (free tier limit). The first click after a quiet spell takes 30-60 seconds to wake up. Totally normal, not broken.

## Backfilling historical data

If you export CSVs from a platform's own analytics (Substack does this well: Settings → Exports), you don't need to retype every row into the dashboard by hand. `tools/import_substack_history.py` reads Substack's `followers` and `email_stats` exports and writes them straight into the database.

On your machine, with Python installed:

```
pip install psycopg2-binary
set DATABASE_URL=postgresql://...        (paste your Neon connection string)
python tools/import_substack_history.py followers.csv email_stats.csv
```

Safe to re-run — it updates existing days rather than duplicating them. Refresh the dashboard afterwards to see the trend line.

The same pattern works for LinkedIn: `tools/import_linkedin_history.py` reads LinkedIn Page admin's own exports (Analytics → Content, and Analytics → Followers — both "Export" buttons give `.xls` files):

```
pip install psycopg2-binary pandas xlrd
python tools/import_linkedin_history.py factiverse_content_XXXX.xls factiverse_followers_XXXX.xls
```

For YouTube, Studio's own export (Analytics → Advanced mode → Export current view) gives per-day totals and a per-video table, richer than the live API key alone can pull:

```
pip install psycopg2-binary pandas
python tools/import_youtube_history.py Totals.csv Table_data.csv
```

## If something breaks

- **Page won't load at all**: check the Render dashboard → Logs tab for errors, usually a typo in the environment variables.
- **YouTube card shows an error**: double check `YOUTUBE_API_KEY` and `YOUTUBE_CHANNEL_ID` are correct in Render's Environment tab.
- **Data disappeared**: shouldn't happen since it's stored in Neon, not on Render. If it does, check the Neon dashboard to confirm the project is still active (free projects can pause after long inactivity — just open the Neon dashboard to wake it up).
