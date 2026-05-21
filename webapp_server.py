import os
import time
import json
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


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


def yandex_headers() -> dict:
    if not YANDEX_DISK_TOKEN:
        raise HTTPException(status_code=500, detail="YANDEX_DISK_TOKEN is not set")
    return {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}


def ensure_yandex_folder(path: str) -> None:
    response = requests.put(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers=yandex_headers(),
        params={"path": path},
        timeout=60,
    )

    if response.status_code not in [200, 201, 409]:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot create Yandex.Disk folder: {response.text}",
        )


def get_yandex_upload_url(remote_path: str) -> str:
    response = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers=yandex_headers(),
        params={"path": remote_path, "overwrite": "true"},
        timeout=60,
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot get Yandex.Disk upload URL: {response.text}",
        )

    href = response.json().get("href")
    if not href:
        raise HTTPException(status_code=500, detail="Yandex.Disk did not return upload href")

    return href


def upload_to_yandex_disk(local_path: Path, remote_path: str) -> None:
    upload_url = get_yandex_upload_url(remote_path)

    with local_path.open("rb") as file:
        response = requests.put(upload_url, data=file, timeout=600)

    if response.status_code not in [200, 201, 202]:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot upload file to Yandex.Disk: {response.text}",
        )


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("static/upload.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    stage: str = Form(...),
):
    """
    stage:
    - locality
    - sales
    - stocks

    Возвращает JSON, который Mini App отправит боту через Telegram.WebApp.sendData().
    """
    if stage not in {"locality", "sales", "stocks"}:
        raise HTTPException(status_code=400, detail="Invalid stage")

    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()

    allowed_suffixes = {".xlsx", ".xls", ".csv"}
    if suffix not in allowed_suffixes:
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, .csv are allowed")

    temp_dir = Path("/tmp/marketcopilot_uploads")
    temp_dir.mkdir(parents=True, exist_ok=True)

    safe_user_id = "".join(ch for ch in str(user_id) if ch.isdigit()) or "unknown"
    timestamp = int(time.time())
    temp_path = temp_dir / f"{safe_user_id}_{stage}_{timestamp}{suffix}"

    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024

    with temp_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break

            size += len(chunk)
            if size > limit:
                temp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File is larger than {MAX_UPLOAD_MB} MB")

            out.write(chunk)

    ensure_yandex_folder(YANDEX_UPLOAD_FOLDER)

    remote_path = f"{YANDEX_UPLOAD_FOLDER}/{safe_user_id}_{stage}_{timestamp}{suffix}"
    upload_to_yandex_disk(temp_path, remote_path)

    temp_path.unlink(missing_ok=True)

    payload = {
        "type": "yandex_disk_upload",
        "stage": stage,
        "user_id": safe_user_id,
        "filename": filename,
        "remote_path": remote_path,
        "size_bytes": size,
    }

    return JSONResponse(payload)


@app.get("/health")
async def health():
    return {"ok": True}
