"""
Row counts modülü — akıllı tablo sayım stratejisi.

Mod seçimi (config veya CLI):
  auto    → num_rows + threshold bazlı otomatik seçim
  exact   → SELECT COUNT(*) — küçük tablolar
  sample  → SELECT COUNT(*) FROM table SAMPLE(pct)
  stats   → ALL_TABLES.NUM_ROWS (istatistik tabanlı, sıfır I/O)
  skip    → bu tabloyu atla

Güvenlik mekanizmaları:
  - callTimeout: sorgu X saniyeyi geçerse ORA-03136 → TIMEOUT
  - SAMPLE clause: full scan yerine blok örnekleme
  - Parallel hint: büyük tablolarda opsiyonel paralel okuma
  - Tablo bazlı override: validation.yaml → row_count.overrides
"""

import oracledb
from datetime import datetime, timezone
from validator.connection import fetch_all, fetch_one, assert_writable
from validator.debug import dbg
from validator.result import ValidationResult, ModuleSummary, Status
from validator.config_loader import AppConfig, SchemaMapping

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_TABLE_STATS = """
SELECT
    table_name,
    num_rows,
    last_analyzed
FROM all_tables
WHERE owner      = :schema
  AND table_name NOT LIKE 'BIN$%'
ORDER BY table_name
"""


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _stats_age_days(last_analyzed) -> int | None:
    """İstatistiğin kaç gün önce toplandığını döner. None = hiç toplanmamış."""
    if last_analyzed is None:
        return None
    now = datetime.now(timezone.utc)
    # cx_Oracle/oracledb datetime nesnesi döner, timezone-naive olabilir
    if last_analyzed.tzinfo is None:
        from datetime import timezone as tz
        last_analyzed = last_analyzed.replace(tzinfo=tz.utc)
    return (now - last_analyzed).days


def _resolve_mode(table_name: str, num_rows: int | None, cfg: AppConfig) -> str:
    """Tablo için uygulanacak modu döner."""
    rc = cfg.row_count

    # Tablo bazlı override
    override = rc.overrides.get(table_name.upper())
    if override:
        return override.lower()

    mode = rc.mode.lower()
    if mode != "auto":
        return mode

    # auto: num_rows yoksa stats kullan
    if num_rows is None:
        return "stats"

    thresholds = rc.auto_thresholds
    if num_rows < thresholds["exact_below"]:
        return "exact"
    elif num_rows < thresholds["sample_below"]:
        return "sample"
    else:
        return "stats"


def _count_exact(conn: oracledb.Connection, schema: str, table: str,
                 timeout_ms: int, parallel: int) -> tuple[int | None, str]:
    """Exact COUNT(*). Dönüş: (sayı, kullanılan_mod)"""
    hint = f"/*+ PARALLEL(t, {parallel}) */" if parallel > 0 else ""
    sql = f"SELECT {hint} COUNT(*) FROM {schema}.{table} t"

    conn.callTimeout = timeout_ms
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        return cursor.fetchone()[0], "EXACT"
    except oracledb.DatabaseError as e:
        (err,) = e.args
        if err.code in (3136, 1013):  # callTimeout / ORA-01013 user requested cancel
            return None, "TIMEOUT"
        raise
    finally:
        conn.callTimeout = 0  # sıfırla


def _count_sample(conn: oracledb.Connection, schema: str, table: str,
                  pct: float, timeout_ms: int, parallel: int) -> tuple[int | None, str]:
    """SAMPLE bazlı tahmin. Dönüş: (tahmini_sayı, kullanılan_mod)"""
    hint = f"/*+ PARALLEL(t, {parallel}) */" if parallel > 0 else ""
    sql = f"SELECT {hint} COUNT(*) FROM {schema}.{table} t SAMPLE({pct})"

    conn.callTimeout = timeout_ms
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        sampled = cursor.fetchone()[0]
        estimated = int(sampled * (100.0 / pct)) if pct > 0 else 0
        return estimated, f"SAMPLE({pct}%)"
    except oracledb.DatabaseError as e:
        (err,) = e.args
        if err.code in (3136, 1013):
            return None, "TIMEOUT"
        raise
    finally:
        conn.callTimeout = 0


def _refresh_stats(conn_cfg, conn: oracledb.Connection, schema: str, table: str, degree: int = 4):
    """DBMS_STATS.GATHER_TABLE_STATS çalıştırır. Read-only bağlantıda engellenir."""
    assert_writable(conn_cfg, "DBMS_STATS.GATHER_TABLE_STATS")
    cursor = conn.cursor()
    cursor.callproc(
        "DBMS_STATS.GATHER_TABLE_STATS",
        keyword_parameters={
            "ownname": schema,
            "tabname": table,
            "estimate_percent": "DBMS_STATS.AUTO_SAMPLE_SIZE",
            "degree": degree,
            "no_invalidate": False,
        }
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Ana run fonksiyonu
# ---------------------------------------------------------------------------

def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
    skip_tables: list[str] = None,
    only_tables: list[str] = None,
) -> ModuleSummary:

    summary = ModuleSummary(module="row_counts")
    rc = cfg.row_count
    timeout_ms = rc.timeout_sec * 1000

    # İstatistikleri yükle
    src_stats = {
        r["table_name"]: r
        for r in fetch_all(src_conn, SQL_TABLE_STATS, {"schema": mapping.source})
    }
    tgt_stats = {
        r["table_name"]: r
        for r in fetch_all(tgt_conn, SQL_TABLE_STATS, {"schema": mapping.target})
    }

    tables = sorted(src_stats.keys())

    if only_tables:
        tables = [t for t in tables if t in only_tables]
    if skip_tables:
        tables = [t for t in tables if t not in skip_tables]

    for table in tables:
        src_row = src_stats.get(table, {})
        tgt_row = tgt_stats.get(table)

        if tgt_row is None:
            # Tablo target'ta yok — tables modülü zaten raporlar
            continue

        src_num_rows = src_row.get("num_rows")
        mode = _resolve_mode(table, src_num_rows, cfg)

        if mode == "skip":
            summary.add(ValidationResult(
                module="row_counts", schema=mapping.source,
                object_type="TABLE", object_name=table,
                status=Status.SKIPPED,
                note="Config gereği atlandı",
            ))
            continue

        dbg("row_counts", f"{mapping.source}.{table} sayılıyor [{mode}]")

        # İstatistik yaşı kontrolü
        last_analyzed = src_row.get("last_analyzed")
        age_days = _stats_age_days(last_analyzed)
        stale_warning = None

        if age_days is None:
            stale_warning = "İstatistik hiç toplanmamış"
        elif age_days > rc.stats_max_age_days:
            stale_warning = f"İstatistik {age_days} gün önce toplandı"

        # --refresh-stats varsa önce istatistik topla.
        # Source read-only ise source'a DBMS_STATS gönderilmez; sadece target yenilenir.
        if rc.refresh_stats and mode in ("stats", "auto"):
            try:
                if cfg.source.read_only:
                    ro_note = "source read-only — istatistik yenilenmedi"
                    stale_warning = f"{stale_warning}; {ro_note}" if stale_warning else ro_note
                else:
                    _refresh_stats(cfg.source, src_conn, mapping.source, table)
                    updated = fetch_one(
                        src_conn,
                        "SELECT num_rows FROM all_tables WHERE owner=:s AND table_name=:t",
                        {"s": mapping.source, "t": table}
                    )
                    if updated:
                        src_num_rows = updated["num_rows"]
                        stale_warning = None

                _refresh_stats(cfg.target, tgt_conn, mapping.target, table)
            except Exception as e:
                stale_warning = f"Stats toplama hatası: {e}"

        # Sayım işlemi
        if mode == "exact":
            src_count, src_mode = _count_exact(
                src_conn, mapping.source, table, timeout_ms, rc.parallel_degree)
            tgt_count, tgt_mode = _count_exact(
                tgt_conn, mapping.target, table, timeout_ms, rc.parallel_degree)

        elif mode == "sample":
            src_count, src_mode = _count_sample(
                src_conn, mapping.source, table, rc.sample_pct, timeout_ms, rc.parallel_degree)
            tgt_count, tgt_mode = _count_sample(
                tgt_conn, mapping.target, table, rc.sample_pct, timeout_ms, rc.parallel_degree)

        else:  # stats
            src_count = src_num_rows
            tgt_count = tgt_row.get("num_rows")
            src_mode = tgt_mode = "STATS"

        # Timeout kontrolü
        if src_count is None or tgt_count is None:
            summary.add(ValidationResult(
                module="row_counts", schema=mapping.source,
                object_type="TABLE", object_name=table,
                status=Status.TIMEOUT,
                source_value=str(src_count) if src_count else "TIMEOUT",
                target_value=str(tgt_count) if tgt_count else "TIMEOUT",
                note=f"Sorgu >{rc.timeout_sec}s — tabloyu --skip-tables ile atlayın",
            ))
            continue

        # Sayı karşılaştırması
        if src_count is None and tgt_count is None:
            status = Status.SKIPPED
            note = "Her iki tarafta da istatistik yok"
        elif src_count == tgt_count:
            status = Status.PASS
            note = stale_warning
        else:
            diff = abs(src_count - tgt_count)
            pct_diff = (diff / max(src_count, 1)) * 100
            # sample modunda %5'e kadar tolerans
            tolerance = 5.0 if "SAMPLE" in src_mode else 0.0

            if pct_diff <= tolerance:
                status = Status.WARNING
                note = f"Fark: {diff:,} ({pct_diff:.1f}%) — örnekleme toleransı içinde"
            else:
                status = Status.FAIL
                note = f"Fark: {diff:,} ({pct_diff:.1f}%)"

            if stale_warning:
                note = f"{stale_warning}; {note}"

        summary.add(ValidationResult(
            module="row_counts", schema=mapping.source,
            object_type="TABLE", object_name=table,
            status=status,
            source_value=f"{src_count:,} [{src_mode}]" if src_count is not None else "N/A",
            target_value=f"{tgt_count:,} [{tgt_mode}]" if tgt_count is not None else "N/A",
            note=note,
        ))

    return summary
