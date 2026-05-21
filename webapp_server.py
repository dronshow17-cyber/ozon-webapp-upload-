import os
import time
import shutil
import requests

from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
YANDEX_UPLOAD_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "MarketCopilotUploads")

TEMP_DIR = "/tmp/marketcopilot_uploads"

os.makedirs(TEMP_DIR, exist_ok=True)

app = FastAPI()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(STATIC_DIR / "upload.html", "r", encoding="utf-8") as f:
        return f.read()


def upload_to_yandex(local_path: str, remote_path: str):
    headers = {
        "Authorization": f"OAuth {YANDEX_DISK_TOKEN}"
    }

    print(f"[WEBAPP] create folder: {YANDEX_UPLOAD_FOLDER}")

    folder_url = "https://cloud-api.yandex.net/v1/disk/resources"

    try:
        requests.put(
            folder_url,
            headers=headers,
            params={"path": YANDEX_UPLOAD_FOLDER},
            timeout=20
        )
    except Exception as e:
        print("[WEBAPP] folder create error:", e)

    print(f"[WEBAPP] get upload url: {remote_path}")

    upload_url_resp = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers=headers,
        params={
            "path": remote_path,
            "overwrite": "true"
        },
        timeout=30
    )

    print("[WEBAPP] upload url status:", upload_url_resp.status_code)

    upload_data = upload_url_resp.json()

    upload_url = upload_data.get("href")

    if not upload_url:
        raise Exception(f"YANDEX ERROR: {upload_data}")

    print("[WEBAPP] start upload to yadisk")

    with open(local_path, "rb") as f:
        upload_resp = requests.put(
            upload_url,
            files={"file": f},
            timeout=600
        )

    print("[WEBAPP] upload finished:", upload_resp.status_code)

    return upload_resp.status_code in [200, 201, 202]


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    stage: str = Form(...)
):
    try:
        print(f"[WEBAPP] upload started: {file.filename}")

        timestamp = int(time.time())

        local_filename = f"{timestamp}_{file.filename}"

        local_path = os.path.join(TEMP_DIR, local_filename)

        with open(local_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        file_size = os.path.getsize(local_path) / 1024 / 1024

        print(f"[WEBAPP] temp file saved: {local_path}")
        print(f"[WEBAPP] size: {file_size:.2f} MB")

        remote_path = f"{YANDEX_UPLOAD_FOLDER}/{local_filename}"

        success = upload_to_yandex(local_path, remote_path)

        os.remove(local_path)

        if success:
            print("[WEBAPP] SUCCESS")

            return JSONResponse({
                "success": True,
                "message": "Файл загружен"
            })

        return JSONResponse({
            "success": False,
            "message": "Ошибка загрузки"
        })

    except Exception as e:
        print("[WEBAPP] ERROR:", str(e))

        return JSONResponse({
            "success": False,
            "message": str(e)
        })
