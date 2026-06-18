#!/usr/bin/env python3
"""
Oracle Migration Validator — CLI entry point
Kullanım: python run.py [OPTIONS]
"""

import sys
import click
from rich.console import Console
from rich.panel import Panel
from rich import box

from validator.config_loader import load_config, SchemaMapping
from validator.connection import test_connection, get_connection, fetch_one
from validator.result import (
    Status, ModuleSummary, ValidationResult, register_observer
)
from validator import debug
from validator.reporter import Reporter

console = Console()
reporter: Reporter | None = None


# ---------------------------------------------------------------------------
# CLI tanımı
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source-schema", "-s", multiple=True,
              help="Kontrol edilecek source schema (tekrar kullanılabilir)")
@click.option("--target-schema", "-t", multiple=True,
              help="Hedef schema adı (--source-schema ile birebir eşleşmeli)")
@click.option("--modules", "-m", default=None,
              help="Virgülle ayrılmış modül listesi: inventory,tables,indexes,constraints,sequences,grants,code,row_counts")
@click.option("--count-mode", default=None,
              type=click.Choice(["auto", "exact", "sample", "stats", "skip"]),
              help="Row count modu (config'i override eder)")
@click.option("--sample-pct", default=None, type=float,
              help="SAMPLE için yüzde (ör: 0.1, 1, 5)")
@click.option("--refresh-stats", is_flag=True, default=False,
              help="Count öncesi DBMS_STATS çalıştır")
@click.option("--parallel-degree", default=None, type=int,
              help="Oracle tek-sorgu-içi PARALLEL hint derecesi (0=kapalı)")
@click.option("--parallel-workers", default=None, type=int,
              help="Tablolar arası eşzamanlı sayım worker sayısı (1=seri)")
@click.option("--source-workers", default=None, type=int,
              help="Source (production) havuzu için ayrı worker tavanı")
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
@click.option("--generate-missing", is_flag=True, default=False,
              help="Target'ta eksik objelerin DDL scriptlerini üret")
@click.option("--output-dir", default=None,
              help="DDL scriptlerinin yazılacağı klasör (varsayılan: ./ddl_output)")
@click.option("--no-color", is_flag=True, default=False,
              help="Renkli çıktıyı kapat")
@click.option("--debug", "-d", "debug_flag", is_flag=True, default=False,
              help="Canlı akış — kontrol edilen her objeyi anlık olarak ekrana (stderr) yaz")
@click.option("--level", default=None,
              type=click.Choice(["sync", "not-sync", "failed"]),
              help="Çıktı eşiği (config'i override eder): sync=her şey, not-sync=NOT-SYNC+FAILED, failed=yalnızca FAILED")
def main(source_schema, target_schema, modules, count_mode, sample_pct,
         refresh_stats, parallel_degree, parallel_workers, source_workers,
         query_timeout, skip_tables,
         only_tables, connections, validation_config,
         generate_missing, output_dir, no_color, debug_flag, level):
    """
    Oracle 11g → 19c migration validation aracı.

    Örnekler:\n
      python run.py\n
      python run.py -s HR -t HR_NEW\n
      python run.py -s HR -t HR_NEW --modules inventory,tables,row_counts\n
      python run.py -s HR -t HR_NEW --count-mode sample --sample-pct 0.5\n
      python run.py -s HR -t HR_NEW --skip-tables AUDIT_LOG,BIG_EVENTS\n
      python run.py --generate-missing\n
      python run.py --generate-missing --output-dir ./scripts/missing
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

    # Oracle Client modu — top-level `oracle_client` bloğundan belirlenir.
    # Teknik not: init_oracle_client() process-level globaldir; her iki bağlantı da
    # thick mode'a geçer. Oracle 11g (11.2.0.4) thin mode'u desteklemez (DPY-3010),
    # bu yüzden 11g source için mode: thick zorunludur; 19c thick'i tam destekler.
    if cfg.oracle_client.mode == "thick":
        from validator.connection import init_thick_mode
        try:
            init_thick_mode(cfg.oracle_client.lib_dir)
            lib_info = cfg.oracle_client.lib_dir or "sistem PATH"
            console.print(f"[dim]  ℹ️  Thick mode etkin ({lib_info})[/]")
        except Exception as e:
            console.print(f"[bold red]Thick mode başlatılamadı:[/] {e}")
            console.print("[dim]  Oracle Instant Client kurulu ve PATH'te olmalı.[/]")
            sys.exit(1)

    # --generate-missing flag'i config'i override eder
    if generate_missing:
        cfg.generate_scripts.enabled = True
    if output_dir:
        cfg.generate_scripts.output_dir = output_dir

    # CLI'dan parametre overrideları
    if count_mode:
        cfg.row_count.mode = count_mode
    if sample_pct is not None:
        cfg.row_count.sample_pct = sample_pct
    if refresh_stats:
        cfg.row_count.refresh_stats = True
    if parallel_degree is not None:
        cfg.row_count.parallel_degree = parallel_degree
    if parallel_workers is not None:
        cfg.row_count.parallel_workers = parallel_workers
    if source_workers is not None:
        cfg.row_count.source_max_workers = source_workers
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
        if mc.users:               active_modules.add("users")
        if mc.code_objects_enabled: active_modules.add("code")
        # row_counts ayrıca -- her zaman tables modülüne eşlik eder
        # veya açıkça belirtilirse çalışır
        if "row_counts" in (modules or ""):
            active_modules.add("row_counts")

    # ------------------------------------------------------------------
    # Raporlama — merkezi Reporter. Dosya logu HER ZAMAN açıktır; tek eşik (level)
    # hem terminal tablolarını, hem canlı ekranı, hem dosya logunu süzer:
    #   sync=her şey · not-sync=NOT-SYNC+FAILED (default) · failed=yalnızca FAILED
    # --debug/output.live ek olarak canlı stderr akışını açar (aynı eşikle).
    # ------------------------------------------------------------------
    global reporter
    out = cfg.output
    eff_level = (level or out.level or "not-sync").lower()
    live = bool(debug_flag or out.live)
    reporter = Reporter(level=eff_level, log_file=out.log_file, live=live, no_color=no_color)
    register_observer(reporter.on_result)
    debug.set_reporter(reporter)
    console.print(f"[dim]  🗒️  Log: {reporter.log_path}  (eşik: {eff_level})[/]")
    if live:
        console.print(f"[dim]  🐞 Canlı akış aktif (eşik: {eff_level})[/]")

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
    if cfg.source.read_only:
        console.print("[dim]  🔒 Source read-only koruması aktif — bu bağlantıya hiçbir yazma yapılmaz.[/]")
    else:
        console.print("[bold yellow]  ⚠️  Source read-only KAPALI — bu bağlantıya yazma yapılabilir![/]")
    console.print()

    if not (src_ok and tgt_ok):
        console.print("[bold red]Bağlantı hatası — işlem durduruldu.[/]")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Schema döngüsü
    # ------------------------------------------------------------------
    all_summaries: list[ModuleSummary] = []
    # users modülü instance-wide'dır (schema-bağımsız) → tüm şema döngüsünde bir kez çalışır.
    users_done = False

    for mapping in cfg.schemas:
        console.rule(f"[bold]Schema: {mapping.source} → {mapping.target}[/]")
        console.print()
        debug.dbg("schema", f"{mapping.source} → {mapping.target} işleniyor")

        with get_connection(cfg.source) as src_conn, \
             get_connection(cfg.target) as tgt_conn:

            # Preflight — source şemada hiç görünür obje yoksa modülleri çalıştırmak
            # anlamsızdır ve yanıltıcı "TEMIZ" raporuna yol açar. Şema adı veya yetki
            # (SELECT ANY TABLE vb.) sorununu sessiz geçmek yerine FAIL olarak yakala.
            src_obj_cnt = fetch_one(
                src_conn,
                "SELECT COUNT(*) AS c FROM all_objects "
                "WHERE owner = :s AND object_name NOT LIKE 'BIN$%'",
                {"s": mapping.source},
            )
            if not src_obj_cnt or (src_obj_cnt.get("c") or 0) == 0:
                pre = ModuleSummary(module="preflight")
                pre.add(ValidationResult(
                    module="preflight", schema=mapping.source,
                    object_type="SCHEMA", object_name=mapping.source,
                    status=Status.FAILED,
                    source_value="0 obje",
                    target_value="",
                    note=("Source şemada görünür obje yok — şema adını ve yetkileri "
                          "(SELECT ANY TABLE vb.) kontrol edin"),
                ))
                reporter.render_module(pre)
                all_summaries.append(pre)
                continue

            if "inventory" in active_modules:
                from validator.modules.inventory import run as run_inventory
                summary = run_inventory(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            if "tables" in active_modules:
                from validator.modules.tables import run as run_tables
                summary = run_tables(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            if "constraints" in active_modules:
                from validator.modules.constraints import run as run_constraints
                summary = run_constraints(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            if "indexes" in active_modules:
                from validator.modules.indexes import run as run_indexes
                summary = run_indexes(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            if "sequences" in active_modules:
                from validator.modules.sequences import run as run_sequences
                summary = run_sequences(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            if "code" in active_modules:
                from validator.modules.code_objects import run as run_code
                summary = run_code(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            if "grants" in active_modules:
                from validator.modules.grants import run as run_grants
                summary = run_grants(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)

            # users instance-wide → ilk şema iterasyonunda bir kez (mevcut bağlantılarla)
            if "users" in active_modules and not users_done:
                from validator.modules.users import run as run_users
                summary = run_users(src_conn, tgt_conn, mapping, cfg)
                reporter.render_module(summary)
                all_summaries.append(summary)
                users_done = True

            if "row_counts" in active_modules:
                from validator.modules.row_counts import run as run_counts
                rcc = cfg.row_count
                if rcc.parallel_workers and rcc.parallel_workers > 1:
                    src_w = max(1, min(rcc.parallel_workers, rcc.source_max_workers))
                    console.print(
                        f"[dim]  ⚡ Paralel sayım — target {rcc.parallel_workers} worker, "
                        f"source {src_w} worker (havuz=worker)[/]"
                    )
                summary = run_counts(
                    src_conn, tgt_conn, mapping, cfg,
                    skip_tables=skip_list,
                    only_tables=only_list,
                )
                reporter.render_module(summary)
                all_summaries.append(summary)

            # ----------------------------------------------------------
            # DDL Script Üretimi
            # ----------------------------------------------------------
            if cfg.generate_scripts.enabled:
                _run_generate_scripts(
                    src_conn, tgt_conn, all_summaries, mapping, cfg, active_modules
                )

    # ------------------------------------------------------------------
    # Genel özet
    # ------------------------------------------------------------------
    reporter.render_overall(all_summaries)


# ---------------------------------------------------------------------------
# Yardımcı print fonksiyonları
# ---------------------------------------------------------------------------

def _run_generate_scripts(src_conn, tgt_conn, summaries: list, mapping, cfg, enabled_modules=None):
    """Validation sonuçlarından eksik objeleri toplayıp DDL dosyaları üretir.

    `enabled_modules`: açık validation modülleri (Execution Guard). Generator'a
    iletilir → `modules.X=false` olan bir modülün sahiplendiği tip üretilmez.
    """
    from validator.modules.ddl_generator import generate_scripts
    from validator.result import Status

    gs_cfg = cfg.generate_scripts
    # CONSTRAINT üretimi de modules.constraint_types filtresine uyar (tek kaynak).
    allowed_cons = getattr(cfg.modules, "constraint_types", None) or {"PK", "UK", "FK", "CHECK"}

    # FAILED sonuçlarından eksik objeleri topla
    # "eksik" = source'da var, target'ta yok → target_value boş
    missing: dict[str, list[str]] = {}
    # NOT-SYNC sequence'ler → hizalayıcı ALTER üretilir (eksik değil, farklı)
    not_sync_sequences: list[str] = []
    # Eksik constraint'ler → (tablo, label, imza). object_type="CONSTRAINT(PK|UK|FK|CHECK)",
    # object_name=tablo, source_value=yapısal imza (constraints.py adı bilinçli atar).
    missing_constraints: list[tuple] = []
    for sm in summaries:
        for r in sm.results:
            obj_type = (r.object_type or "").upper()
            if r.status == Status.FAILED and r.target_value in (None, "", "—", "-", "(yok)"):
                if obj_type.startswith("CONSTRAINT("):
                    label = obj_type[len("CONSTRAINT("):-1]  # PK / UK / FK / CHECK
                    if label not in allowed_cons:
                        continue
                    spec = (r.object_name, label, r.source_value or "")
                    if spec not in missing_constraints:
                        missing_constraints.append(spec)
                    continue
                if obj_type not in missing:
                    missing[obj_type] = []
                if r.object_name not in missing[obj_type]:
                    missing[obj_type].append(r.object_name)
            elif r.status == Status.NOT_SYNC and obj_type == "SEQUENCE":
                if r.object_name not in not_sync_sequences:
                    not_sync_sequences.append(r.object_name)

    if not any(missing.values()) and not not_sync_sequences and not missing_constraints:
        console.print("[dim]  ℹ️  Generate scripts: eksik/NOT-SYNC obje bulunamadı.[/]")
        return

    total_missing = sum(len(v) for v in missing.values())
    extras = []
    if not_sync_sequences:
        extras.append(f"{len(not_sync_sequences)} NOT-SYNC sequence")
    if missing_constraints:
        extras.append(f"{len(missing_constraints)} eksik constraint")
    extra = (" + " + " + ".join(extras)) if extras else ""
    console.rule(f"[bold cyan]DDL Script Üretimi[/] — {total_missing} eksik obje{extra}")
    console.print(f"  Çıktı klasörü: [cyan]{gs_cfg.output_dir}[/]")
    console.print()

    created = generate_scripts(
        source_conn=src_conn,
        missing_objects=missing,
        source_schema=mapping.source,
        target_schema=mapping.target,
        cfg=gs_cfg,
        console=console,
        not_sync_sequences=not_sync_sequences,
        missing_constraints=missing_constraints,
        target_conn=tgt_conn,
        enabled_modules=enabled_modules,
    )

    console.print()
    if created:
        console.print(
            f"[bold green]✅ {len(created)} dosya oluşturuldu → "
            f"{gs_cfg.output_dir}/README_apply_order.txt[/]"
        )
    console.print()


def _print_conn_status(label: str, dsn: str, ok: bool, info: str):
    icon  = "✅" if ok else "❌"
    style = "green" if ok else "red"
    console.print(f"  {icon} [{style}]{label}[/]  {dsn}  —  {info}")


if __name__ == "__main__":
    main()
