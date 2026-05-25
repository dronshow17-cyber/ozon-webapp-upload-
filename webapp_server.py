import os
import time
import uuid
import shutil
import zipfile
import requests

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from openpyxl import Workbook


app = FastAPI()

WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN") or os.getenv("YANDEX_DISK_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN")

UPLOAD_DIR = "/tmp/marketcopilot_uploads"
YANDEX_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "MarketCopilotUploads")

ALLOWED_MARKETPLACES = {"ozon", "wb"}
ALLOWED_STAGES = {"locality", "sales", "stocks", "wb_sales", "wb_stocks"}
ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".zip"}
MAX_FILE_SIZE_MB = 150
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

allowed_origins = ["*"]
if WEBAPP_URL.startswith("https://"):
    allowed_origins = [WEBAPP_URL]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(UPLOAD_DIR, exist_ok=True)

UPLOAD_REGISTRY = {}


@app.get("/")
async def index():
    return FileResponse(
        "static/upload.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def yadisk_headers():
    if not YANDEX_TOKEN:
        raise Exception("YANDEX_TOKEN / YANDEX_DISK_TOKEN is missing in Railway Variables")
    return {"Authorization": f"OAuth {YANDEX_TOKEN}"}


def safe_user_id(value: str) -> str:
    value = str(value or "").strip()
    if not value.isdigit() or value == "0":
        raise Exception("Некорректный пользователь. Откройте загрузку заново из бота.")
    return value


def safe_marketplace(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in ALLOWED_MARKETPLACES:
        raise Exception("Некорректный маркетплейс. Откройте загрузку заново из бота.")
    return value


def marketplace_for_stage(stage: str) -> str:
    return "wb" if stage in {"wb_sales", "wb_stocks"} else "ozon"


def safe_stage(value: str, marketplace: str) -> str:
    value = str(value or "").strip()
    marketplace = safe_marketplace(marketplace)

    if value not in ALLOWED_STAGES:
        raise Exception("Некорректный тип файла.")

    if marketplace == "wb" and value not in {"wb_sales", "wb_stocks"}:
        raise Exception("Этот файл не относится к сценарию Wildberries.")

    if marketplace == "ozon" and value not in {"locality", "sales", "stocks"}:
        raise Exception("Этот файл не относится к сценарию Ozon.")

    return value


def safe_session_id(value: str) -> str:
    value = str(value or "").strip()
    value = "".join(ch for ch in value if ch.isalnum() or ch in ["_", "-"])[:64]
    if not value:
        value = uuid.uuid4().hex[:16]
    return value


def safe_filename(filename: str) -> str:
    filename = os.path.basename(str(filename or "file"))
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = filename.replace("..", "_").strip()

    if not filename:
        filename = "file"

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise Exception("Недопустимый формат файла. Разрешены: xlsx, xls, csv, zip.")

    return filename


def validate_zip_contents(path: str):
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]

            if not infos:
                raise Exception("ZIP архив пустой.")

            if len(infos) > 10:
                raise Exception("В ZIP архиве слишком много файлов. Максимум: 10.")

            total_uncompressed_size = 0

            for info in infos:
                name = str(info.filename or "")
                normalized_name = name.replace("\\", "/")
                base_name = os.path.basename(normalized_name)

                if not base_name:
                    raise Exception("В ZIP архиве найден файл с некорректным именем.")

                if normalized_name.startswith("/") or "../" in normalized_name or normalized_name.startswith("../"):
                    raise Exception("В ZIP архиве найден небезопасный путь к файлу.")

                ext = os.path.splitext(base_name)[1].lower()
                if ext not in {".csv", ".xlsx", ".xls"}:
                    raise Exception("В ZIP архиве разрешены только файлы csv, xlsx или xls.")

                total_uncompressed_size += int(info.file_size or 0)

                if total_uncompressed_size > MAX_FILE_SIZE_BYTES:
                    raise Exception(f"ZIP архив слишком большой после распаковки. Максимум: {MAX_FILE_SIZE_MB} МБ.")

    except zipfile.BadZipFile:
        raise Exception("Файл ZIP повреждён или имеет неверный формат.")


def create_yadisk_folder(path: str):
    response = requests.put(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers=yadisk_headers(),
        params={"path": path},
        timeout=30,
    )

    print(f"[WEBAPP] create folder {path}: {response.status_code}", flush=True)

    if response.status_code not in (201, 409):
        raise Exception(f"Create folder failed: {response.status_code} {response.text[:500]}")


def ensure_yadisk_path(user_id: str, session_id: str):
    create_yadisk_folder(YANDEX_FOLDER)
    create_yadisk_folder(f"{YANDEX_FOLDER}/{user_id}")
    create_yadisk_folder(f"{YANDEX_FOLDER}/{user_id}/{session_id}")


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


def upload_file_to_yadisk(local_path: str, remote_path: str):
    upload_url = get_yadisk_upload_url(remote_path)

    print(f"[WEBAPP] start PUT to yadisk: {remote_path}", flush=True)

    with open(local_path, "rb") as file:
        response = requests.put(
            upload_url,
            data=file,
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
    user_id: str = Form("0"),
    session_id: str = Form(""),
    marketplace: str = Form(""),
):
    temp_path = None

    try:
        user_id = safe_user_id(user_id)
        session_id = safe_session_id(session_id)

        if not marketplace:
            marketplace = marketplace_for_stage(stage)

        marketplace = safe_marketplace(marketplace)
        stage = safe_stage(stage, marketplace)
        filename = safe_filename(file.filename)

        user_temp_dir = os.path.join(UPLOAD_DIR, user_id, session_id)
        os.makedirs(user_temp_dir, exist_ok=True)

        saved_name = f"{stage}_{int(time.time())}_{filename}"
        temp_path = os.path.join(user_temp_dir, saved_name)

        size = 0
        with open(temp_path, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size += len(chunk)
                if size > MAX_FILE_SIZE_BYTES:
                    raise Exception(f"Файл слишком большой. Максимум: {MAX_FILE_SIZE_MB} МБ.")

                buffer.write(chunk)

        if filename.lower().endswith(".zip"):
            validate_zip_contents(temp_path)

        ensure_yadisk_path(user_id, session_id)

        remote_path = f"{YANDEX_FOLDER}/{user_id}/{session_id}/{saved_name}"
        upload_file_to_yadisk(temp_path, remote_path)

        UPLOAD_REGISTRY.setdefault(user_id, {})
        UPLOAD_REGISTRY[user_id][stage] = {
            "marketplace": marketplace,
            "filename": filename,
            "remote_path": remote_path,
            "uploaded_at": int(time.time()),
            "session_id": session_id,
        }

        return {
            "success": True,
            "type": "yandex_disk_upload",
            "marketplace": marketplace,
            "stage": stage,
            "user_id": user_id,
            "session_id": session_id,
            "filename": filename,
            "remote_path": remote_path,
            "size_bytes": size,
            "uploaded_stages": list(UPLOAD_REGISTRY[user_id].keys()),
        }

    except Exception as e:
        print(f"[WEBAPP] ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def create_result_xlsx(user_id: str):
    result_path = os.path.join(UPLOAD_DIR, f"marketcopilot_result_{user_id}_{int(time.time())}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "MarketCopilot"

    ws.append(["MarketCopilot report"])
    ws.append(["Статус", "Готовый файл сформирован"])
    ws.append(["User ID", user_id])
    ws.append([])
    ws.append(["Следующий этап", "готовый отчёт формирует Telegram-бот после получения файлов"])

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
async def process_files(
    user_id: str = Form("0"),
    session_id: str = Form(""),
):
    try:
        user_id = safe_user_id(user_id)
        session_id = safe_session_id(session_id)

        return {
            "success": True,
            "message": "Файлы приняты. Готовый отчёт будет сформирован ботом.",
            "user_id": user_id,
            "session_id": session_id,
        }

    except Exception as e:
        print(f"[WEBAPP] PROCESS ERROR: {str(e)}", flush=True)

        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )
