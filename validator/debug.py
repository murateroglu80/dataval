"""
Loglama ve canlı debug akışı.

İki AYRI sorumluluk:

1. **Dosya logu — HER ZAMAN açık, EKSİKSİZ.** `setup_file_log()` her çalıştırmada bir
   `FileHandler` kurar ve `result.ModuleSummary.add()` gözlemcisi (`on_result`) üzerinden
   kontrol edilen HER objeyi (PASS dahil, tüm modüller) dosyaya yazar. Bu, terminaldeki
   rapor tablolarının tam, kalıcı bir aynasıdır (aynı `ValidationResult` nesneleri kaynakta
   yakalanır).

2. **Canlı ekran akışı — OPT-IN + SEVİYELİ.** `enable_live()` yalnızca `--debug`/
   `debug.enabled` iken stderr'e renkli, canlı satırlar basar. `log_level` (INFO/WARNING/
   ERROR) eşiği ekran gürültüsünü kısar; dosya logu bundan etkilenmez (daima eksiksiz).

Tasarım notu: result.py hiçbir debug/IO modülünü import etmez — bağımlılık tek yönlüdür.
Gözlemci hatası asla doğrulamayı bozmaz (add() içinde try/except ile sarılı).
"""

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from validator.result import Status, STATUS_STYLE, STATUS_ICON

# ---------------------------------------------------------------------------
# Modül durumu
# ---------------------------------------------------------------------------
_file_logger: logging.Logger | None = None   # her zaman-açık dosya logu
_log_path: str | None = None

_live_enabled: bool = False                  # canlı stderr akışı (opt-in)
_console: Console | None = None              # stderr — canlı ekran çıktısı
_screen_level: int = logging.INFO            # canlı ekran eşik seviyesi

_BASE = Path(__file__).resolve().parent.parent   # proje kök dizini

# Sonuç durumu → log seviyesi. Dosya tümünü (INFO ve üstü) yazar; canlı ekran
# yalnızca _screen_level eşiğini geçenleri gösterir.
_RESULT_LEVEL = {
    Status.PASS:    logging.INFO,
    Status.SKIPPED: logging.INFO,
    Status.WARNING: logging.WARNING,
    Status.TIMEOUT: logging.WARNING,
    Status.FAIL:    logging.ERROR,
    Status.ERROR:   logging.ERROR,
}


def is_live() -> bool:
    return _live_enabled


def log_path() -> str | None:
    return _log_path


def _level_int(name: str) -> int:
    """INFO/WARNING/ERROR isimini logging seviyesine çevirir (bilinmeyen → INFO)."""
    return getattr(logging, str(name).upper(), logging.INFO)


# ---------------------------------------------------------------------------
# Kurulum
# ---------------------------------------------------------------------------

def setup_file_log(log_file: str | None = None) -> str:
    """
    Her zaman-açık dosya logunu kurar. Zaman damgalı log dosyasını hazırlar ve
    çözülen yolu döner. Handler seviyesi INFO → kontrol edilen tüm sonuçlar yazılır.

    log_file: Boş/None ise ./logs/dataval_<YYYYMMDD_HHMMSS>.log otomatik üretilir.
    """
    global _file_logger, _log_path

    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = _BASE / path
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _BASE / "logs" / f"dataval_{stamp}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_path = str(path)

    logger = logging.getLogger("dataval.file")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.FileHandler(_log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    _file_logger = logger
    _file_logger.info("=== dataval log başladı ===")
    return _log_path


def enable_live(no_color: bool = False, screen_level: str = "INFO") -> None:
    """
    Canlı stderr akışını açar (yalnızca --debug/debug.enabled iken çağrılır).

    screen_level: INFO/WARNING/ERROR — ekranda gösterilecek minimum sonuç seviyesi.
                  Dosya logu bundan ETKİLENMEZ; daima eksiksizdir.
    """
    global _live_enabled, _console, _screen_level
    _console = Console(stderr=True, no_color=no_color, highlight=False)
    _screen_level = _level_int(screen_level)
    _live_enabled = True


# ---------------------------------------------------------------------------
# Gözlemci + ilerleme
# ---------------------------------------------------------------------------

def _fmt_val(v) -> str:
    if v is None or v == "":
        return "-"
    return str(v)


def on_result(module: str, r) -> None:
    """
    result.ModuleSummary.add() gözlemcisi. Eklenen her ValidationResult için:
      - dosyaya HER ZAMAN (seviye eşlemeli) yazar — eksiksiz kalıcı kayıt,
      - canlı ekran açık VE sonuç seviyesi eşiği geçiyorsa stderr'e renkli basar.
    """
    if _file_logger is None and not _live_enabled:
        return

    obj = f"{r.schema}.{r.object_name}"
    src = _fmt_val(r.source_value)
    tgt = _fmt_val(r.target_value)
    status = r.status.value
    note = f" — {r.note}" if r.note else ""
    level = _RESULT_LEVEL.get(r.status, logging.INFO)

    # Dosya (düz metin) — her zaman, tam kayıt.
    if _file_logger is not None:
        _file_logger.log(level, f"[{module}] {obj} source={src} target={tgt} {status}{note}")

    # Ekran (renkli) — yalnızca canlı açık ve seviye eşiği geçiliyorsa.
    if _live_enabled and level >= _screen_level:
        style = STATUS_STYLE.get(r.status, "white")
        icon = STATUS_ICON.get(r.status, "")
        tag = escape(f"[{module}]")
        _console.print(
            f"[dim]  · {tag}[/] {escape(obj)}  "
            f"[dim]src=[/]{escape(src)}  [dim]tgt=[/]{escape(tgt)}  "
            f"[{style}]{icon} {status}[/]"
        )


def dbg(module: str, msg: str) -> None:
    """
    Yavaş işlemler için 'kontrol ediliyor' tarzı ilerleme satırı (sonuç gelmeden).
    Dosyaya (varsa) INFO olarak; ekrana yalnızca canlı akış açıksa yazar.
    """
    if _file_logger is not None:
        _file_logger.info(f"[{module}] {msg}")
    if _live_enabled:
        _console.print(f"[dim]  · {escape(f'[{module}] {msg}')}[/]")
