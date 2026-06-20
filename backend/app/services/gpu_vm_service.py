import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GpuVmService:
    def __init__(self, project_id: Optional[str], zone: Optional[str], instance_name: Optional[str]):
        self._project = project_id
        self._zone = zone
        self._instance = instance_name

    @property
    def configured(self) -> bool:
        return bool(self._project and self._zone and self._instance)

    async def status(self) -> dict:
        if not self.configured:
            return {"configured": False, "online": False}
        try:
            from google.cloud import compute_v1  # type: ignore
            client = compute_v1.InstancesClient()
            inst = await asyncio.to_thread(
                client.get,
                project=self._project,
                zone=self._zone,
                instance=self._instance,
            )
            return {"configured": True, "online": inst.status == "RUNNING", "vm_status": inst.status}
        except Exception as e:
            logger.error(f"GPU VM status error: {e}")
            return {"configured": True, "online": False, "error": str(e)}

    async def start(self) -> dict:
        if not self.configured:
            raise ValueError("GPU VM이 설정되지 않았습니다")
        from google.cloud import compute_v1  # type: ignore
        client = compute_v1.InstancesClient()
        op = await asyncio.to_thread(
            client.start,
            project=self._project, zone=self._zone, instance=self._instance,
        )
        return {"message": "시작 요청됨", "operation": op.name}

    async def stop(self) -> dict:
        if not self.configured:
            raise ValueError("GPU VM이 설정되지 않았습니다")
        from google.cloud import compute_v1  # type: ignore
        client = compute_v1.InstancesClient()
        op = await asyncio.to_thread(
            client.stop,
            project=self._project, zone=self._zone, instance=self._instance,
        )
        return {"message": "중지 요청됨", "operation": op.name}
