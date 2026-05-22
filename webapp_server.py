import os
import time
import shutil
import requests

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
UPLOAD_DIR = "/tmp/marketcopilot_uploads"
YANDEX_FOLDER = "MarketCopilotUploads"

os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.get("/")
async def index():
    return FileResponse("static/upload.html")


def yadisk_headers():
    return {
        "Authorization": f"OAuth {YANDEX_TOKEN}"
    }


def create_yadisk_folder():
    url = "https://cloud-api.yandex.net/v1/disk/resources"

    response = requests.put(
        url,
        headers=yadisk_headers(),
        params={"path": YANDEX_FOLDER},
        timeout=30
    )

    print(f"[WEBAPP] create folder response: {response.status_code}", flush=True)

    if response.status_code not in (201, 409):
        raise Exception(
            f"Create folder failed: {response.status_code} {response.text}"
        )


def get_yadisk_upload_url(remote_path: str):
    url = "https://cloud-api.yandex.net/v1/disk/resources/upload"

    response = requests.get(
        url,
        headers=yadisk_headers(),
        params={
            "path": remote_path,
            "overwrite": "true"
        },
        timeout=30
    )

    print(f"[WEBAPP] upload url status: {response.status_code}", flush=True)

    response.raise_for_status()

    return response.json()["href"]


def upload_file_to_yadisk(local_path: str, remote_path: str):
    size = os.path.getsize(local_path)

    print(
        f"[WEBAPP] start upload to yadisk: {remote_path}, "
        f"size={size / 1024 / 1024:.2f} MB",
        flush=True
    )

    create_yadisk_folder()

    upload_url = get_yadisk_upload_url(remote_path)

    print("[WEBAPP] start PUT to yadisk", flush=True)

    with open(local_path, "rb") as f:
        file_bytes = f.read()

    response = requests.put(
        upload_url,
        data=file_bytes,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(file_bytes)),
            "Connection": "close",
        },
        timeout=(30, 600)
    )

    print(f"[WEBAPP] upload finished: {response.status_code}", flush=True)

    if response.status_code not in (200, 201, 202):
        print(
            f"[WEBAPP] upload error body: {response.text[:500]}",
            flush=True
        )

        raise Exception(
            f"Yandex upload failed: {response.status_code}"
        )

    print("[WEBAPP] SUCCESS uploaded to Yandex Disk", flush=True)


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    stage: str = Form(...),
    user_id: str = Form("0")
):
    temp_path = None

    try:
        print(
            f"[WEBAPP] upload started: "
            f"filename={file.filename}, "
            f"stage={stage}, "
            f"user_id={user_id}",
            flush=True
        )

        safe_filename = (
            file.filename
            .replace("/", "_")
            .replace("\\", "_")
        )

        timestamp = int(time.time())

        saved_name = (
            f"{user_id}_{stage}_{timestamp}_{safe_filename}"
        )

        temp_path = os.path.join(
            UPLOAD_DIR,
            saved_name
        )

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        size = os.path.getsize(temp_path)

        print(
            f"[WEBAPP] temp file saved: {temp_path}",
            flush=True
        )

        print(
            f"[WEBAPP] size: {size / 1024 / 1024:.2f} MB",
            flush=True
        )

        remote_path = (
            f"{YANDEX_FOLDER}/{saved_name}"
        )

        upload_file_to_yadisk(
            temp_path,
            remote_path
        )

        return {
            "success": True,
            "type": "yandex_disk_upload",
            "stage": stage,
            "user_id": user_id,
            "filename": file.filename,
            "remote_path": remote_path,
            "size_bytes": size
        }

    except Exception as e:
        print(
            f"[WEBAPP] ERROR: {str(e)}",
            flush=True
        )

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e)
            }
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

            print(
                f"[WEBAPP] temp file removed: {temp_path}",
                flush=True
            )


@app.post("/process")
async def process_files():
    try:
        print(
            "[WEBAPP] process started",
            flush=True
        )

        return {
            "success": True,
            "message": (
                "Process endpoint works. "
                "Real XLSX generation "
                "will be added next."
            ),
            "download_url": "https://disk.yandex.ru/"
        }

    except Exception as e:
        print(
            f"[WEBAPP] PROCESS ERROR: {str(e)}",
            flush=True
        )

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e)
            }
        )
