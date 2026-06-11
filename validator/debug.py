"""
İlerleme (progress) köprüsü — geriye uyumlu `dbg()` arayüzü.

Tüm raporlama/loglama artık `validator.reporter.Reporter` içinde toplanmıştır. Bu modül
yalnızca, modüllerin (row_counts, code_objects) çağırdığı `dbg(module, msg)` ilerleme
satırını aktif Reporter'a yönlendiren ince bir köprü olarak kalır. run.py, Reporter'ı
kurduktan sonra `set_reporter(reporter)` çağırır.
"""

_reporter = None


def set_reporter(rep) -> None:
    global _reporter
    _reporter = rep


def dbg(module: str, msg: str) -> None:
    """'Kontrol ediliyor' tarzı ilerleme satırı — aktif Reporter'a iletir (yoksa sessiz)."""
    if _reporter is not None:
        _reporter.dbg(module, msg)
