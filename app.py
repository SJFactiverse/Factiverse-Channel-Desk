import os
import json
import csv
import math
import tempfile
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import urllib.request
import urllib.error
import pandas as pd
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")

# Platforms that hold one row per day and accept manual entry.
PLATFORMS = ("x", "linkedin", "substack")

# Platforms that hold a single latest snapshot rather than a daily series.
SNAPSHOTS = ("linkedin_audience", "linkedin_top_posts", "youtube_top_videos")


def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS youtube_cache (
                    id SERIAL PRIMARY KEY,
                    channel_name TEXT,
                    subscriber_count BIGINT,
                    view_count BIGINT,
                    video_count BIGINT,
                    fetched_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS manual_entries (
                    id SERIAL PRIMARY KEY,
                    platform TEXT NOT NULL,
                    entry_date DATE,
                    data JSONB NOT NULL,
                    logged_at TIMESTAMPTZ DEFAULT now()
                );
            """)
        conn.commit()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def clean(value):
    """pandas gives NaN for blanks; JSON and Postgres both want null."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def as_int(value):
    v = clean(value)
    if v is None:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def as_float(value, places=2):
    v = clean(value)
    if v is None:
        return None
    try:
        return round(float(v), places)
    except (ValueError, TypeError):
        return None


def parse_date(raw, fmt):
    v = clean(raw)
    if v is None:
        return None
    try:
        return datetime.strptime(str(v), fmt).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _iso_date(raw):
    """YouTube writes publish time in several formats; let pandas work it out."""
    v = clean(raw)
    if v is None:
        return None
    try:
        return pd.to_datetime(v).strftime("%Y-%m-%d")
    except Exception:
        return None


def drop_empty(d):
    """Keep absent fields absent. A missing metric must not be stored as 0."""
    return {k: v for k, v in d.items() if v is not None}


def fetch_youtube_stats():
    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
        return {"error": "YouTube API key or channel ID isn't set. Add them in Render's Environment settings."}

    url = (
        "https://www.googleapis.com/youtube/v3/channels"
        f"?part=statistics,snippet&id={YOUTUBE_CHANNEL_ID}&key={YOUTUBE_API_KEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"YouTube API error: {e.code} {e.reason}"}
    except Exception as e:
        return {"error": f"Could not reach YouTube API: {e}"}

    items = payload.get("items")
    if not items:
        return {"error": "No channel found. Check YOUTUBE_CHANNEL_ID."}

    stats = items[0]["statistics"]
    snippet = items[0]["snippet"]
    return {
        "channel_name": snippet.get("title"),
        "subscriber_count": int(stats.get("subscriberCount", 0)),
        "view_count": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
    }


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def upsert_manual(cur, platform, entry_date, data, replace=False):
    cur.execute(
        "SELECT id FROM manual_entries WHERE platform = %s AND entry_date = %s;",
        (platform, entry_date)
    )
    existing = cur.fetchone()
    if existing:
        sql = ("UPDATE manual_entries SET data = %s::jsonb WHERE id = %s;" if replace
               else "UPDATE manual_entries SET data = data || %s::jsonb WHERE id = %s;")
        cur.execute(sql, (json.dumps(data), existing[0]))
        return "updated"
    cur.execute(
        "INSERT INTO manual_entries (platform, entry_date, data) VALUES (%s, %s, %s);",
        (platform, entry_date, json.dumps(data))
    )
    return "inserted"


def save_snapshot(cur, platform, payload):
    """Snapshots replace rather than accumulate — only the latest matters."""
    cur.execute("DELETE FROM manual_entries WHERE platform = %s;", (platform,))
    cur.execute(
        "INSERT INTO manual_entries (platform, entry_date, data) VALUES (%s, %s, %s);",
        (platform, datetime.now(timezone.utc).date().isoformat(), json.dumps(payload))
    )


def commit_daily(platform, daily_data, replace=False):
    inserted = updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for date_str, data in daily_data.items():
                if not data:
                    continue
                if upsert_manual(cur, platform, date_str, data, replace) == "inserted":
                    inserted += 1
                else:
                    updated += 1
        conn.commit()
    return inserted, updated


# ---------------------------------------------------------------------------
# LinkedIn parsing
# ---------------------------------------------------------------------------

def parse_linkedin_content(path):
    """Daily metrics. Keeps clicks and engagement rate, which carry far more
    signal than impressions, and keeps organic separate from sponsored so paid
    reach is never silently reported as organic growth."""
    df = pd.read_excel(path, sheet_name="Metrics", header=1)
    cols = df.columns
    out = {}
    for _, row in df.iterrows():
        date_str = parse_date(row.get("Date"), "%m/%d/%Y")
        if not date_str:
            continue
        entry = drop_empty({
            "impressions":        as_int(row.get("Impressions (total)")) if "Impressions (total)" in cols else None,
            "impressions_organic": as_int(row.get("Impressions (organic)")) if "Impressions (organic)" in cols else None,
            "impressions_paid":   as_int(row.get("Impressions (sponsored)")) if "Impressions (sponsored)" in cols else None,
            "unique_impressions": as_int(row.get("Unique impressions (organic)")) if "Unique impressions (organic)" in cols else None,
            "clicks":             as_int(row.get("Clicks (total)")) if "Clicks (total)" in cols else None,
            "reactions":          as_int(row.get("Reactions (total)")) if "Reactions (total)" in cols else None,
            "comments":           as_int(row.get("Comments (total)")) if "Comments (total)" in cols else None,
            "reposts":            as_int(row.get("Reposts (total)")) if "Reposts (total)" in cols else None,
            "engagement_rate":    as_float(_pct(row.get("Engagement rate (total)"))) if "Engagement rate (total)" in cols else None,
        })
        if entry:
            out[date_str] = entry
    return out


def _pct(value):
    """LinkedIn writes engagement rate as a fraction; show it as a percentage."""
    v = clean(value)
    if v is None:
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return f * 100 if f <= 1 else f


def parse_linkedin_all_posts(path):
    """Per-post rows. Gives a top-posts table and, critically, posting cadence:
    without it a drop in impressions is indistinguishable from posting less."""
    df = pd.read_excel(path, sheet_name="All posts", header=1)
    cols = df.columns
    posts, per_day = [], {}

    for _, row in df.iterrows():
        created = clean(row.get("Created date"))
        date_str = None
        if created is not None:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %H:%M"):
                date_str = parse_date(str(created)[:19], fmt)
                if date_str:
                    break
            if not date_str:
                try:
                    date_str = pd.to_datetime(created).strftime("%Y-%m-%d")
                except Exception:
                    date_str = None

        if date_str:
            per_day[date_str] = per_day.get(date_str, 0) + 1

        title = clean(row.get("Post title")) if "Post title" in cols else None
        posts.append({
            "title": str(title)[:160] if title else "(untitled post)",
            "link": clean(row.get("Post link")) if "Post link" in cols else None,
            "date": date_str,
            "impressions": as_int(row.get("Impressions")) if "Impressions" in cols else None,
            "clicks": as_int(row.get("Clicks")) if "Clicks" in cols else None,
            "ctr": as_float(_pct(row.get("Click through rate (CTR)"))) if "Click through rate (CTR)" in cols else None,
            "follows": as_int(row.get("Follows")) if "Follows" in cols else None,
            "engagement_rate": as_float(_pct(row.get("Engagement rate"))) if "Engagement rate" in cols else None,
        })

    # Rank by clicks — intent beats reach. Fall back to impressions.
    posts.sort(key=lambda p: ((p["clicks"] or 0), (p["impressions"] or 0)), reverse=True)
    return posts[:10], {d: {"posts": n} for d, n in per_day.items()}


def parse_linkedin_followers(path):
    """Daily net follower change, plus the audience breakdown sheets, which
    answer who follows rather than how many — the question that actually
    matters when the buyers are AI labs, defence and media."""
    xl = pd.ExcelFile(path)
    daily, audience = {}, {}

    if "New followers" in xl.sheet_names:
        df = pd.read_excel(path, sheet_name="New followers")
        for _, row in df.iterrows():
            date_str = parse_date(row.get("Date"), "%m/%d/%Y")
            if not date_str:
                continue
            v = as_int(row.get("Total followers")) if "Total followers" in df.columns else None
            if v is not None:
                daily[date_str] = {"new_followers": v}

    for sheet, key in (("Seniority", "seniority"), ("Industry", "industry"),
                       ("Job function", "job_function"), ("Company size", "company_size"),
                       ("Location", "location")):
        if sheet not in xl.sheet_names:
            continue
        df = pd.read_excel(path, sheet_name=sheet)
        if df.empty or "Total followers" not in df.columns:
            continue
        label_col = df.columns[0]
        rows = []
        for _, r in df.iterrows():
            label, count = clean(r.get(label_col)), as_int(r.get("Total followers"))
            if label is None or count is None:
                continue
            rows.append({"label": str(label), "count": count})
        rows.sort(key=lambda x: x["count"], reverse=True)
        if rows:
            audience[key] = rows[:12]

    return daily, audience


# ---------------------------------------------------------------------------
# Substack parsing
# ---------------------------------------------------------------------------

def parse_substack_followers(path):
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            date_str = row[0].strip().replace("/", "-")
            try:
                out[date_str] = int(row[1])
            except (ValueError, IndexError):
                continue
    return out


def parse_substack_email_stats(path):
    """Keeps subscribes and signups: open rate says who read it, subscribes
    says which post was good enough to make someone join."""
    out, posts = {}, []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            post_date = row.get("post_date", "")
            if not post_date:
                continue
            date_str = post_date[:10]

            def number(field, scale=1.0, integer=True):
                raw = row.get(field)
                if raw in (None, ""):
                    return None
                try:
                    val = float(raw) * scale
                except ValueError:
                    return None
                return int(val) if integer else round(val, 2)

            entry = drop_empty({
                "reads":           number("views"),
                "open_rate":       number("open_rate", 100, False),
                "engagement_rate": number("engagement_rate", 100, False),
                "subscribes":      number("subscribes"),
                "signups":         number("signups"),
                "title":           row.get("title") or None,
                "posts":           1,
            })
            if entry:
                out[date_str] = entry
                posts.append({
                    "title": row.get("title") or "(untitled)",
                    "date": date_str,
                    "open_rate": entry.get("open_rate"),
                    "reads": entry.get("reads"),
                    "subscribes": entry.get("subscribes"),
                })
    posts.sort(key=lambda p: ((p["subscribes"] or 0), (p["reads"] or 0)), reverse=True)
    return out, posts[:10]


# ---------------------------------------------------------------------------
# YouTube parsing
# ---------------------------------------------------------------------------

def parse_youtube_totals(path):
    """Takes whatever daily columns the Studio export happens to carry, so a
    wider export starts populating extra metrics with no code change."""
    df = pd.read_csv(path)
    cols = df.columns
    out = {}
    for _, row in df.iterrows():
        date_str = parse_date(row.get("Date"), "%Y-%m-%d")
        if not date_str:
            continue
        entry = drop_empty({
            "views":             as_int(row.get("Views")) if "Views" in cols else None,
            "watch_time_hours":  as_float(row.get("Watch time (hours)")) if "Watch time (hours)" in cols else None,
            "subscribers_gained": as_int(row.get("Subscribers")) if "Subscribers" in cols else None,
            "impressions":       as_int(row.get("Impressions")) if "Impressions" in cols else None,
            "ctr":               as_float(row.get("Impressions click-through rate (%)")) if "Impressions click-through rate (%)" in cols else None,
        })
        if entry:
            out[date_str] = entry
    return out


def parse_youtube_top_videos(path, top_n=10):
    df = pd.read_csv(path)
    if "Content" in df.columns:
        df = df[df["Content"] != "Total"]
    cols = df.columns
    if "Views" in cols:
        df = df.sort_values("Views", ascending=False)
    videos = []
    for _, row in df.head(top_n).iterrows():
        videos.append({
            "content": clean(row.get("Content")),
            "title": clean(row.get("Video title")) or clean(row.get("Content")),
            "published": _iso_date(row.get("Video publish time")),
            "duration": as_int(row.get("Duration")) if "Duration" in cols else None,
            "views": as_int(row.get("Views")) or 0,
            "watch_time_hours": as_float(row.get("Watch time (hours)")) or 0,
            "subscribers_gained": as_int(row.get("Subscribers")),
            "thumbnail_impressions": as_int(row.get("Thumbnail impressions")) if "Thumbnail impressions" in cols else None,
            "ctr": as_float(row.get("Thumbnail click-through rate (%)")),
        })
    return videos


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def save_uploads(files):
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(f.filename)[1])
        f.save(tmp.name)
        saved.append((f.filename, tmp.name))
    return saved


def merge(target, source):
    for date_str, entry in source.items():
        target.setdefault(date_str, {}).update(entry)


@app.route("/api/upload/substack", methods=["POST"])
def upload_substack():
    saved = save_uploads(request.files.getlist("files"))
    if not saved:
        return jsonify({"error": "No files received."}), 400

    daily, top_posts, seen = {}, None, []
    try:
        for name, path in saved:
            lower = name.lower()
            if "follower" in lower:
                for d, count in parse_substack_followers(path).items():
                    daily.setdefault(d, {})["subscribers"] = count
                seen.append("followers")
            elif "email_stat" in lower:
                stats, top_posts = parse_substack_email_stats(path)
                merge(daily, stats)
                seen.append("post stats")
            else:
                return jsonify({"error": f"Didn't recognise '{name}'. Expected a filename containing 'followers' or 'email_stats'."}), 400
    finally:
        for _, path in saved:
            os.unlink(path)

    if not daily:
        return jsonify({"error": "No usable rows found in the file(s)."}), 400

    inserted, updated = commit_daily("substack", daily)
    if top_posts:
        with get_conn() as conn:
            with conn.cursor() as cur:
                save_snapshot(cur, "substack_top_posts", {"posts": top_posts})
            conn.commit()

    return jsonify({"ok": True, "inserted": inserted, "updated": updated, "read": seen})


@app.route("/api/upload/linkedin", methods=["POST"])
def upload_linkedin():
    saved = save_uploads(request.files.getlist("files"))
    if not saved:
        return jsonify({"error": "No files received."}), 400

    daily, audience, top_posts, seen = {}, None, None, []
    try:
        for name, path in saved:
            try:
                xl = pd.ExcelFile(path)
            except Exception as e:
                return jsonify({"error": f"Couldn't open '{name}': {e}"}), 400

            if "Metrics" in xl.sheet_names:
                merge(daily, parse_linkedin_content(path))
                seen.append("daily metrics")
                if "All posts" in xl.sheet_names:
                    top_posts, cadence = parse_linkedin_all_posts(path)
                    merge(daily, cadence)
                    seen.append("per-post data")
            elif "New followers" in xl.sheet_names:
                follower_daily, audience = parse_linkedin_followers(path)
                merge(daily, follower_daily)
                seen.append("followers")
                if audience:
                    seen.append("audience breakdown")
            else:
                return jsonify({"error": f"Didn't recognise the sheets in '{name}'. Expected a LinkedIn Content or Followers export."}), 400
    finally:
        for _, path in saved:
            os.unlink(path)

    if not daily and not audience:
        return jsonify({"error": "No usable rows found in the file(s)."}), 400

    inserted, updated = commit_daily("linkedin", daily)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if audience:
                save_snapshot(cur, "linkedin_audience", audience)
            if top_posts:
                save_snapshot(cur, "linkedin_top_posts", {"posts": top_posts})
        conn.commit()

    return jsonify({"ok": True, "inserted": inserted, "updated": updated, "read": seen})


@app.route("/api/upload/youtube", methods=["POST"])
def upload_youtube():
    saved = save_uploads(request.files.getlist("files"))
    if not saved:
        return jsonify({"error": "No files received."}), 400

    daily, top_videos, seen = {}, None, []
    try:
        for name, path in saved:
            lower = name.lower()
            if "totals" in lower:
                daily = parse_youtube_totals(path)
                seen.append("daily totals")
            elif "table_data" in lower or "table data" in lower:
                top_videos = parse_youtube_top_videos(path)
                seen.append("per-video data")
            else:
                return jsonify({"error": f"Didn't recognise '{name}'. Expected a filename containing 'Totals' or 'Table_data'."}), 400
    finally:
        for _, path in saved:
            os.unlink(path)

    if not daily and top_videos is None:
        return jsonify({"error": "No usable rows found in the file(s)."}), 400

    inserted, updated = commit_daily("youtube_views", daily, replace=True)
    if top_videos:
        with get_conn() as conn:
            with conn.cursor() as cur:
                save_snapshot(cur, "youtube_top_videos", {"videos": top_videos})
            conn.commit()

    return jsonify({"ok": True, "inserted": inserted, "updated": updated, "read": seen})


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def snapshot(cur, platform):
    cur.execute(
        "SELECT data FROM manual_entries WHERE platform = %s "
        "ORDER BY entry_date DESC, logged_at DESC LIMIT 1;", (platform,)
    )
    row = cur.fetchone()
    return row["data"] if row else None


@app.route("/api/data")
def api_data():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM youtube_cache ORDER BY fetched_at DESC LIMIT 1;")
            youtube = cur.fetchone()

            cur.execute(
                "SELECT subscriber_count, view_count, video_count, fetched_at "
                "FROM youtube_cache ORDER BY fetched_at ASC LIMIT 120;"
            )
            youtube_history = cur.fetchall()

            manual = {}
            for platform in PLATFORMS:
                cur.execute(
                    "SELECT id, entry_date, data FROM manual_entries "
                    "WHERE platform = %s ORDER BY entry_date DESC LIMIT 180;",
                    (platform,)
                )
                manual[platform] = cur.fetchall()

            cur.execute(
                "SELECT entry_date, data FROM manual_entries "
                "WHERE platform = 'youtube_views' ORDER BY entry_date ASC LIMIT 400;"
            )
            youtube_views_history = cur.fetchall()

            yt_top = snapshot(cur, "youtube_top_videos")
            sub_top = snapshot(cur, "substack_top_posts")
            li_top = snapshot(cur, "linkedin_top_posts")

            payload = {
                "youtube": youtube,
                "youtube_history": youtube_history,
                "youtube_views_history": youtube_views_history,
                "youtube_top_videos": (yt_top or {}).get("videos"),
                "substack_top_posts": (sub_top or {}).get("posts"),
                "linkedin_top_posts": (li_top or {}).get("posts"),
                "linkedin_audience": snapshot(cur, "linkedin_audience"),
                "manual": manual,
                "youtube_configured": bool(YOUTUBE_API_KEY and YOUTUBE_CHANNEL_ID),
            }

    return app.response_class(json.dumps(payload, default=str), mimetype="application/json")


@app.route("/api/refresh-youtube", methods=["POST"])
def api_refresh_youtube():
    result = fetch_youtube_stats()
    if "error" in result:
        return jsonify(result), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO youtube_cache (channel_name, subscriber_count, view_count, video_count) "
                "VALUES (%s, %s, %s, %s);",
                (result["channel_name"], result["subscriber_count"],
                 result["view_count"], result["video_count"])
            )
        conn.commit()

    return jsonify(result)


@app.route("/api/manual/<platform>", methods=["POST"])
def api_manual_add(platform):
    if platform not in PLATFORMS:
        return jsonify({"error": "Unknown platform"}), 400

    body = request.get_json(force=True) or {}
    entry_date = body.pop("entry_date", None) or datetime.now(timezone.utc).date().isoformat()
    body = drop_empty(body)
    if not body:
        return jsonify({"error": "Nothing to save — fill in at least one figure."}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            upsert_manual(cur, platform, entry_date, body)
        conn.commit()

    return jsonify({"ok": True})


@app.route("/api/manual/<platform>/<int:entry_id>", methods=["DELETE"])
def api_manual_delete(platform, entry_id):
    if platform not in PLATFORMS:
        return jsonify({"error": "Unknown platform"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM manual_entries WHERE id = %s AND platform = %s;",
                        (entry_id, platform))
        conn.commit()

    return jsonify({"ok": True})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)
