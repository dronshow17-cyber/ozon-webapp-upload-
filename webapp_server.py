import os
import time
import shutil
from pathlib import Path

import requests
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
YANDEX_UPLOAD_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "MarketCopilotUploads")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

TEMP_DIR = Path("/tmp/marketcopilot_uploads")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="MarketCopilot Upload WebApp")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def log(message: str):
    print(f"[WEBAPP] {message}", flush=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    upload_html = STATIC_DIR / "upload.html"

    if not upload_html.exists():
        return HTMLResponse(
            "<h3>upload.html not found</h3><p>Файл должен лежать: static/upload.html</p>",
            status_code=500,
        )

    return HTMLResponse(upload_html.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {
        "ok": True,
        "upload_html_exists": (STATIC_DIR / "upload.html").exists(),
        "yandex_token_exists": bool(YANDEX_DISK_TOKEN),
        "yandex_upload_folder": YANDEX_UPLOAD_FOLDER,
        "max_upload_mb": MAX_UPLOAD_MB,
    }


def yandex_headers():
    if not YANDEX_DISK_TOKEN:
        raise RuntimeError("YANDEX_DISK_TOKEN is empty")

    return {
        "Authorization": f"OAuth {YANDEX_DISK_TOKEN}"
    }


def create_yandex_folder():
    log(f"create folder: {YANDEX_UPLOAD_FOLDER}")

    response = requests.put(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers=yandex_headers(),
        params={"path": YANDEX_UPLOAD_FOLDER},
        timeout=(10, 30),
    )

    log(f"create folder response: {response.status_code}")

    # 201 created, 409 already exists
    if response.status_code not in (200, 201, 409):
        raise RuntimeError(f"Yandex folder error: {response.status_code} {response.text}")


def get_yandex_upload_href(remote_path: str):
    log(f"get upload url: {remote_path}")

    response = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers=yandex_headers(),
        params={
            "path": remote_path,
            "overwrite": "true",
        },
        timeout=(10, 30),
    )

    log(f"upload url status: {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(f"Yandex upload URL error: {response.status_code} {response.text}")

    data = response.json()
    href = data.get("href")

    if not href:
        raise RuntimeError(f"Yandex did not return href: {data}")

    return href


def upload_file_to_yandex(local_path: Path, remote_path: str):
    file_size_mb = round(local_path.stat().st_size / 1024 / 1024, 2)
    log(f"start upload to yadisk: {remote_path}, size={file_size_mb} MB")

    create_yandex_folder()

    href = get_yandex_upload_href(remote_path)

    # ВАЖНО:
    # Для upload href Яндекс.Диска файл нужно отправлять как raw body через data=f.
    # Нельзя использовать files={"file": f}, иначе запрос может зависать/ломаться.
    with local_path.open("rb") as f:
        response = requests.put(
            href,
            data=f,
            headers={
                "Content-Type": "application/octet-stream"
            },
            timeout=(10, 600),
        )

    log(f"upload finished: {response.status_code}")

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"Yandex upload error: {response.status_code} {response.text}")

    log("SUCCESS uploaded to Yandex Disk")


def normalize_stage(stage: str = None, step: str = None):
    value = str(stage or step or "").strip().lower()

    mapping = {
        "1": "locality",
        "2": "sales",
        "3": "stocks",
        "locality": "locality",
        "sales": "sales",
        "stocks": "stocks",
    }

    result = mapping.get(value)

    if not result:
        raise RuntimeError("Invalid stage. Expected: locality/sales/stocks or 1/2/3")

    return result


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    stage: str = Form(None),
    step: str = Form(None),
    user_id: str = Form("0"),
):
    local_path = None

    try:
        stage = normalize_stage(stage=stage, step=step)

        filename = file.filename or "upload"
        suffix = Path(filename).suffix.lower()

        if suffix not in {".xlsx", ".xls", ".csv"}:
            return JSONResponse(
                {"success": False, "message": "Разрешены только .xlsx, .xls, .csv"},
                status_code=400,
            )

        safe_user_id = "".join(ch for ch in str(user_id) if ch.isdigit()) or "0"
        timestamp = int(time.time())

        safe_filename = f"{safe_user_id}_{stage}_{timestamp}{suffix}"
        local_path = TEMP_DIR / safe_filename

        log(f"upload started: filename={filename}, stage={stage}, user_id={safe_user_id}")

        with local_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        file_size = local_path.stat().st_size
        file_size_mb = round(file_size / 1024 / 1024, 2)

        log(f"temp file saved: {local_path}")
        log(f"size: {file_size_mb} MB")

        if file_size > MAX_UPLOAD_MB * 1024 * 1024:
            raise RuntimeError(f"Файл больше лимита {MAX_UPLOAD_MB} MB")

        remote_path = f"{YANDEX_UPLOAD_FOLDER}/{safe_filename}"

        upload_file_to_yandex(local_path, remote_path)

        payload = {
            "type": "yandex_disk_upload",
            "stage": stage,
            "user_id": safe_user_id,
            "filename": filename,
            "remote_path": remote_path,
            "size_bytes": file_size,
            "success": True,
        }

        log(f"POST /upload complete: {payload}")

        return JSONResponse(payload)

    except Exception as error:
        log(f"ERROR: {repr(error)}")

        return JSONResponse(
            {
                "success": False,
                "message": str(error),
            },
            status_code=500,
        )

    finally:
        if local_path:
            try:
                local_path.unlink(missing_ok=True)
                log(f"temp file removed: {local_path}")
            except Exception as cleanup_error:
                log(f"cleanup error: {repr(cleanup_error)}")
