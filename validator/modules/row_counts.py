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
  - Parallel hint: büyük tablolarda opsiyonel paralel okuma (intra-query)
  - Tablo bazlı override: validation.yaml → row_count.overrides
  - safe_table_ref: tablo/şema adları SQL'e gömülmeden önce regex ile doğrulanır

Paralel sayım (parallel_workers > 1):
  exact/sample sayımları, source ve target için ayrı bağlantı havuzları + ayrı
  ThreadPoolExecutor ile tablolar arası paralel çalışır. Source (production) havuzu
  source_max_workers ile ayrıca sınırlanır. parallel_workers <= 1 → seri yol (varsayılan).
"""

import oracledb
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from validator.connection import (
    fetch_all, fetch_one, assert_writable, safe_table_ref, build_pool,
)
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
    sql = f"SELECT {hint} COUNT(*) FROM {safe_table_ref(schema, table)} t"

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
    sql = f"SELECT {hint} COUNT(*) FROM {safe_table_ref(schema, table)} t SAMPLE({pct})"

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
# Karşılaştırma — seri ve paralel yolun ortak sınıflandırması
# ---------------------------------------------------------------------------

def _classify(src_count, tgt_count, src_mode, tgt_mode, stale_warning, rc):
    """
    İki taraf sayımını statü + görüntü değerleri + nota çevirir.
    Dönüş: (Status, source_value, target_value, note)
    Hem seri hem paralel yol bunu çağırır → karşılaştırma davranışı tek yerde.
    """
    # Timeout: herhangi bir taraf None → doğrulanamadı → FAILED
    if src_count is None or tgt_count is None:
        return (
            Status.FAILED,
            str(src_count) if src_count else "TIMEOUT",
            str(tgt_count) if tgt_count else "TIMEOUT",
            f"Sorgu >{rc.timeout_sec}s — tabloyu --skip-tables ile atlayın",
        )

    if src_count == tgt_count:
        status = Status.SYNC
        note = stale_warning
    else:
        diff = abs(src_count - tgt_count)
        pct_diff = (diff / max(src_count, 1)) * 100
        # sample modunda %5'e kadar tolerans
        tolerance = 5.0 if "SAMPLE" in src_mode else 0.0

        if pct_diff <= tolerance:
            # Örnekleme toleransı içinde → veri eşit kabul edilir (SYNC, not'ta belirtilir).
            status = Status.SYNC
            note = f"Fark: {diff:,} ({pct_diff:.1f}%) — örnekleme toleransı içinde"
        else:
            # Tablo iki tarafta da var ama veri farklı → NOT-SYNC.
            status = Status.NOT_SYNC
            note = f"Fark: {diff:,} ({pct_diff:.1f}%)"

        if stale_warning:
            note = f"{stale_warning}; {note}"

    return (
        status,
        f"{src_count:,} [{src_mode}]",
        f"{tgt_count:,} [{tgt_mode}]",
        note,
    )


def _result(schema, table, status, source_value, target_value, note) -> ValidationResult:
    return ValidationResult(
        module="row_counts", schema=schema,
        object_type="TABLE", object_name=table,
        status=status, source_value=source_value,
        target_value=target_value, note=note,
    )


# ---------------------------------------------------------------------------
# Paralel sayım — taraf-bazlı worker + sonuç birleştirme
# ---------------------------------------------------------------------------

@dataclass
class _SideOutcome:
    """Tek bir tablonun tek bir taraftaki (source veya target) sayım sonucu."""
    table: str
    count: int | None
    mode: str                  # "EXACT" | "SAMPLE(1%)" | "TIMEOUT" | ...
    timed_out: bool = False
    error: str | None = None   # "ORA-00942: ..." | "Geçersiz tanımlayıcı" | ...


def _count_side(pool, schema: str, table: str, mode: str, rc, timeout_ms: int) -> _SideOutcome:
    """
    Havuzdan bir bağlantı alıp tek tabloyu sayar; her hata yapısal _SideOutcome'a
    çevrilir (exception sızdırmaz → bir tablo diğerlerinin sayımını bozmaz).
    """
    # Kimlik doğrulama: geçersiz ad bağlantı bile açtırmadan ERROR olur.
    try:
        safe_table_ref(schema, table)
    except ValueError:
        return _SideOutcome(table, None, mode.upper(), error="Geçersiz Oracle tanımlayıcısı")

    conn = pool.acquire()
    try:
        if mode == "exact":
            count, used = _count_exact(conn, schema, table, timeout_ms, rc.parallel_degree)
        else:  # sample
            count, used = _count_sample(conn, schema, table, rc.sample_pct, timeout_ms, rc.parallel_degree)
        if count is None:
            return _SideOutcome(table, None, used, timed_out=True)
        return _SideOutcome(table, count, used)
    except oracledb.DatabaseError as e:
        (err,) = e.args
        return _SideOutcome(table, None, mode.upper(), error=f"ORA-{err.code}: {err.message.strip()}")
    except Exception as e:  # beklenmeyen — yine de süreç çökmesin
        return _SideOutcome(table, None, mode.upper(), error=str(e))
    finally:
        pool.release(conn)


def _assemble(summary: ModuleSummary, job: dict, so: _SideOutcome, to: _SideOutcome, rc):
    """İki taraf outcome'unu tek ValidationResult'a indirger ve summary'e ekler (ana thread)."""
    schema = job["src_schema"]
    table = job["table"]

    if so.error or to.error:
        parts = []
        if so.error:
            parts.append(f"source: {so.error}")
        if to.error:
            parts.append(f"target: {to.error}")
        summary.add(_result(
            schema, table, Status.FAILED,
            f"{so.count:,}" if so.count is not None else "ERR",
            f"{to.count:,}" if to.count is not None else "ERR",
            "; ".join(parts),
        ))
        return

    src_count = None if so.timed_out else so.count
    tgt_count = None if to.timed_out else to.count
    status, sv, tv, note = _classify(src_count, tgt_count, so.mode, to.mode, job["stale_warning"], rc)
    summary.add(_result(schema, table, status, sv, tv, note))


def _run_parallel(summary: ModuleSummary, jobs: list, cfg: AppConfig, rc, timeout_ms: int):
    """
    exact/sample işlerini source ve target için ayrı havuz + ayrı executor ile paralel sayar.
    Source havuzu source_max_workers ile sınırlanır; target tam parallel_workers hızında.
    """
    target_workers = max(1, rc.parallel_workers)
    source_workers = max(1, min(rc.parallel_workers, rc.source_max_workers))

    src_pool = build_pool(cfg.source, source_workers)
    tgt_pool = None
    try:
        tgt_pool = build_pool(cfg.target, target_workers)

        src_futs: dict = {}
        tgt_futs: dict = {}
        with ThreadPoolExecutor(max_workers=source_workers, thread_name_prefix="src-count") as se, \
             ThreadPoolExecutor(max_workers=target_workers, thread_name_prefix="tgt-count") as te:
            for job in jobs:
                src_futs[job["table"]] = se.submit(
                    _count_side, src_pool, job["src_schema"], job["table"], job["mode"], rc, timeout_ms)
                tgt_futs[job["table"]] = te.submit(
                    _count_side, tgt_pool, job["tgt_schema"], job["table"], job["mode"], rc, timeout_ms)
        # with blokları çıkışında tüm görevler tamamlandı — sonuçları ana thread'de birleştir
        for job in jobs:
            so = src_futs[job["table"]].result()
            to = tgt_futs[job["table"]].result()
            _assemble(summary, job, so, to, rc)
    finally:
        if tgt_pool is not None:
            tgt_pool.close()
        src_pool.close()


def _run_serial(summary: ModuleSummary, jobs: list, src_conn, tgt_conn, rc, timeout_ms: int):
    """exact/sample işlerini mevcut tek bağlantı üzerinde seri sayar (varsayılan davranış)."""
    for job in jobs:
        table = job["table"]
        mode = job["mode"]
        if mode == "exact":
            src_count, src_mode = _count_exact(src_conn, job["src_schema"], table, timeout_ms, rc.parallel_degree)
            tgt_count, tgt_mode = _count_exact(tgt_conn, job["tgt_schema"], table, timeout_ms, rc.parallel_degree)
        else:  # sample
            src_count, src_mode = _count_sample(src_conn, job["src_schema"], table, rc.sample_pct, timeout_ms, rc.parallel_degree)
            tgt_count, tgt_mode = _count_sample(tgt_conn, job["tgt_schema"], table, rc.sample_pct, timeout_ms, rc.parallel_degree)

        status, sv, tv, note = _classify(src_count, tgt_count, src_mode, tgt_mode, job["stale_warning"], rc)
        summary.add(_result(job["src_schema"], table, status, sv, tv, note))


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

    # exact/sample tabloları paralel/seri sayım için kuyruğa alınır; stats/skip
    # ana thread'de hemen işlenir (sorgu gerektirmez).
    count_jobs: list[dict] = []

    for table in tables:
        src_row = src_stats.get(table, {})
        tgt_row = tgt_stats.get(table)

        if tgt_row is None:
            # Tablo target'ta yok — tables modülü zaten raporlar
            continue

        src_num_rows = src_row.get("num_rows")
        mode = _resolve_mode(table, src_num_rows, cfg)

        if mode == "skip":
            summary.add(_result(
                mapping.source, table, Status.SKIPPED, None, None, "Config gereği atlandı"))
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

        if mode == "stats":
            # --refresh-stats varsa önce istatistik topla (sadece stats modunda anlamlı).
            # Source read-only ise source'a DBMS_STATS gönderilmez; sadece target yenilenir.
            if rc.refresh_stats:
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

            src_count = src_num_rows
            tgt_count = tgt_row.get("num_rows")
            status, sv, tv, note = _classify(src_count, tgt_count, "STATS", "STATS", stale_warning, rc)
            summary.add(_result(mapping.source, table, status, sv, tv, note))
            continue

        # exact / sample → sayım kuyruğuna
        count_jobs.append({
            "table": table,
            "mode": mode,
            "src_schema": mapping.source,
            "tgt_schema": mapping.target,
            "stale_warning": stale_warning,
        })

    # Sayım kuyruğunu çalıştır
    if count_jobs:
        if rc.parallel_workers and rc.parallel_workers > 1:
            _run_parallel(summary, count_jobs, cfg, rc, timeout_ms)
        else:
            _run_serial(summary, count_jobs, src_conn, tgt_conn, rc, timeout_ms)

    return summary
