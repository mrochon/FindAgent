"""Persists the indexer watermark (start time and schema version of the last successful run) in blob storage."""

import json
from dataclasses import dataclass
from datetime import datetime

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

STATE_BLOB = "watermark.json"


@dataclass
class Watermark:
    last_run_start: datetime
    schema_version: int


class WatermarkStore:
    def __init__(self, account_url: str, container: str, credential: TokenCredential) -> None:
        self._container = BlobServiceClient(account_url, credential=credential).get_container_client(container)

    def load(self) -> Watermark | None:
        try:
            data = json.loads(self._container.download_blob(STATE_BLOB).readall())
        except ResourceNotFoundError:
            return None
        # Watermarks written before schema versioning existed are version 1.
        return Watermark(datetime.fromisoformat(data["lastRunStart"]), data.get("schemaVersion", 1))

    def save(self, run_start: datetime, schema_version: int) -> None:
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass
        data = {"lastRunStart": run_start.isoformat(), "schemaVersion": schema_version}
        self._container.upload_blob(STATE_BLOB, json.dumps(data), overwrite=True)
