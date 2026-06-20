import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GCSService:
    def __init__(self, bucket_name: str, credentials_path: Optional[str] = None):
        from google.cloud import storage

        if credentials_path:
            self._client = storage.Client.from_service_account_json(credentials_path)
        else:
            self._client = storage.Client()  # ADC (Application Default Credentials)
        self._bucket = self._client.bucket(bucket_name)

    async def upload(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        def _upload():
            blob = self._bucket.blob(path)
            blob.upload_from_string(data, content_type=content_type)
            return f"gs://{self._bucket.name}/{path}"

        return await asyncio.to_thread(_upload)

    async def download(self, path: str) -> Optional[bytes]:
        def _download():
            blob = self._bucket.blob(path)
            return blob.download_as_bytes() if blob.exists() else None

        return await asyncio.to_thread(_download)

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(self._bucket.blob(path).exists)
