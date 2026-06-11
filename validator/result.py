"""
Tüm modüllerin ortak sonuç veri modeli.

Statü jargonu (migration odaklı):
  SYNC      → obje/veri iki tarafta da birebir aynı (eşit).
  NOT_SYNC  → obje iki tarafta da var ama yapısı/özellikleri farklı.
  FAILED    → obje source'ta var, target'ta eksik VEYA doğrulanamadı (timeout/hata).
  SKIPPED   → kontrol atlandı (config gereği).

Filtreleme tek eşik (`level`) ile yapılır: sync < not-sync < failed. Bir sonuç,
STATUS_RANK[status] >= LEVEL_RANK[level] ise ekrana/dosyaya yazılır. SKIPPED daima
`sync` seviyesindedir (yalnızca level=sync iken görünür).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Status(str, Enum):
    SYNC     = "SYNC"      # iki tarafta birebir aynı
    NOT_SYNC = "NOT-SYNC"  # var ama farklı
    FAILED   = "FAILED"    # source'ta var, target'ta yok / doğrulanamadı
    SKIPPED  = "SKIPPED"   # atlandı


# Rich terminal renkleri
STATUS_STYLE = {
    Status.SYNC:     "bold green",
    Status.NOT_SYNC: "bold yellow",
    Status.FAILED:   "bold red",
    Status.SKIPPED:  "dim",
}

STATUS_ICON = {
    Status.SYNC:     "✅",
    Status.NOT_SYNC: "⚠️ ",
    Status.FAILED:   "❌",
    Status.SKIPPED:  "⏭️ ",
}


# ---------------------------------------------------------------------------
# Eşik (level) filtreleme — tek doğruluk kaynağı.
# Hem terminal tabloları, hem canlı ekran, hem dosya logu bu sıralamayı kullanır.
# ---------------------------------------------------------------------------
STATUS_RANK = {
    Status.SKIPPED:  0,
    Status.SYNC:     0,
    Status.NOT_SYNC: 1,
    Status.FAILED:   2,
}

LEVEL_RANK = {
    "sync":     0,   # her şey (SYNC + SKIPPED dahil)
    "not-sync": 1,   # NOT-SYNC + FAILED
    "failed":   2,   # yalnızca FAILED
}

DEFAULT_LEVEL = "not-sync"


def level_rank(level: str) -> int:
    """'sync'/'not-sync'/'failed' eşik adını sıraya çevirir (bilinmeyen → default)."""
    return LEVEL_RANK.get(str(level).lower(), LEVEL_RANK[DEFAULT_LEVEL])


def passes_level(status: Status, level: str) -> bool:
    """Bir sonucun verilen eşikte gösterilip gösterilmeyeceğini söyler."""
    return STATUS_RANK.get(status, 0) >= level_rank(level)


def extra_status(extra_as: str) -> Status:
    """
    Target'ta FAZLA (kaynakta yok) objelerin statüsünü config'e göre döner.
    extra_as='not-sync' → NOT_SYNC (göster, default); 'sync' → SYNC (default gizli).
    """
    return Status.SYNC if str(extra_as).lower() == "sync" else Status.NOT_SYNC


# ---------------------------------------------------------------------------
# Opsiyonel gözlemci (observer) kaydı.
# Reporter gibi katmanlar, her ValidationResult eklendiğinde haberdar olmak için
# buraya kayıt olur. Kimse kayıtlı değilse (varsayılan) add() davranışı birebir aynıdır.
# result.py hiçbir reporter/IO modülünü import etmez — bağımlılık tek yönlüdür.
# ---------------------------------------------------------------------------
_observers: list = []


def register_observer(fn) -> None:
    """Her add()'de çağrılacak bir gözlemci ekler: fn(module: str, result: ValidationResult)."""
    if fn not in _observers:
        _observers.append(fn)


def clear_observers() -> None:
    _observers.clear()


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
    # Granüler fark listesi: (öznitelik, source, target) üçlüleri. Doluysa Reporter
    # bunu hiyerarşik basar (ör. ("tip", "NUMBER", "VARCHAR2")); None ise note'a düşer.
    diffs: Optional[list] = None


@dataclass
class ModuleSummary:
    module: str
    results: list[ValidationResult] = field(default_factory=list)

    def add(self, r: ValidationResult):
        self.results.append(r)
        # Gözlemci(ler)e bildir — bir gözlemci hatası asla doğrulamayı bozmamalı.
        for obs in _observers:
            try:
                obs(self.module, r)
            except Exception:
                pass

    @property
    def counts(self) -> dict:
        from collections import Counter
        c = Counter(r.status for r in self.results)
        return {s: c.get(s, 0) for s in Status}

    @property
    def passed(self) -> bool:
        # Tek "sorun" kovası FAILED'dir; NOT-SYNC ayrı raporlanır (özet ikisini de gösterir).
        return not any(r.status == Status.FAILED for r in self.results)
