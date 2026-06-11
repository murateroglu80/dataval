"""
Merkezi raporlama — tek sınıf, üç çıktı kanalı, tek eşik.

`Reporter` doğrulama sonuçlarının TEK yetkili çıktı katmanıdır:

1. **Terminal raporu** (stdout) — `render_module()` / `render_overall()`: modül başına
   kompakt başlık (sayımlar) + eşik geçen NOT-SYNC/FAILED sonuçlarının hiyerarşik listesi.
2. **Dosya logu** (her zaman) — zaman damgalı `./logs/dataval_*.log`; `on_result` gözlemcisi
   her sonucu düz metin satır olarak yazar.
3. **Canlı ekran** (opt-in, stderr) — `--debug`/`output.live` iken her sonuç anlık renkli basılır.

Üç kanal da aynı `level` eşiğiyle (sync < not-sync < failed) süzülür: `passes_level()`.
Reporter, `result.register_observer(reporter.on_result)` ile sonuç choke-point'ine bağlanır;
böylece ekrana basılan ve loglanan **aynı** ValidationResult nesneleridir.
"""

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich import box
from rich.markup import escape

from validator.result import (
    Status, STATUS_STYLE, STATUS_ICON, passes_level, DEFAULT_LEVEL,
)

_BASE = Path(__file__).resolve().parent.parent   # proje kök dizini

# Terminal/dosya listesinde önce en kritik sonuç görünsün diye sıralama anahtarı.
_SORT_ORDER = {Status.FAILED: 0, Status.NOT_SYNC: 1, Status.SKIPPED: 2, Status.SYNC: 3}


def _fmt_val(v) -> str:
    if v is None or v == "":
        return "-"
    return str(v)


class Reporter:
    def __init__(self, level: str = DEFAULT_LEVEL, log_file: str | None = None,
                 live: bool = False, no_color: bool = False):
        self.level = str(level).lower()
        self.no_color = no_color
        self.console = Console(no_color=no_color, highlight=False)
        self.err_console: Console | None = None
        self.live = live
        if live:
            self.err_console = Console(stderr=True, no_color=no_color, highlight=False)

        # Dosya logu — her zaman kurulur. Seviye filtrelemesi Python tarafında
        # passes_level() ile yapılır; handler yalnızca düz sink'tir.
        if log_file:
            path = Path(log_file)
            if not path.is_absolute():
                path = _BASE / path
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = _BASE / "logs" / f"dataval_{stamp}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._log_path = str(path)

        logger = logging.getLogger("dataval.file")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)
        handler = logging.FileHandler(self._log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                               datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        self._logger = logger
        self._logger.info(f"=== dataval log başladı (eşik: {self.level}) ===")

    @property
    def log_path(self) -> str:
        return self._log_path

    # -----------------------------------------------------------------------
    # Gözlemci — dosya + canlı ekran (her sonuçta, eşik süzgeçli)
    # -----------------------------------------------------------------------
    def on_result(self, module: str, r) -> None:
        if not passes_level(r.status, self.level):
            return

        obj = f"{r.schema}.{r.object_name}"
        detail = self._detail_text(r)

        # Dosya (düz metin)
        self._logger.info(
            f"[{module}] {obj} source={_fmt_val(r.source_value)} "
            f"target={_fmt_val(r.target_value)} {r.status.value}"
            f"{(' — ' + detail) if detail else ''}"
        )

        # Canlı ekran (renkli)
        if self.live and self.err_console is not None:
            style = STATUS_STYLE.get(r.status, "white")
            icon = STATUS_ICON.get(r.status, "")
            tag = escape(f"[{module}]")
            line = (
                f"[dim]  · {tag}[/] {escape(obj)}  "
                f"[{style}]{icon} {r.status.value}[/]"
            )
            if detail:
                line += f"  [dim]{escape(detail)}[/]"
            self.err_console.print(line)

    def dbg(self, module: str, msg: str) -> None:
        """İlerleme satırı (sonuç değil) — dosyaya yazılır, canlı ekrana yalnızca live iken."""
        self._logger.info(f"[{module}] {msg}")
        if self.live and self.err_console is not None:
            self.err_console.print(f"[dim]  · {escape(f'[{module}] {msg}')}[/]")

    # -----------------------------------------------------------------------
    # Terminal raporu — modül paneli
    # -----------------------------------------------------------------------
    def render_module(self, summary) -> None:
        if not summary.results:
            return

        counts = summary.counts
        title = (
            f"[bold]{summary.module.upper()}[/]  "
            f"[green]✅ {counts[Status.SYNC]}[/]  "
            f"[yellow]⚠️  {counts[Status.NOT_SYNC]}[/]  "
            f"[red]❌ {counts[Status.FAILED]}[/]  "
            f"[dim]⏭️  {counts[Status.SKIPPED]}[/]"
        )

        shown = [r for r in summary.results if passes_level(r.status, self.level)]
        shown.sort(key=lambda r: (_SORT_ORDER.get(r.status, 9), r.object_type, r.object_name))

        if not shown:
            # Eşik altındaki her şey gizli — modülün temiz çalıştığını tek satır bildir.
            body = "[dim]  (eşik altı — gösterilecek sonuç yok)[/]"
        else:
            body = "\n".join(self._result_block(r) for r in shown)

        self.console.print(Panel(body, title=title, box=box.ROUNDED, padding=(0, 1)))
        self.console.print()

    def _result_block(self, r) -> str:
        """Tek bir sonucu hiyerarşik metin bloğuna çevirir (Reporter.console markup'ı)."""
        style = STATUS_STYLE.get(r.status, "white")
        icon = STATUS_ICON.get(r.status, "")
        head = (
            f"[{style}]{icon} {r.status.value}[/]  "
            f"[dim]{escape(r.object_type)}[/]  {escape(r.schema)}.{escape(r.object_name)}"
        )
        if r.diffs:
            lines = [head]
            width = max((len(str(a)) for a, _, _ in r.diffs), default=0)
            for attr, src, tgt in r.diffs:
                lines.append(
                    f"      [cyan]{escape(str(attr)).ljust(width)}[/]  "
                    f"[dim]Source:[/] {escape(_fmt_val(src))}   "
                    f"[dim]Target:[/] {escape(_fmt_val(tgt))}"
                )
            return "\n".join(lines)
        # diffs yoksa: not / source→target tek satır
        detail = self._detail_text(r)
        if detail:
            return f"{head}  [dim]— {escape(detail)}[/]"
        return head

    @staticmethod
    def _detail_text(r) -> str:
        """diffs varsa 'attr: src->tgt; ...', yoksa note (ya da source→target)."""
        if r.diffs:
            return "; ".join(f"{a}: {_fmt_val(s)}->{_fmt_val(t)}" for a, s, t in r.diffs)
        if r.note:
            return r.note
        if r.source_value or r.target_value:
            return f"{_fmt_val(r.source_value)} -> {_fmt_val(r.target_value)}"
        return ""

    # -----------------------------------------------------------------------
    # Terminal raporu — genel özet
    # -----------------------------------------------------------------------
    def render_overall(self, summaries: list) -> None:
        if not summaries:
            return

        self.console.rule("[bold]GENEL ÖZET[/]")
        self.console.print()

        total = {s: 0 for s in Status}
        for sm in summaries:
            for s, n in sm.counts.items():
                total[s] += n

        from rich.table import Table
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("Durum", width=14)
        t.add_column("Sayı",  width=8, justify="right")
        t.add_row("[green]✅ SYNC[/]",      str(total[Status.SYNC]))
        t.add_row("[yellow]⚠️  NOT-SYNC[/]", str(total[Status.NOT_SYNC]))
        t.add_row("[red]❌ FAILED[/]",       str(total[Status.FAILED]))
        if total[Status.SKIPPED]:
            t.add_row("[dim]⏭️  SKIPPED[/]", str(total[Status.SKIPPED]))

        failed = total[Status.FAILED]
        not_sync = total[Status.NOT_SYNC]
        if failed:
            title_style, label = "bold red", f"❌ {failed} FAILED"
        elif not_sync:
            title_style, label = "bold yellow", f"⚠️  {not_sync} NOT-SYNC"
        else:
            title_style, label = "bold green", "✅ TÜMÜ SYNC"

        self.console.print(Panel(t, title=f"[{title_style}]{label}[/]",
                                 box=box.ROUNDED, padding=(0, 2)))
        self.console.print()
