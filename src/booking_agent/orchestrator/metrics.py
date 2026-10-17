from dataclasses import dataclass, field


@dataclass
class SupplierMetrics:
    supplier_id: str
    search_count: int = 0
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    total_offers_returned: int = 0
    win_count: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        denom = self.success_count + self.error_count + self.timeout_count
        return self.error_count / denom if denom else 0.0

    @property
    def timeout_rate(self) -> float:
        return self.timeout_count / self.search_count if self.search_count else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0


class MetricsCollector:
    def __init__(self) -> None:
        self._data: dict[str, SupplierMetrics] = {}

    def _get(self, supplier_id: str) -> SupplierMetrics:
        if supplier_id not in self._data:
            self._data[supplier_id] = SupplierMetrics(supplier_id=supplier_id)
        return self._data[supplier_id]

    def record_search_started(self, supplier_id: str) -> None:
        self._get(supplier_id).search_count += 1

    def record_success(self, supplier_id: str, latency_ms: float, offers_count: int) -> None:
        m = self._get(supplier_id)
        m.success_count += 1
        m.total_offers_returned += offers_count
        m.latencies_ms.append(latency_ms)

    def record_error(self, supplier_id: str) -> None:
        self._get(supplier_id).error_count += 1

    def record_timeout(self, supplier_id: str) -> None:
        self._get(supplier_id).timeout_count += 1

    def record_win(self, supplier_id: str) -> None:
        self._get(supplier_id).win_count += 1

    def snapshot(self) -> dict[str, SupplierMetrics]:
        return dict(self._data)
