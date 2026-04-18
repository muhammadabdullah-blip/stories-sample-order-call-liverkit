import os
import logging
from app.config import get_settings
from app.database import get_db

logger = logging.getLogger("storage")

async def upload_recording(call_id: str, file_path: str) -> str | None:
    """Uploads a local recording file to Supabase Storage and returns the public URL."""
    if not os.path.exists(file_path):
        logger.error(f"[{call_id}] Recording file not found: {file_path}")
        return None

    db = get_db()
    s = get_settings()
    bucket_name = s.supabase_storage_bucket
    
    # Check if bucket exists, create if not
    try:
        buckets = db.storage.list_buckets()
        if not any(b.name == bucket_name for b in buckets):
            db.storage.create_bucket(bucket_name, {"public": False})
            logger.info(f"Created Supabase storage bucket: {bucket_name}")
    except Exception as e:
        logger.warning(f"Could not verify/create bucket {bucket_name}. Proceeding anyway. Error: {e}")

    file_ext = os.path.splitext(file_path)[1]
    storage_path = f"{call_id}{file_ext}"

    try:
        with open(file_path, "rb") as f:
             db.storage.from_(bucket_name).upload(storage_path, f.read())
        logger.info(f"[{call_id}] Successfully uploaded {storage_path} to Supabase Storage")
        
        # We will create a signed URL that is valid for an extended period 
        # or we could make the bucket public. We default to returning a signed URL (valid for 10 years for simplicity here, though often one makes the bucket public or generates on read).
        # We'll just generate a very long-lived signed URL or you can use create_signed_url
        res = db.storage.from_(bucket_name).create_signed_url(storage_path, 60*60*24*365*10) # 10 years
        return res["signedURL"]
    except Exception as e:
        logger.error(f"[{call_id}] Failed to upload recording: {e}")
        return None
