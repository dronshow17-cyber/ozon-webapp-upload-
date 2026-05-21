import os
import time
from pathlib import Path

import requests
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
YANDEX_UPLOAD_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "/MarketCopilotUploads")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

app = FastAPI(title="MarketCopilot Upload WebApp")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def log(message: str):
    print(f"[WEBAPP] {message}", flush=True)


def yandex_headers() -> dict:
    if not YANDEX_DISK_TOKEN:
        raise HTTPException(status_code=500, detail="YANDEX_DISK_TOKEN is not set")

    return {
        "Authorization": f"OAuth {YANDEX_DISK_TOKEN}"
    }


def normalize_stage(stage: str = None, step: str = None) -> str:
    """
    Поддерживаем оба варианта с фронта:
    - stage=locality/sales/stocks
    - step=1/2/3
    """
    value = (stage or step or "").strip().lower()

    mapping = {
        "1": "locality",
        "2": "sales",
        "3": "stocks",
        "locality": "locality",
        "sales": "sales",
        "stocks": "stocks",
    }

    normalized = mapping.get(value)

    if not normalized:
        raise HTTPException(
            status_code=400,
            detail="Invalid stage. Expected stage=locality/sales/stocks or step=1/2/3"
        )

    return normalized


def ensure_yandex_folder(path: str) -> None:
    log(f"ensure folder: {path}")

    response = requests.put(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers=yandex_headers(),
        params={"path": path},
        timeout=30,
    )

    log(f"ensure folder response: {response.status_code}")

    if response.status_code not in [200, 201, 409]:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot create Yandex.Disk folder: {response.text}",
        )


def get_yandex_upload_url(remote_path: str) -> str:
    log(f"get upload url for: {remote_path}")

    response = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers=yandex_headers(),
        params={"path": remote_path, "overwrite": "true"},
        timeout=30,
    )

    log(f"get upload url response: {response.status_code}")

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot get Yandex.Disk upload URL: {response.text}",
        )

    href = response.json().get("href")

    if not href:
        raise HTTPException(
            status_code=500,
            detail="Yandex.Disk did not return upload href"
        )

    return href


def upload_to_yandex_disk(local_path: Path, remote_path: str) -> None:
    file_size_mb = round(local_path.stat().st_size / 1024 / 1024, 2)
    log(f"start upload to yandex: {remote_path}, size={file_size_mb} MB")

    upload_url = get_yandex_upload_url(remote_path)

    with local_path.open("rb") as file:
        response = requests.put(
            upload_url,
            data=file,
            timeout=300,
        )

    log(f"upload to yandex response: {response.status_code}")

    if response.status_code not in [200, 201, 202]:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot upload file to Yandex.Disk: {response.text}",
        )

    log("upload to yandex complete")


@app.get("/", response_class=HTMLResponse)
async def index():
    upload_html_path = STATIC_DIR / "upload.html"

    if not upload_html_path.exists():
        return HTMLResponse(
            "<h3>Upload page not found</h3>"
            "<p>Проверь, что файл лежит здесь: static/upload.html</p>",
            status_code=500
        )

    html = upload_html_path.read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "static_dir": str(STATIC_DIR),
        "upload_html_exists": (STATIC_DIR / "upload.html").exists(),
        "yandex_token_exists": bool(YANDEX_DISK_TOKEN),
        "yandex_upload_folder": YANDEX_UPLOAD_FOLDER,
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    stage: str = Form(None),
    step: str = Form(None),
):
    """
    Принимает файл из Telegram WebApp.

    Поддерживает два варианта формы:
    - stage=locality/sales/stocks
    - step=1/2/3

    Возвращает payload, который upload.html отправляет обратно боту через tg.sendData().
    """
    normalized_stage = normalize_stage(stage=stage, step=step)

    log(
        f"POST /upload started: "
        f"user_id={user_id}, stage={normalized_stage}, filename={file.filename}"
    )

    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()

    allowed_suffixes = {".xlsx", ".xls", ".csv"}

    if suffix not in allowed_suffixes:
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx, .xls, .csv are allowed"
        )

    temp_dir = Path("/tmp/marketcopilot_uploads")
    temp_dir.mkdir(parents=True, exist_ok=True)

    safe_user_id = "".join(ch for ch in str(user_id) if ch.isdigit()) or "unknown"
    timestamp = int(time.time())

    temp_path = temp_dir / f"{safe_user_id}_{normalized_stage}_{timestamp}{suffix}"

    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024

    log(f"start saving temp file: {temp_path}")

    try:
        with temp_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > limit:
                    temp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is larger than {MAX_UPLOAD_MB} MB"
                    )

                out.write(chunk)

        size_mb = round(size / 1024 / 1024, 2)
        log(f"temp file saved: {temp_path}, size={size_mb} MB")

        ensure_yandex_folder(YANDEX_UPLOAD_FOLDER)

        remote_path = f"{YANDEX_UPLOAD_FOLDER}/{safe_user_id}_{normalized_stage}_{timestamp}{suffix}"

        upload_to_yandex_disk(temp_path, remote_path)

        payload = {
            "type": "yandex_disk_upload",
            "stage": normalized_stage,
            "user_id": safe_user_id,
            "filename": filename,
            "remote_path": remote_path,
            "size_bytes": size,
        }

        log(f"POST /upload complete: {payload}")

        return JSONResponse(payload)

    except HTTPException:
        raise

    except Exception as error:
        log(f"POST /upload error: {repr(error)}")
        raise HTTPException(
            status_code=500,
            detail=f"Upload failed: {error}"
        )

    finally:
        try:
            temp_path.unlink(missing_ok=True)
            log(f"temp file removed: {temp_path}")
        except Exception as cleanup_error:
            log(f"temp cleanup error: {repr(cleanup_error)}")
