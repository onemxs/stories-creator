import json
import base64
import io
import os
import time
from pathlib import Path
from PIL import Image, ExifTags

from database import get_conn

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tiff"}

# ── AI provider selection ─────────────────────────────────────────────────────
# Uses Gemini if GEMINI_API_KEY is set, otherwise falls back to OpenAI.

def _get_gemini():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY", "")
    return genai.Client(api_key=key)

def _make_openai_client():
    from openai import OpenAI
    return OpenAI()

def _use_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())

# ── EXIF helpers ──────────────────────────────────────────────────────────────

def get_exif_date(img: Image.Image) -> str | None:
    try:
        exif_data = img._getexif()  # type: ignore
        if not exif_data:
            return None
        for tag_id, value in exif_data.items():
            tag = ExifTags.TAGS.get(tag_id, tag_id)
            if tag == "DateTimeOriginal":
                return value
    except Exception:
        pass
    return None


def guess_theme_from_path(path: Path) -> str:
    text = (path.parent.name + " " + path.stem).lower()
    mapping = {
        "naturaleza": ["nature", "natural", "bosque", "forest", "campo", "mountain", "montaña", "playa", "beach", "rio", "lake", "lago"],
        "ciudad": ["city", "ciudad", "urban", "calle", "street", "downtown"],
        "personas": ["people", "persona", "portrait", "retrato", "face", "cara", "familia", "family"],
        "comida": ["food", "comida", "cocina", "kitchen", "recipe", "receta"],
        "viaje": ["travel", "viaje", "trip", "tour", "vacation", "vacacion"],
        "arquitectura": ["arch", "building", "edificio", "house", "casa"],
        "atardecer/amanecer": ["sunset", "sunrise", "dawn", "atardecer", "amanecer"],
        "animales": ["animal", "pet", "mascota", "dog", "perro", "cat", "gato"],
        "arte": ["art", "arte", "design", "diseño", "creative"],
        "deporte": ["sport", "deporte", "gym", "fitness", "run"],
    }
    for theme, keywords in mapping.items():
        if any(k in text for k in keywords):
            return theme
    return "otro"


def encode_image_b64(path: str, max_size: int = 800) -> str:
    img = Image.open(path)
    img.thumbnail((max_size, max_size))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def load_image_bytes(path: str, max_size: int = 800) -> bytes:
    img = Image.open(path)
    img.thumbnail((max_size, max_size))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


THEMES = [
    "naturaleza", "ciudad", "personas", "comida", "viaje",
    "arquitectura", "arte", "deporte", "animales", "tecnología",
    "celebración", "trabajo", "familia", "atardecer/amanecer", "otro"
]

CLASSIFY_PROMPT = (
    "Clasifica esta foto. Responde SOLO con JSON válido, sin markdown:\n"
    '{{"theme": "<uno de: {themes}>", '
    '"confidence": <0.0-1.0>, '
    '"tags": ["tag1", "tag2", "tag3"]}}'
)


def classify_photo(path: str) -> dict:
    if _use_gemini():
        return _classify_gemini(path)
    return _classify_openai(path)


def _classify_gemini(path: str) -> dict:
    try:
        from google import genai
        from google.genai import types
        client = _get_gemini()
        img_bytes = load_image_bytes(path)
        prompt = CLASSIFY_PROMPT.format(themes=", ".join(THEMES))
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                prompt,
                types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            ],
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"  [gemini classify error] {e}")
        return {"theme": "otro", "confidence": 0.0, "tags": []}


def _classify_openai(path: str) -> dict:
    try:
        from openai import OpenAI
        client = OpenAI()
        b64 = encode_image_b64(path)
        prompt = CLASSIFY_PROMPT.format(themes=", ".join(THEMES))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}},
            ]}],
            max_tokens=120,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"  [openai classify error] {e}")
        return {"theme": "otro", "confidence": 0.0, "tags": []}


def generate_phrase(theme: str, tags: list[str], tone: str = "inspirador", topic: str = "") -> str:
    if _use_gemini():
        return _phrase_gemini(theme, tags, tone, topic)
    return _phrase_openai(theme, tags, tone, topic)


def _build_phrase_prompt(theme: str, tags: list[str], tone: str, topic: str) -> str:
    tags_str = ", ".join(tags) if tags else theme
    subject = f'El mensaje que quiero transmitir es: "{topic}".' if topic else f"El tema visual de la foto es: {theme} ({tags_str})."
    return (
        f"Crea una frase corta y {tone} para una historia de Instagram. "
        f"{subject} "
        f"La frase debe tener máximo 8 palabras, ser memorable, "
        f"sin hashtags ni emojis. Responde solo con la frase, nada más."
    )


def _phrase_gemini(theme: str, tags: list[str], tone: str, topic: str) -> str:
    try:
        client = _get_gemini()
        prompt = _build_phrase_prompt(theme, tags, tone, topic)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text.strip().strip('"').strip("'")
    except Exception as e:
        print(f"  [gemini phrase error] {e}")
        return ""


def _phrase_openai(theme: str, tags: list[str], tone: str, topic: str) -> str:
    try:
        from openai import OpenAI
        client = OpenAI()
        prompt = _build_phrase_prompt(theme, tags, tone, topic)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
        )
        return response.choices[0].message.content.strip().strip('"').strip("'")
    except Exception as e:
        print(f"  [openai phrase error] {e}")
        return ""


def mark_folder_indexed(folder_path: str) -> None:
    """Create a .indexed marker file in the folder to track that it was indexed."""
    folder = Path(folder_path)
    indexed_file = folder / ".indexed"
    indexed_file.write_text(str(int(time.time())))


def is_folder_indexed(folder_path: str) -> bool:
    """Check if folder has been indexed (has .indexed marker file)."""
    folder = Path(folder_path)
    return (folder / ".indexed").exists()


def count_unindexed(folder_path: str) -> dict:
    folder = Path(folder_path)
    if not folder.exists():
        return {"error": "Carpeta no encontrada"}
    files = [p for p in folder.rglob("*") if p.suffix.lower() in SUPPORTED_EXTS]
    conn = get_conn()
    folder_str = str(folder.resolve()).rstrip("/") + "/"
    # Photos already in DB whose path starts with this folder
    already_in_db = {
        r["path"] for r in conn.execute(
            "SELECT path FROM photos WHERE path LIKE ?", (folder_str + "%",)
        ).fetchall()
    }
    conn.close()
    file_paths = {str(f) for f in files}
    new_count = len(file_paths - already_in_db)
    already_count = len(file_paths & already_in_db)
    return {
        "total_in_folder": len(files),
        "already_indexed": already_count,
        "to_index": new_count,
        "is_folder_indexed": is_folder_indexed(folder_path),
        "estimated_cost_usd": 0.0 if _use_gemini() else round(new_count * 0.0015, 4),
        "provider": "Gemini (gratuito)" if _use_gemini() else "OpenAI GPT-4o-mini",
    }


def index_folder(folder_path: str, use_ai: bool = True, progress_cb=None) -> dict:
    folder = Path(folder_path)
    if not folder.exists():
        return {"error": f"Carpeta no encontrada: {folder_path}"}

    conn = get_conn()
    indexed = 0
    skipped = 0
    errors = 0

    folder_str = str(folder.resolve()).rstrip("/") + "/"
    photo_files = [p for p in folder.rglob("*") if p.suffix.lower() in SUPPORTED_EXTS]

    for photo_path in photo_files:
        path_str = str(photo_path)
        existing = conn.execute("SELECT id, source_folder FROM photos WHERE path = ?", (path_str,)).fetchone()
        if existing:
            # Already indexed — just update source_folder if it changed
            if existing["source_folder"] != folder_str:
                conn.execute("UPDATE photos SET source_folder = ? WHERE id = ?", (folder_str, existing["id"]))
                conn.commit()
            skipped += 1
            continue
        try:
            if progress_cb:
                progress_cb(current=photo_path.name)

            with Image.open(photo_path) as img:
                width, height = img.size
                aspect = round(width / height, 4)
                exif_date = get_exif_date(img)

            classification = (
                classify_photo(path_str) if use_ai
                else {"theme": guess_theme_from_path(photo_path), "confidence": 0.5, "tags": []}
            )

            conn.execute("""
                INSERT INTO photos (path, filename, width, height, aspect_ratio, theme, theme_confidence, tags, exif_date, source_folder)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                path_str, photo_path.name, width, height, aspect,
                classification["theme"], classification["confidence"],
                json.dumps(classification.get("tags", [])), exif_date,
                folder_str,
            ))
            conn.commit()
            indexed += 1
            if progress_cb:
                progress_cb(done=indexed)
            print(f"  ✓ {photo_path.name} → {classification['theme']}")

        except Exception as e:
            errors += 1
            if progress_cb:
                progress_cb(errors=errors)
            print(f"  ✗ {photo_path.name}: {e}")

    conn.close()
    # Mark folder as indexed
    mark_folder_indexed(folder_path)
    return {"indexed": indexed, "skipped": skipped, "errors": errors, "total_found": len(photo_files)}
