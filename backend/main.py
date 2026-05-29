import json
import os
import subprocess
import zipfile
import asyncio
import shutil
from pathlib import Path
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ── Global indexing progress tracker ──────────────────────────────────────────
_index_state: dict = {
    "running": False,
    "total_found": 0,
    "done": 0,
    "skipped": 0,
    "errors": 0,
    "current": "",
    "last_total_db": 0,
}

from database import init_db
from indexer import index_folder, count_unindexed, generate_phrase
from scheduler import generate_weekly_plan, get_week_selections, get_week_start
from cropper import smart_crop_to_story, add_text_overlay
from database import get_conn

app = FastAPI(title="Stories Creator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUTS = Path(__file__).parent / "outputs"
OUTPUTS.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)), name="outputs")


@app.on_event("startup")
def startup():
    init_db()


# ── Models ──────────────────────────────────────────────────────────────────

class IndexRequest(BaseModel):
    folder_path: str
    use_ai: bool = True


class WeekPlanRequest(BaseModel):
    week_start: str | None = None
    themes: list[str] | None = None
    force: bool = False


class CropRequest(BaseModel):
    photo_id: int
    week_start: str
    day_of_week: int


class AIConfigRequest(BaseModel):
    provider: str          # "gemini" | "openai"
    api_key: str


class AIConfigResponse(BaseModel):
    provider: str
    configured: bool
    model: str


class TextOverlayRequest(BaseModel):
    week_start: str
    day_of_week: int
    text: str
    style: dict


class SwapPhotoRequest(BaseModel):
    week_start: str
    day_of_week: int
    theme: str | None = None
    folder: str = ""
    exclude_ids: list[int] = []   # IDs that have already been shown for this day


class PhraseRequest(BaseModel):
    week_start: str
    day_of_week: int
    tone: str = "inspirador"
    topic: str = ""
    font: str = "Helvetica Neue"
    text_position: str = "bottom"
    style: dict = {}


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ai/config")
def get_ai_config():
    """Return current AI provider config (never returns the key itself)."""
    from indexer import _use_gemini
    gemini_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    openai_set = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    if _use_gemini():
        return {"provider": "gemini", "configured": gemini_set, "model": "gemini-2.0-flash"}
    elif openai_set:
        return {"provider": "openai", "configured": True, "model": "gpt-4o-mini"}
    return {"provider": "none", "configured": False, "model": ""}


@app.post("/ai/config")
def set_ai_config(req: AIConfigRequest):
    """Set AI provider and API key at runtime (updates env + .env file)."""
    env_path = Path(__file__).parent / ".env"

    if req.provider == "gemini":
        os.environ["GEMINI_API_KEY"] = req.api_key
        os.environ.pop("OPENAI_API_KEY", None)
        model = "gemini-2.0-flash"
    elif req.provider == "openai":
        os.environ["OPENAI_API_KEY"] = req.api_key
        os.environ.pop("GEMINI_API_KEY", None)
        model = "gpt-4o-mini"
    else:
        raise HTTPException(400, "provider must be 'gemini' or 'openai'")

    # Persist to .env
    lines = []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY=") or line.startswith("OPENAI_API_KEY="):
                continue
            lines.append(line)
    if req.provider == "gemini":
        lines.append(f"GEMINI_API_KEY={req.api_key}")
        lines.append("# OPENAI_API_KEY=")
    else:
        lines.append(f"OPENAI_API_KEY={req.api_key}")
        lines.append("# GEMINI_API_KEY=")
    env_path.write_text("\n".join(lines) + "\n")

    # Quick validation test
    try:
        from indexer import generate_phrase
        test = generate_phrase("naturaleza", [], "inspirador", "")
        ok = bool(test)
    except Exception as e:
        return {"provider": req.provider, "configured": True, "model": model, "test": f"error: {e}"}

    return {"provider": req.provider, "configured": True, "model": model, "test": test}


@app.post("/index")
async def index_photos(req: IndexRequest, background_tasks: BackgroundTasks):
    if _index_state["running"]:
        return {"message": "Already running", "folder": req.folder_path}

    # Count total before starting
    preview = count_unindexed(req.folder_path)
    _index_state.update({
        "running": True,
        "total_found": preview.get("to_index", 0),
        "done": 0, "skipped": 0, "errors": 0,
        "current": "",
        "last_total_db": preview.get("already_indexed", 0),
    })

    def progress_cb(**kwargs):
        _index_state.update(kwargs)

    def run():
        try:
            index_folder(req.folder_path, req.use_ai, progress_cb=progress_cb)
        finally:
            _index_state["running"] = False
            _index_state["current"] = ""

    background_tasks.add_task(run)
    return {"message": "Indexing started", "folder": req.folder_path, "to_index": _index_state["total_found"]}


@app.get("/index/progress")
def index_progress():
    """Return current indexing progress as plain JSON (for polling)."""
    conn = get_conn()
    db_total = conn.execute("SELECT COUNT(*) as n FROM photos").fetchone()["n"]
    conn.close()
    return {**_index_state, "db_total": db_total}


@app.get("/index/status")
def index_status():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) as n FROM photos").fetchone()["n"]
    by_theme = conn.execute(
        "SELECT theme, COUNT(*) as n FROM photos GROUP BY theme ORDER BY n DESC"
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "by_theme": [dict(r) for r in by_theme],
        "indexing": _index_state["running"],
    }


@app.get("/photos")
def list_photos(theme: str | None = None, limit: int = 50, offset: int = 0):
    conn = get_conn()
    if theme:
        rows = conn.execute(
            "SELECT * FROM photos WHERE theme = ? LIMIT ? OFFSET ?",
            (theme, limit, offset)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM photos LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/week/plan")
def create_week_plan(req: WeekPlanRequest):
    return generate_weekly_plan(req.week_start, req.themes, req.force)


@app.get("/week/{week_start}")
def get_week(week_start: str):
    selections = get_week_selections(week_start)
    return selections


@app.get("/week")
def get_current_week():
    week_start = get_week_start()
    return {"week_start": week_start, "selections": get_week_selections(week_start)}


@app.post("/crop")
def crop_photo(req: CropRequest):
    conn = get_conn()
    photo = conn.execute("SELECT * FROM photos WHERE id = ?", (req.photo_id,)).fetchone()
    if not photo:
        raise HTTPException(404, "Photo not found")

    out_name = f"w{req.week_start}_d{req.day_of_week}_{photo['filename']}.jpg"
    out_path = smart_crop_to_story(photo["path"], out_name)

    conn.execute("""
        UPDATE weekly_selections SET cropped_path = ?
        WHERE week_start = ? AND day_of_week = ?
    """, (out_path, req.week_start, req.day_of_week))
    conn.commit()
    conn.close()

    return {"cropped_path": out_path, "url": f"/outputs/{Path(out_path).name}"}


@app.post("/text-overlay")
def apply_text_overlay(req: TextOverlayRequest):
    conn = get_conn()
    sel = conn.execute("""
        SELECT ws.*, p.path, p.filename
        FROM weekly_selections ws JOIN photos p ON p.id = ws.photo_id
        WHERE ws.week_start = ? AND ws.day_of_week = ?
    """, (req.week_start, req.day_of_week)).fetchone()

    if not sel:
        raise HTTPException(404, "Selection not found")

    base = sel["cropped_path"] or sel["path"]
    out_name = f"w{req.week_start}_d{req.day_of_week}_text.jpg"
    out_path = add_text_overlay(base, req.text, req.style, out_name)

    conn.execute("""
        UPDATE weekly_selections SET text_overlay = ?, text_style = ?
        WHERE week_start = ? AND day_of_week = ?
    """, (req.text, json.dumps(req.style), req.week_start, req.day_of_week))
    conn.commit()
    conn.close()

    return {"output_path": out_path, "url": f"/outputs/{Path(out_path).name}"}


@app.post("/swap")
def swap_photo(req: SwapPhotoRequest):
    conn = get_conn()
    sel = conn.execute("""
        SELECT photo_id FROM weekly_selections
        WHERE week_start = ? AND day_of_week = ?
    """, (req.week_start, req.day_of_week)).fetchone()

    current_id = sel["photo_id"] if sel else None

    used_this_week = {
        r["photo_id"] for r in conn.execute(
            "SELECT photo_id FROM weekly_selections WHERE week_start = ?",
            (req.week_start,)
        ).fetchall()
    }

    folder_pattern = (req.folder.rstrip("/") + "/%") if req.folder.strip() else "%"
    # Exclude: current photo + other days this week + previously shown for this day
    exclude = used_this_week | set(req.exclude_ids) | {current_id or -1}

    def find_available(with_theme: bool) -> list[dict]:
        query = "SELECT * FROM photos WHERE path LIKE ?"
        params: list = [folder_pattern]
        if with_theme and req.theme and req.theme != "otro":
            query += " AND theme = ?"
            params.append(req.theme)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows if r["id"] not in exclude]

    available = find_available(with_theme=True)
    if not available:
        available = find_available(with_theme=False)  # relax theme filter
    if not available:
        # Last resort: ignore exclude_ids history (only keep current week)
        available = [
            dict(r) for r in conn.execute(
                "SELECT * FROM photos WHERE path LIKE ?", (folder_pattern,)
            ).fetchall()
            if r["id"] not in used_this_week
        ]
    if not available:
        raise HTTPException(404, "No hay más fotos disponibles para cambiar")

    import random
    new_photo = random.choice(available)

    conn.execute("""
        UPDATE weekly_selections SET photo_id = ?, cropped_path = NULL, text_overlay = NULL
        WHERE week_start = ? AND day_of_week = ?
    """, (new_photo["id"], req.week_start, req.day_of_week))
    conn.commit()
    conn.close()

    return new_photo


@app.get("/themes")
def list_themes(folder: str = ""):
    conn = get_conn()
    if folder.strip():
        pattern = folder.rstrip("/") + "/%"
        rows = conn.execute(
            "SELECT DISTINCT theme FROM photos WHERE theme IS NOT NULL AND path LIKE ? ORDER BY theme",
            (pattern,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT theme FROM photos WHERE theme IS NOT NULL ORDER BY theme"
        ).fetchall()
    conn.close()
    return [r["theme"] for r in rows]


@app.get("/index/preview")
def preview_index(folder_path: str):
    """Count photos to index and estimate cost — no AI calls."""
    return count_unindexed(folder_path)


@app.post("/auto-text")
def auto_text(req: PhraseRequest):
    """Generate a phrase with AI and apply it automatically to the story image."""
    conn = get_conn()
    sel = conn.execute("""
        SELECT ws.*, p.path, p.filename, p.theme, p.tags
        FROM weekly_selections ws JOIN photos p ON p.id = ws.photo_id
        WHERE ws.week_start = ? AND ws.day_of_week = ?
    """, (req.week_start, req.day_of_week)).fetchone()

    if not sel:
        raise HTTPException(404, "Selection not found")

    tags: list[str] = []
    try:
        tags = json.loads(sel["tags"] or "[]")
    except Exception:
        pass

    phrase = generate_phrase(sel["theme"] or "otro", tags, req.tone, req.topic)
    if not phrase:
        raise HTTPException(500, "No se pudo generar la frase")

    # Default style for auto-text
    style = {
        "position": req.text_position,
        "font": req.font,
        "font_size": 68,
        "color": "#FFFFFF",
        "bg_color": "#000000",
        "bg_opacity": 150,
        "align": "center",
        **req.style,
    }

    base = sel["cropped_path"] or sel["path"]
    # Always write to _final.jpg so ZIP export picks it up correctly
    out_name = f"w{req.week_start}_d{req.day_of_week}_final.jpg"
    out_path = add_text_overlay(base, phrase, style, out_name)

    conn.execute("""
        UPDATE weekly_selections SET text_overlay = ?, text_style = ?
        WHERE week_start = ? AND day_of_week = ?
    """, (phrase, json.dumps(style), req.week_start, req.day_of_week))
    conn.commit()
    conn.close()

    return {
        "phrase": phrase,
        "url": f"/outputs/{Path(out_path).name}",
    }


@app.get("/fonts")
def list_fonts():
    """Return available fonts with their names."""
    from cropper import FEATURED_FONTS, FONTS
    return [
        {"name": f, "path": FONTS[f]}
        for f in FEATURED_FONTS if f in FONTS
    ]


@app.get("/folder-picker")
def folder_picker():
    """Open native macOS folder picker dialog."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Selecciona la carpeta de fotos:")'],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise HTTPException(400, "Selección cancelada")
        path = result.stdout.strip()
        return {"path": path}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Timeout")


class AutoRunRequest(BaseModel):
    week_start: str
    tone: str = "inspirador"
    topics: list[str] = []
    day_themes: list[str] = []
    week_topic: str = ""
    text_position: str = "bottom"
    font: str = "Helvetica Neue"
    active_days: list[bool] = [True]*7
    folder: str = ""   # restrict photos to this folder


@app.post("/run-week")
def run_week(req: AutoRunRequest):
    """
    Full automated pipeline for a week:
    1. Generate photo selection (with per-day themes)
    2. Crop each photo to 9:16 (with EXIF auto-rotation)
    3. Generate AI phrase and apply text overlay
    Returns list of 7 finished stories.
    """
    import time

    DEFAULT_STYLE = {
        "position": req.text_position,
        "font": req.font,
        "font_size": 68,
        "color": "#FFFFFF",
        "bg_color": "#000000",
        "bg_opacity": 160,
        "align": "center",
    }

    active = req.active_days if len(req.active_days) == 7 else [True]*7
    themes = req.day_themes if any(req.day_themes) else None
    folder = req.folder.strip() or None
    selections = generate_weekly_plan(
        req.week_start, themes, force=True,
        folder=folder, active_days=active
    )

    results = []
    conn = get_conn()

    for idx, sel in enumerate(selections):
        day        = sel["day_of_week"]
        photo_id   = sel["photo_id"]
        photo_path = sel["path"]

        # ── 1. Crop to 9:16 ───────────────────────────────────────────────
        crop_name = f"w{req.week_start}_d{day}_story.jpg"
        try:
            cropped_path = smart_crop_to_story(photo_path, crop_name)
            conn.execute(
                "UPDATE weekly_selections SET cropped_path=? WHERE week_start=? AND day_of_week=?",
                (cropped_path, req.week_start, day),
            )
            conn.commit()
            print(f"  ✂ day {day} cropped → {crop_name}")
        except Exception as e:
            cropped_path = photo_path
            print(f"  [crop error day {day}] {e}")

        # ── 2. Resolve topic ──────────────────────────────────────────────
        topic = ""
        if req.topics and day < len(req.topics) and req.topics[day].strip():
            topic = req.topics[day].strip()
        elif req.week_topic.strip():
            topic = req.week_topic.strip()

        tags: list[str] = []
        try:
            tags = json.loads(sel.get("tags") or "[]")
        except Exception:
            pass

        # ── 3. Generate phrase (with gentle rate-limit delay) ─────────────
        if idx > 0:
            time.sleep(4.5)   # Gemini free tier: 15 RPM → wait ~4s between calls

        phrase = generate_phrase(sel.get("theme") or "otro", tags, req.tone, topic)
        print(f"  💬 day {day} phrase: {phrase!r}")

        # ── 4. Apply text overlay ─────────────────────────────────────────
        final_path = cropped_path
        if phrase:
            text_name = f"w{req.week_start}_d{day}_final.jpg"
            try:
                final_path = add_text_overlay(cropped_path, phrase, DEFAULT_STYLE, text_name)
                conn.execute(
                    "UPDATE weekly_selections SET text_overlay=?, text_style=? WHERE week_start=? AND day_of_week=?",
                    (phrase, json.dumps(DEFAULT_STYLE), req.week_start, day),
                )
                conn.commit()
                print(f"  🖼 day {day} text applied → {text_name}")
            except Exception as e:
                print(f"  [text error day {day}] {e}")

        results.append({
            "day":      day,
            "photo_id": photo_id,
            "theme":    sel.get("theme"),
            "phrase":   phrase,
            "has_text": bool(phrase),
            "url":      f"/outputs/{Path(final_path).name}",
            "filename": Path(final_path).name,
        })

    conn.close()
    return {"week_start": req.week_start, "stories": results}


@app.get("/export-week/{week_start}")
def export_week(week_start: str, save_to: str = ""):
    """
    Build a ZIP of all stories for the week.
    If save_to is provided, also copy files to that folder.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT day_of_week, cropped_path, text_overlay FROM weekly_selections WHERE week_start=? ORDER BY day_of_week",
        (week_start,)
    ).fetchall()
    conn.close()

    DAYS = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]

    # Collect existing files
    files: list[tuple[Path, str]] = []
    for row in rows:
        day = row["day_of_week"]
        arcname = f"{DAYS[day]}.jpg"
        # Prefer: final → text (from swap/re-phrase) → story (cropped only) → original cropped
        for candidate in [
            OUTPUTS / f"w{week_start}_d{day}_final.jpg",
            OUTPUTS / f"w{week_start}_d{day}_text.jpg",
            OUTPUTS / f"w{week_start}_d{day}_story.jpg",
            Path(row["cropped_path"]) if row["cropped_path"] else None,
        ]:
            if candidate and Path(candidate).exists():
                files.append((Path(candidate), arcname))
                break

    if not files:
        raise HTTPException(404, "No hay imágenes generadas para esta semana")

    # Always regenerate the ZIP (delete cached version first)
    zip_path = OUTPUTS / f"stories_{week_start}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in files:
            zf.write(src, arcname)

    # Optionally copy to user-chosen folder
    if save_to:
        dest_dir = Path(save_to)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src, arcname in files:
            shutil.copy2(src, dest_dir / arcname)

    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"stories_{week_start}.zip",
        headers={"Content-Disposition": f'attachment; filename="stories_{week_start}.zip"'},
    )


@app.get("/folder-picker-save")
def folder_picker_save():
    """Open native macOS folder picker to choose save destination."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "¿Dónde guardar las historias?")'],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise HTTPException(400, "Cancelado")
        return {"path": result.stdout.strip()}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Timeout")


@app.get("/photo-file/{photo_id}")
def serve_photo(photo_id: int):
    conn = get_conn()
    photo = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    conn.close()
    if not photo or not Path(photo["path"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(photo["path"])
