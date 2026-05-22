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
YANDEX_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "MarketCopilotUploads")

os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.get("/")
async def index():
    return FileResponse("static/upload.html")


def yadisk_headers():
    return {"Authorization": f"OAuth {YANDEX_TOKEN}"}


def create_yadisk_folder():
    response = requests.put(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers=yadisk_headers(),
        params={"path": YANDEX_FOLDER},
        timeout=30,
    )

    print(f"[WEBAPP] create folder response: {response.status_code}", flush=True)

    if response.status_code not in (201, 409):
        raise Exception(f"Create folder failed: {response.status_code} {response.text}")


def get_yadisk_upload_url(remote_path: str):
    response = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers=yadisk_headers(),
        params={
            "path": remote_path,
            "overwrite": "true",
        },
        timeout=30,
    )

    print(f"[WEBAPP] upload url status: {response.status_code}", flush=True)

    response.raise_for_status()
    return response.json()["href"]


@app.post("/get-upload-url")
async def get_upload_url(
    filename: str = Form(...),
    stage: str = Form(...),
    user_id: str = Form("0"),
):
    try:
        print(
            f"[WEBAPP] get upload url: filename={filename}, stage={stage}, user_id={user_id}",
            flush=True,
        )

        create_yadisk_folder()

        safe_filename = filename.replace("/", "_").replace("\\", "_")
        timestamp = int(time.time())
        saved_name = f"{user_id}_{stage}_{timestamp}_{safe_filename}"

        remote_path = f"{YANDEX_FOLDER}/{saved_name}"
        upload_url = get_yadisk_upload_url(remote_path)

        return {
            "success": True,
            "upload_url": upload_url,
            "remote_path": remote_path,
            "filename": filename,
            "stage": stage,
            "user_id": user_id,
        }

    except Exception as e:
        print(f"[WEBAPP] GET UPLOAD URL ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
            },
        )


def upload_file_to_yadisk(local_path: str, remote_path: str):
    size = os.path.getsize(local_path)

    print(
        f"[WEBAPP] fallback upload to yadisk: {remote_path}, size={size / 1024 / 1024:.2f} MB",
        flush=True,
    )

    create_yadisk_folder()
    upload_url = get_yadisk_upload_url(remote_path)

    print("[WEBAPP] fallback start PUT to yadisk", flush=True)

    with open(local_path, "rb") as f:
        file_bytes = f.read()

    response = requests.put(
        upload_url,
        files={
            "file": (
                os.path.basename(local_path),
                file_bytes,
                "application/octet-stream",
            )
        },
        timeout=(30, 600),
    )

    print(f"[WEBAPP] fallback upload finished: {response.status_code}", flush=True)

    if response.status_code not in (200, 201, 202):
        print(f"[WEBAPP] fallback upload error body: {response.text[:500]}", flush=True)
        raise Exception(f"Yandex upload failed: {response.status_code}")

    print("[WEBAPP] fallback SUCCESS uploaded to Yandex Disk", flush=True)


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    stage: str = Form(...),
    user_id: str = Form("0"),
):
    temp_path = None

    try:
        print(
            f"[WEBAPP] fallback upload started: filename={file.filename}, stage={stage}, user_id={user_id}",
            flush=True,
        )

        safe_filename = file.filename.replace("/", "_").replace("\\", "_")
        timestamp = int(time.time())
        saved_name = f"{user_id}_{stage}_{timestamp}_{safe_filename}"

        temp_path = os.path.join(UPLOAD_DIR, saved_name)

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        size = os.path.getsize(temp_path)

        print(f"[WEBAPP] fallback temp file saved: {temp_path}", flush=True)
        print(f"[WEBAPP] fallback size: {size / 1024 / 1024:.2f} MB", flush=True)

        remote_path = f"{YANDEX_FOLDER}/{saved_name}"

        upload_file_to_yadisk(temp_path, remote_path)

        return {
            "success": True,
            "type": "fallback_yandex_disk_upload",
            "stage": stage,
            "user_id": user_id,
            "filename": file.filename,
            "remote_path": remote_path,
            "size_bytes": size,
        }

    except Exception as e:
        print(f"[WEBAPP] FALLBACK UPLOAD ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
            },
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            print(f"[WEBAPP] fallback temp file removed: {temp_path}", flush=True)


@app.post("/process")
async def process_files():
    try:
        print("[WEBAPP] process started", flush=True)

        return {
            "success": True,
            "message": "Process endpoint works. Real XLSX generation will be added next.",
            "download_url": "https://disk.yandex.ru/",
        }

    except Exception as e:
        print(f"[WEBAPP] PROCESS ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
            },
        )
