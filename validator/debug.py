"""
Debug mode — doğrulama çalışırken kontrol edilen her objeyi CANLI olarak ekrana
(ayrı stderr akışı) ve bir log dosyasına yazar.

Tasarım notu: Bu katman tamamen opsiyoneldir ve mevcut akışa eklemelidir.
- result.ModuleSummary.add() içindeki gözlemci kancasına `on_result` kaydedilir;
  böylece tüm modüllerin sonuçları (obje + source + target + status) tek yerden,
  karşılaştırma mantığına dokunulmadan yakalanır.
- Kapalıyken (varsayılan) hiçbir şey basılmaz; mevcut stdout raporu etkilenmez.
"""

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from validator.result import STATUS_STYLE, STATUS_ICON

# ---------------------------------------------------------------------------
# Modül durumu
# ---------------------------------------------------------------------------
_enabled: bool = False
_console: Console | None = None      # stderr — canlı ekran çıktısı
_logger: logging.Logger | None = None
_log_path: str | None = None

_BASE = Path(__file__).resolve().parent.parent   # proje kök dizini


def is_enabled() -> bool:
    return _enabled


def enable(log_file: str | None = None, no_color: bool = False) -> str:
    """
    Debug mode'u açar. Zaman damgalı log dosyasını kurar ve çözülen yolu döner.

    log_file: Boş/None ise ./logs/dataval_<YYYYMMDD_HHMMSS>.log otomatik üretilir.
    no_color: True ise ekran çıktısı renksiz.
    """
    global _enabled, _console, _logger, _log_path

    # Log dosyası yolu
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = _BASE / path
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _BASE / "logs" / f"dataval_{stamp}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_path = str(path)

    # Ekran konsolu (stderr — stdout raporunu kirletmez)
    _console = Console(stderr=True, no_color=no_color, highlight=False)

    # Dosya logger'ı
    _logger = logging.getLogger("dataval.debug")
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
    handler = logging.FileHandler(_log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(handler)

    _enabled = True
    _logger.info("=== Debug mode başladı ===")
    return _log_path


def _fmt_val(v) -> str:
    if v is None or v == "":
        return "-"
    return str(v)


def on_result(module: str, r) -> None:
    """
    result.ModuleSummary.add() gözlemcisi. Eklenen her ValidationResult'ı yazar:
      ŞEMA.OBJE  source=<...>  target=<...>  STATUS
    """
    if not _enabled:
        return
    obj = f"{r.schema}.{r.object_name}"
    src = _fmt_val(r.source_value)
    tgt = _fmt_val(r.target_value)
    status = r.status.value
    note = f" — {r.note}" if r.note else ""

    # Ekran (renkli). Not: modül adı köşeli parantezli olduğundan markup sanılmasın
    # diye escape edilir; yalnızca kasıtlı stil etiketleri (dim, status stili) markup'tır.
    style = STATUS_STYLE.get(r.status, "white")
    icon = STATUS_ICON.get(r.status, "")
    tag = escape(f"[{module}]")
    _console.print(
        f"[dim]  · {tag}[/] {escape(obj)}  "
        f"[dim]src=[/]{escape(src)}  [dim]tgt=[/]{escape(tgt)}  "
        f"[{style}]{icon} {status}[/]"
    )
    # Log (düz metin)
    _logger.info(f"[{module}] {obj} source={src} target={tgt} {status}{note}")


def dbg(module: str, msg: str) -> None:
    """
    Yavaş işlemler için 'kontrol ediliyor' tarzı ilerleme satırı (sonuç gelmeden).
    Ekran + log'a yazar.
    """
    if not _enabled:
        return
    _console.print(f"[dim]  · {escape(f'[{module}] {msg}')}[/]")
    _logger.info(f"[{module}] {msg}")
