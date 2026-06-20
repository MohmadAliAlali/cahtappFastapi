import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/api/v1")


class FileUploadResponse(BaseModel):
    file_url: str
    file_name: str
    file_type: str
    file_size: int


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file provided")

    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Max size is {settings.MAX_UPLOAD_SIZE // (1024*1024)}MB",
        )

    upload_dir = settings.UPLOAD_DIR
    os.makedirs(upload_dir, exist_ok=True)

    ext = ""
    if "." in file.filename:
        ext = file.filename.rsplit(".", 1)[1]
    safe_name = f"{uuid.uuid4().hex}_{uuid.uuid4().hex[:8]}"
    if ext:
        safe_name = f"{safe_name}.{ext}"

    file_path = os.path.join(upload_dir, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)

    file_url = f"/uploads/{safe_name}"
    return FileUploadResponse(
        file_url=file_url,
        file_name=file.filename,
        file_type=file.content_type or "application/octet-stream",
        file_size=len(content),
    )
