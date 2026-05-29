import json
import random
from datetime import date, timedelta
from database import get_conn

DAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def get_week_start(ref_date: date | None = None) -> str:
    d = ref_date or date.today()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def get_used_photo_ids(conn) -> set[int]:
    rows = conn.execute("SELECT photo_id FROM used_photos").fetchall()
    return {r["photo_id"] for r in rows}


def get_week_selections(week_start: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT ws.day_of_week, ws.cropped_path, ws.text_overlay, ws.text_style,
               p.path, p.filename, p.theme, p.tags, p.width, p.height, p.id as photo_id,
               p.source_folder
        FROM weekly_selections ws
        JOIN photos p ON p.id = ws.photo_id
        WHERE ws.week_start = ?
        ORDER BY ws.day_of_week
    """, (week_start,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _folder_filter(folder: str | None) -> str:
    """Return a LIKE pattern for filtering photos by source folder."""
    if not folder:
        return "%"
    f = folder.rstrip("/") + "/"
    return f"{f}%"


def generate_weekly_plan(
    week_start: str | None = None,
    themes: list[str] | None = None,
    force: bool = False,
    folder: str | None = None,       # ← only use photos from this folder
    active_days: list[bool] | None = None,
) -> list[dict]:
    if not week_start:
        week_start = get_week_start()

    conn = get_conn()

    if not force:
        existing = conn.execute(
            "SELECT COUNT(*) as cnt FROM weekly_selections WHERE week_start = ?",
            (week_start,)
        ).fetchone()["cnt"]
        if existing > 0:
            conn.close()
            return get_week_selections(week_start)

    used_ids = get_used_photo_ids(conn)
    folder_pattern = _folder_filter(folder)
    print(f"[scheduler] Generating plan for folder: {folder or 'ALL'}")
    print(f"[scheduler] Pattern: {folder_pattern}")

    # Get available photos filtered by folder + not used
    def fetch_available(exclude_used: bool = True) -> list[dict]:
        if exclude_used and used_ids:
            rows = conn.execute(
                "SELECT * FROM photos WHERE path LIKE ? AND id NOT IN ({})".format(
                    ",".join("?" * len(used_ids))
                ),
                [folder_pattern] + list(used_ids)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM photos WHERE path LIKE ?",
                (folder_pattern,)
            ).fetchall()
        return [dict(r) for r in rows]

    available = fetch_available(exclude_used=True)

    if len(available) < 7:
        # Reset used pool and try again from full folder set
        conn.execute("DELETE FROM used_photos WHERE photo_id IN (SELECT id FROM photos WHERE path LIKE ?)", (folder_pattern,))
        conn.commit()
        available = fetch_available(exclude_used=False)

    # Group by theme
    by_theme: dict[str, list] = {}
    for p in available:
        t = p["theme"] or "otro"
        by_theme.setdefault(t, []).append(p)

    active = active_days if active_days and len(active_days) == 7 else [True] * 7
    selections = []
    picked_ids = set()

    for day_idx in range(7):
        if not active[day_idx]:
            continue

        target_theme = themes[day_idx] if themes and day_idx < len(themes) else None

        pool = []
        if target_theme and target_theme in by_theme:
            pool = [p for p in by_theme[target_theme] if p["id"] not in picked_ids]

        if not pool:
            pool = [p for p in available if p["id"] not in picked_ids]

        if not pool:
            break

        chosen = random.choice(pool)
        picked_ids.add(chosen["id"])
        selections.append({"day": day_idx, "photo": chosen})

    # Save to DB
    conn.execute("DELETE FROM weekly_selections WHERE week_start = ?", (week_start,))
    for sel in selections:
        photo = sel["photo"]
        conn.execute("""
            INSERT INTO weekly_selections (week_start, day_of_week, photo_id)
            VALUES (?, ?, ?)
        """, (week_start, sel["day"], photo["id"]))
        conn.execute("""
            INSERT OR REPLACE INTO used_photos (photo_id, last_used)
            VALUES (?, datetime('now'))
        """, (photo["id"],))
    conn.commit()
    conn.close()

    return get_week_selections(week_start)
