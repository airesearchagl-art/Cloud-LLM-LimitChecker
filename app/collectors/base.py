from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CollectedUsage:
    service_provider: str
    model_name: str
    limit_type: str
    used_value: float
    unit: str
    recorded_at: datetime
    source_type: str = "api"
    note: str | None = None


class UsageCollector(ABC):
    """公式API/管理API取得を差し替えるための境界。

    MVPでは具象クラスは雛形のみ。API仕様変更時はここを実装し直します。
    """

    @abstractmethod
    async def collect(self) -> list[CollectedUsage]:
        raise NotImplementedError
