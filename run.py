#!/usr/bin/env python3
"""
Oracle Migration Validator — CLI entry point
Kullanım: python run.py [OPTIONS]
"""

import sys
import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from validator.config_loader import load_config, SchemaMapping
from validator.connection import test_connection, get_connection
from validator.result import Status, STATUS_STYLE, STATUS_ICON, ModuleSummary

console = Console()


# ---------------------------------------------------------------------------
# CLI tanımı
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source-schema", "-s", multiple=True,
              help="Kontrol edilecek source schema (tekrar kullanılabilir)")
@click.option("--target-schema", "-t", multiple=True,
              help="Hedef schema adı (--source-schema ile birebir eşleşmeli)")
@click.option("--modules", "-m", default=None,
              help="Virgülle ayrılmış modül listesi: inventory,tables,indexes,constraints,sequences,code,row_counts")
@click.option("--count-mode", default=None,
              type=click.Choice(["auto", "exact", "sample", "stats", "skip"]),
              help="Row count modu (config'i override eder)")
@click.option("--sample-pct", default=None, type=float,
              help="SAMPLE için yüzde (ör: 0.1, 1, 5)")
@click.option("--refresh-stats", is_flag=True, default=False,
              help="Count öncesi DBMS_STATS çalıştır")
@click.option("--parallel-degree", default=None, type=int,
              help="Parallel sorgu derecesi (0=kapalı)")
@click.option("--query-timeout", default=None, type=int,
              help="Saniye cinsinden sorgu timeout")
@click.option("--skip-tables", default=None,
              help="Row count'tan çıkarılacak tablolar (virgülle)")
@click.option("--only-tables", default=None,
              help="Sadece bu tabloları kontrol et (virgülle)")
@click.option("--connections", default="config/connections.yaml",
              help="Bağlantı config dosyası", show_default=True)
@click.option("--validation-config", default="config/validation.yaml",
              help="Validation config dosyası", show_default=True)
@click.option("--no-color", is_flag=True, default=False,
              help="Renkli çıktıyı kapat")
def main(source_schema, target_schema, modules, count_mode, sample_pct,
         refresh_stats, parallel_degree, query_timeout, skip_tables,
         only_tables, connections, validation_config, no_color):
    """
    Oracle 11g → 19c migration validation aracı.

    Örnekler:\n
      python run.py\n
      python run.py -s HR -t HR_NEW\n
      python run.py -s HR -t HR_NEW --modules inventory,tables,row_counts\n
      python run.py -s HR -t HR_NEW --count-mode sample --sample-pct 0.5\n
      python run.py -s HR -t HR_NEW --skip-tables AUDIT_LOG,BIG_EVENTS
    """
    global console
    if no_color:
        console = Console(highlight=False, markup=False)

    # ------------------------------------------------------------------
    # Config yükle
    # ------------------------------------------------------------------
    try:
        cfg = load_config(connections, validation_config)
    except Exception as e:
        console.print(f"[bold red]Config hatası:[/] {e}")
        sys.exit(1)

    # CLI'dan schema override
    if source_schema:
        if len(source_schema) != len(target_schema):
            console.print("[bold red]--source-schema ve --target-schema sayısı eşit olmalı.[/]")
            sys.exit(1)
        cfg.schemas = [
            SchemaMapping(s.upper(), t.upper())
            for s, t in zip(source_schema, target_schema)
        ]

    # CLI'dan parametre overrideları
    if count_mode:
        cfg.row_count.mode = count_mode
    if sample_pct is not None:
        cfg.row_count.sample_pct = sample_pct
    if refresh_stats:
        cfg.row_count.refresh_stats = True
    if parallel_degree is not None:
        cfg.row_count.parallel_degree = parallel_degree
    if query_timeout is not None:
        cfg.row_count.timeout_sec = query_timeout

    skip_list = [t.upper() for t in skip_tables.split(",")] if skip_tables else []
    only_list = [t.upper() for t in only_tables.split(",")] if only_tables else []

    # Aktif modülleri belirle
    active_modules = set()
    if modules:
        active_modules = {m.strip().lower() for m in modules.split(",")}
    else:
        mc = cfg.modules
        if mc.inventory:           active_modules.add("inventory")
        if mc.tables:              active_modules.add("tables")
        if mc.indexes:             active_modules.add("indexes")
        if mc.constraints:         active_modules.add("constraints")
        if mc.sequences:           active_modules.add("sequences")
        if mc.grants:              active_modules.add("grants")
        if mc.code_objects_enabled: active_modules.add("code")
        # row_counts ayrıca -- her zaman tables modülüne eşlik eder
        # veya açıkça belirtilirse çalışır
        if "row_counts" in (modules or ""):
            active_modules.add("row_counts")

    # ------------------------------------------------------------------
    # Başlık
    # ------------------------------------------------------------------
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Oracle Migration Validator[/]\n"
        "[dim]11g → 19c Schema Validation[/]",
        box=box.DOUBLE
    ))
    console.print()

    # ------------------------------------------------------------------
    # Bağlantı testi
    # ------------------------------------------------------------------
    console.print("[bold]Bağlantı testi yapılıyor...[/]")

    src_ok, src_info = test_connection(cfg.source)
    tgt_ok, tgt_info = test_connection(cfg.target)

    _print_conn_status("SOURCE", cfg.source.dsn, src_ok, src_info)
    _print_conn_status("TARGET", cfg.target.dsn, tgt_ok, tgt_info)
    console.print()

    if not (src_ok and tgt_ok):
        console.print("[bold red]Bağlantı hatası — işlem durduruldu.[/]")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Schema döngüsü
    # ------------------------------------------------------------------
    all_summaries: list[ModuleSummary] = []

    for mapping in cfg.schemas:
        console.rule(f"[bold]Schema: {mapping.source} → {mapping.target}[/]")
        console.print()

        with get_connection(cfg.source) as src_conn, \
             get_connection(cfg.target) as tgt_conn:

            if "inventory" in active_modules:
                from validator.modules.inventory import run as run_inventory
                summary = run_inventory(src_conn, tgt_conn, mapping, cfg)
                _print_module_results(summary)
                all_summaries.append(summary)

            if "tables" in active_modules:
                from validator.modules.tables import run as run_tables
                summary = run_tables(src_conn, tgt_conn, mapping, cfg)
                _print_module_results(summary)
                all_summaries.append(summary)

            if "indexes" in active_modules:
                from validator.modules.indexes import run as run_indexes
                summary = run_indexes(src_conn, tgt_conn, mapping, cfg)
                _print_module_results(summary)
                all_summaries.append(summary)

            if "sequences" in active_modules:
                from validator.modules.sequences import run as run_sequences
                summary = run_sequences(src_conn, tgt_conn, mapping, cfg)
                _print_module_results(summary)
                all_summaries.append(summary)

            if "code" in active_modules:
                from validator.modules.code_objects import run as run_code
                summary = run_code(src_conn, tgt_conn, mapping, cfg)
                _print_module_results(summary)
                all_summaries.append(summary)

            if "row_counts" in active_modules:
                from validator.modules.row_counts import run as run_counts
                summary = run_counts(
                    src_conn, tgt_conn, mapping, cfg,
                    skip_tables=skip_list,
                    only_tables=only_list,
                )
                _print_module_results(summary)
                all_summaries.append(summary)

    # ------------------------------------------------------------------
    # Genel özet
    # ------------------------------------------------------------------
    _print_overall_summary(all_summaries)


# ---------------------------------------------------------------------------
# Yardımcı print fonksiyonları
# ---------------------------------------------------------------------------

def _print_conn_status(label: str, dsn: str, ok: bool, info: str):
    icon  = "✅" if ok else "❌"
    style = "green" if ok else "red"
    console.print(f"  {icon} [{style}]{label}[/]  {dsn}  —  {info}")


def _print_module_results(summary: ModuleSummary):
    if not summary.results:
        return

    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
              expand=False, padding=(0, 1))
    t.add_column("Obje Tipi",  style="dim", width=18)
    t.add_column("Obje Adı",   width=35)
    t.add_column("Durum",      width=10)
    t.add_column("Source",     width=25)
    t.add_column("Target",     width=25)
    t.add_column("Not",        style="dim")

    for r in summary.results:
        style = STATUS_STYLE[r.status]
        icon  = STATUS_ICON[r.status]
        t.add_row(
            r.object_type,
            r.object_name,
            f"[{style}]{icon} {r.status.value}[/]",
            r.source_value or "",
            r.target_value or "",
            r.note or "",
        )

    counts = summary.counts
    title = (
        f"[bold]{summary.module.upper()}[/]  "
        f"[green]✅ {counts[Status.PASS]}[/]  "
        f"[red]❌ {counts[Status.FAIL]}[/]  "
        f"[yellow]⚠️  {counts[Status.WARNING]}[/]  "
        f"[dim]⏭️  {counts[Status.SKIPPED]}[/]"
    )
    console.print(Panel(t, title=title, box=box.ROUNDED, padding=(0, 1)))
    console.print()


def _print_overall_summary(summaries: list[ModuleSummary]):
    if not summaries:
        return

    console.rule("[bold]GENEL ÖZET[/]")
    console.print()

    total = {s: 0 for s in Status}
    for sm in summaries:
        for s, n in sm.counts.items():
            total[s] += n

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(width=12)
    t.add_column(width=8, justify="right")

    for status, n in total.items():
        if n == 0:
            continue
        style = STATUS_STYLE[status]
        icon  = STATUS_ICON[status]
        t.add_row(f"[{style}]{icon} {status.value}[/]", f"[{style}]{n}[/]")

    console.print(t)
    console.print()

    overall_ok = total[Status.FAIL] == 0 and total[Status.ERROR] == 0
    if overall_ok:
        console.print("[bold green]✅ Validation tamamlandı — kritik hata yok.[/]")
    else:
        console.print(
            f"[bold red]❌ Validation tamamlandı — "
            f"{total[Status.FAIL]} FAIL, {total[Status.ERROR]} ERROR.[/]"
        )
    console.print()


if __name__ == "__main__":
    main()
