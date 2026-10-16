"""Persists the indexer watermark (start time of the last successful run) in blob storage."""

import json
from datetime import datetime

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

STATE_BLOB = "watermark.json"


class WatermarkStore:
    def __init__(self, account_url: str, container: str, credential: TokenCredential) -> None:
        self._container = BlobServiceClient(account_url, credential=credential).get_container_client(container)

    def load(self) -> datetime | None:
        try:
            data = json.loads(self._container.download_blob(STATE_BLOB).readall())
        except ResourceNotFoundError:
            return None
        return datetime.fromisoformat(data["lastRunStart"])

    def save(self, run_start: datetime) -> None:
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass
        self._container.upload_blob(STATE_BLOB, json.dumps({"lastRunStart": run_start.isoformat()}), overwrite=True)
