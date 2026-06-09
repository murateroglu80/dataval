"""
Tüm modüllerin ortak sonuç veri modeli.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Status(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"
    TIMEOUT = "TIMEOUT"
    ERROR   = "ERROR"


# Rich terminal renkleri
STATUS_STYLE = {
    Status.PASS:    "bold green",
    Status.FAIL:    "bold red",
    Status.WARNING: "bold yellow",
    Status.SKIPPED: "dim",
    Status.TIMEOUT: "bold magenta",
    Status.ERROR:   "bold red",
}

STATUS_ICON = {
    Status.PASS:    "✅",
    Status.FAIL:    "❌",
    Status.WARNING: "⚠️ ",
    Status.SKIPPED: "⏭️ ",
    Status.TIMEOUT: "⏱️ ",
    Status.ERROR:   "💥",
}


@dataclass
class ValidationResult:
    module: str          # "inventory", "tables", "row_counts", ...
    schema: str          # source schema adı
    object_type: str     # "TABLE", "INDEX", "SEQUENCE", ...
    object_name: str     # obje adı
    status: Status
    source_value: Optional[str] = None
    target_value: Optional[str] = None
    note: Optional[str] = None


@dataclass
class ModuleSummary:
    module: str
    results: list[ValidationResult] = field(default_factory=list)

    def add(self, r: ValidationResult):
        self.results.append(r)

    @property
    def counts(self) -> dict:
        from collections import Counter
        c = Counter(r.status for r in self.results)
        return {s: c.get(s, 0) for s in Status}

    @property
    def passed(self)  -> bool:
        return not any(r.status in (Status.FAIL, Status.ERROR) for r in self.results)
