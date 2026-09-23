"""
One-time backfill: import YouTube Studio's own CSV exports into the Channel Desk database.

From YouTube Studio: Analytics -> Advanced mode -> pick a date range -> Export current view.
That gives you a zip with (among others) these two files this script uses:
  - Totals.csv       -> one row per day, channel-wide views. Powers the daily views trend.
  - Table_data.csv   -> one row per video (lifetime totals). Powers the top-videos table.
Chart_data.csv (per-video per-day) isn't used here — Totals.csv already gives the
channel-wide daily trend without needing to sum it yourself.

Run this ONCE per export. Safe to re-run — daily views update in place rather than
duplicating, and the top-videos table is simply replaced with the latest export.

USAGE:
    1. Install dependencies if you don't already have them:
         pip install psycopg2-binary pandas
    2. Set your Neon connection string (same one used in Render's Environment tab):
         Windows (Command Prompt):  set DATABASE_URL=postgresql://...
         Mac/Linux:                 export DATABASE_URL=postgresql://...
    3. Run:
         python import_youtube_history.py Totals.csv Table_data.csv
"""

import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def parse_totals(path):
    """Returns {date_str: views} from Totals.csv"""
    df = pd.read_csv(path)
    out = {}
    for _, row in df.iterrows():
        date_raw = row.get("Date")
        views = row.get("Views")
        if pd.isna(date_raw) or pd.isna(views):
            continue
        try:
            date_str = datetime.strptime(str(date_raw), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        out[date_str] = int(views)
    return out


def parse_top_videos(path, top_n=10):
    """Returns a list of video dicts, sorted by views descending, excluding the 'Total' row."""
    df = pd.read_csv(path)
    df = df[df["Content"] != "Total"]
    df = df.sort_values("Views", ascending=False).head(top_n)

    videos = []
    for _, row in df.iterrows():
        ctr = row.get("Thumbnail click-through rate (%)")
        videos.append({
            "content": row.get("Content"),
            "title": row.get("Video title") if pd.notna(row.get("Video title")) else row.get("Content"),
            "views": int(row["Views"]) if pd.notna(row.get("Views")) else 0,
            "watch_time_hours": float(row["Watch time (hours)"]) if pd.notna(row.get("Watch time (hours)")) else 0,
            "subscribers_gained": int(row["Subscribers"]) if pd.notna(row.get("Subscribers")) else 0,
            "ctr": float(ctr) if pd.notna(ctr) else None
        })
    return videos


def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set. See the instructions at the top of this file.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python import_youtube_history.py <Totals.csv> [Table_data.csv]")
        sys.exit(1)

    daily_views = {}
    top_videos = None

    for path in sys.argv[1:]:
        name = os.path.basename(path).lower()
        if "totals" in name:
            daily_views = parse_totals(path)
            print(f"Read {len(daily_views)} days of channel views from {path}")
        elif "table_data" in name or "table data" in name:
            top_videos = parse_top_videos(path)
            print(f"Read {len(top_videos)} top videos from {path}")
        else:
            print(f"Skipping {path} — expected a filename containing 'Totals' or 'Table_data'.")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    inserted, updated = 0, 0
    for date_str, views in sorted(daily_views.items()):
        data = {"views": views}
        cur.execute(
            "SELECT id FROM manual_entries WHERE platform = 'youtube_views' AND entry_date = %s;",
            (date_str,)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE manual_entries SET data = %s::jsonb WHERE id = %s;",
                (json.dumps(data), existing[0])
            )
            updated += 1
        else:
            cur.execute(
                "INSERT INTO manual_entries (platform, entry_date, data) VALUES ('youtube_views', %s, %s);",
                (date_str, json.dumps(data))
            )
            inserted += 1

    if top_videos is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        cur.execute(
            "INSERT INTO manual_entries (platform, entry_date, data) VALUES ('youtube_top_videos', %s, %s);",
            (today, json.dumps({"videos": top_videos}))
        )
        print("Saved a fresh top-videos snapshot.")

    conn.commit()
    cur.close()
    conn.close()

    print(f"Done. Daily views: inserted {inserted}, updated {updated}.")
    print("Refresh the dashboard to see the trend line and top videos.")


if __name__ == "__main__":
    main()
