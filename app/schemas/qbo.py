from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class QBOProxyResponse(BaseModel):
    client_id: str
    realm_id: str
    environment: str
    fetched_at: datetime
    latency_ms: float
    data: dict[str, Any]
    refreshed: Optional[bool] = None
