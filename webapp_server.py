import os
import time
import uuid
import shutil
import zipfile
import requests

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()

WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN") or os.getenv("YANDEX_DISK_TOKEN")
UPLOAD_DIR = "/tmp/marketcopilot_uploads"
YANDEX_FOLDER = os.getenv("YANDEX_UPLOAD_FOLDER", "MarketCopilotUploads")

ALLOWED_STAGES = {"locality", "sales", "stocks", "wb_sales", "wb_stocks", "wb_buyout"}
ALLOWED_MARKETPLACES = {"ozon", "wb"}
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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

os.makedirs(UPLOAD_DIR, exist_ok=True)


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
        raise Exception("Storage token is not configured")
    return {"Authorization": f"OAuth {YANDEX_TOKEN}"}


def safe_user_id(value: str) -> str:
    value = str(value or "").strip()
    if not value.isdigit() or value == "0":
        raise Exception("Некорректный пользователь. Откройте загрузку заново из бота.")
    return value


def safe_marketplace(value: str) -> str:
    value = str(value or "ozon").strip().lower()
    if value not in ALLOWED_MARKETPLACES:
        value = "ozon"
    return value


def marketplace_for_stage(stage: str) -> str:
    return "wb" if stage in {"wb_sales", "wb_stocks", "wb_buyout"} else "ozon"


def safe_stage(value: str, marketplace: str = None) -> str:
    value = str(value or "").strip()
    marketplace = safe_marketplace(marketplace or marketplace_for_stage(value))

    if value not in ALLOWED_STAGES:
        raise Exception("Некорректный тип файла.")

    if marketplace == "wb" and value not in {"wb_sales", "wb_stocks", "wb_buyout"}:
        raise Exception("Этот файл не относится к сценарию Wildberries.")

    if marketplace == "ozon" and value not in {"locality", "sales", "stocks"}:
        raise Exception("Этот файл не относится к сценарию Ozon.")

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

    if response.status_code not in (201, 409):
        raise Exception("Не удалось подготовить место для загрузки.")


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

    response.raise_for_status()
    return response.json()["href"]


def upload_file_to_storage(local_path: str, remote_path: str):
    upload_url = get_yadisk_upload_url(remote_path)

    with open(local_path, "rb") as file:
        response = requests.put(
            upload_url,
            data=file,
            timeout=(30, 600),
        )

    if response.status_code not in (200, 201, 202):
        raise Exception("Не удалось загрузить файл.")


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    stage: str = Form(...),
    user_id: str = Form("0"),
    session_id: str = Form(""),
    marketplace: str = Form("ozon"),
):
    temp_path = None

    try:
        user_id = safe_user_id(user_id)
        marketplace = safe_marketplace(marketplace)
        stage = safe_stage(stage, marketplace)
        filename = safe_filename(file.filename)

        if not session_id:
            session_id = uuid.uuid4().hex[:16]
        else:
            session_id = str(session_id).strip()
            session_id = "".join(ch for ch in session_id if ch.isalnum() or ch in ["_", "-"])[:64]
            if not session_id:
                session_id = uuid.uuid4().hex[:16]

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
        upload_file_to_storage(temp_path, remote_path)

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
        }

    except Exception as e:
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


@app.post("/process")
async def process_files(
    user_id: str = Form("0"),
    session_id: str = Form(""),
):
    try:
        user_id = safe_user_id(user_id)

        session_id = str(session_id or "").strip()
        session_id = "".join(ch for ch in session_id if ch.isalnum() or ch in ["_", "-"])[:64]

        if not session_id:
            raise Exception("Сессия не найдена. Загрузите файлы заново из бота.")

        return {
            "success": True,
            "message": "Файлы приняты. Готовый отчёт будет сформирован ботом.",
            "user_id": user_id,
            "session_id": session_id,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
            },
        )
