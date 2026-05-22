import os
import time
import shutil
import zipfile
import requests

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from openpyxl import Workbook


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN")

UPLOAD_DIR = "/tmp/marketcopilot_uploads"
YANDEX_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "MarketCopilotUploads")

os.makedirs(UPLOAD_DIR, exist_ok=True)

UPLOAD_REGISTRY = {}


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
        params={"path": remote_path, "overwrite": "true"},
        timeout=30,
    )

    print(f"[WEBAPP] upload url status: {response.status_code}", flush=True)

    response.raise_for_status()
    return response.json()["href"]


def zip_if_csv(local_path: str):
    if not local_path.lower().endswith(".csv"):
        return local_path

    zip_path = local_path + ".zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(local_path, arcname=os.path.basename(local_path))

    print(
        f"[WEBAPP] csv zipped: {os.path.getsize(local_path)} -> {os.path.getsize(zip_path)} bytes",
        flush=True,
    )

    return zip_path


def upload_file_to_yadisk(local_path: str, remote_path: str):
    upload_url = get_yadisk_upload_url(remote_path)

    with open(local_path, "rb") as f:
        file_bytes = f.read()

    print(f"[WEBAPP] start PUT to yadisk: {remote_path}", flush=True)

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

    print(f"[WEBAPP] upload finished: {response.status_code}", flush=True)

    if response.status_code not in (200, 201, 202):
        raise Exception(f"Yandex upload failed: {response.status_code} {response.text[:500]}")

    print("[WEBAPP] SUCCESS uploaded to Yandex Disk", flush=True)


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    stage: str = Form(...),
    user_id: str = Form(...),
):
    temp_path = None
    upload_path = None

    try:
        create_yadisk_folder()

        safe_filename = file.filename.replace("/", "_").replace("\\", "_")
        timestamp = int(time.time())
        saved_name = f"{user_id}_{stage}_{timestamp}_{safe_filename}"

        temp_path = os.path.join(UPLOAD_DIR, saved_name)

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        upload_path = zip_if_csv(temp_path)

        remote_filename = os.path.basename(upload_path)
        remote_path = f"{YANDEX_FOLDER}/{remote_filename}"

        upload_file_to_yadisk(upload_path, remote_path)

        UPLOAD_REGISTRY.setdefault(user_id, {})
        UPLOAD_REGISTRY[user_id][stage] = {
            "filename": file.filename,
            "remote_path": remote_path,
            "uploaded_at": timestamp,
        }

        return {
            "success": True,
            "stage": stage,
            "user_id": user_id,
            "remote_path": remote_path,
            "uploaded_stages": list(UPLOAD_REGISTRY[user_id].keys()),
        }

    except Exception as e:
        print(f"[WEBAPP] ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )

    finally:
        for path in [temp_path, upload_path]:
            if path and os.path.exists(path):
                os.remove(path)


def create_result_xlsx(user_id: str):
    result_path = os.path.join(UPLOAD_DIR, f"marketcopilot_result_{user_id}_{int(time.time())}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "MarketCopilot"

    ws.append(["MarketCopilot report"])
    ws.append(["Статус", "Готовый файл сформирован"])
    ws.append(["User ID", user_id])
    ws.append([])
    ws.append(["Следующий этап", "сюда подключим реальную обработку продаж/остатков/локализации"])

    wb.save(result_path)
    return result_path


def send_document_to_telegram(chat_id: str, file_path: str):
    if not BOT_TOKEN:
        raise Exception("BOT_TOKEN is missing in Railway Variables")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    with open(file_path, "rb") as f:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "caption": "✅ Готовый файл MarketCopilot сформирован",
            },
            files={
                "document": (
                    os.path.basename(file_path),
                    f,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            timeout=(30, 600),
        )

    print(f"[WEBAPP] telegram sendDocument status: {response.status_code}", flush=True)

    if response.status_code != 200:
        raise Exception(f"Telegram sendDocument failed: {response.status_code} {response.text[:500]}")


@app.post("/process")
async def process_files(user_id: str = Form(...)):
    result_path = None

    try:
        user_files = UPLOAD_REGISTRY.get(user_id, {})

        required = ["locality", "sales", "stocks"]
        missing = [stage for stage in required if stage not in user_files]

        if missing:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Не все файлы загружены",
                    "missing": missing,
                },
            )

        result_path = create_result_xlsx(user_id)

        send_document_to_telegram(user_id, result_path)

        return {
            "success": True,
            "message": "Готовый файл отправлен в Telegram",
        }

    except Exception as e:
        print(f"[WEBAPP] PROCESS ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )

    finally:
        if result_path and os.path.exists(result_path):
            os.remove(result_path)
