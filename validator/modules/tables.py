"""
Tables modülü — tablo varlığı ve kolon yapısı karşılaştırması.

Statüler: eşit kolon → SYNC; tip/nullable/default farkı → NOT_SYNC (granüler `diffs`);
target'ta eksik tablo/kolon → FAILED; target'ta fazla → cfg.output.extra_as.

Constraint karşılaştırması ayrı `constraints` modülündedir. `tables_sql(include_temp)`
ortak tablo sorgusunu üretir (GTT filtresi dahil); `constraints` modülü AYNI kapsamı
görmek için onu import eder.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status, extra_status
from validator.config_loader import AppConfig, SchemaMapping

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_COLUMNS = """
SELECT
    table_name,
    column_name,
    column_id,
    data_type,
    data_length,
    data_precision,
    data_scale,
    nullable,
    data_default
FROM all_tab_columns
WHERE owner = :schema
ORDER BY table_name, column_id
"""

SQL_TABLES = """
SELECT table_name
FROM   all_tables
WHERE  owner = :schema
  AND  table_name NOT LIKE 'BIN$%'
{temp_filter}
ORDER BY table_name
"""


def tables_sql(include_temp: bool = False) -> str:
    """
    Ortak tablo sorgusunu döner. include_temp False (default) iken Global Temporary
    Table'ları (temporary='Y') eler. tables ve constraints modülleri AYNI kapsamı
    görsün diye her ikisi de bu yardımcıyı kullanır.
    """
    temp_filter = "" if include_temp else "  AND  temporary = 'N'"
    return SQL_TABLES.format(temp_filter=temp_filter)

# ---------------------------------------------------------------------------
# Yardımcı: tip normalleştirme (11g ↔ 19c uyumu)
# ---------------------------------------------------------------------------

def _normalize_type(dtype: str, precision, scale, length) -> str:
    dtype = (dtype or "").upper().strip()
    if dtype in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return f"{dtype}({length})"
    if dtype == "NUMBER":
        if precision is not None and scale is not None:
            return f"NUMBER({precision},{scale})"
        if precision is not None:
            return f"NUMBER({precision})"
        return "NUMBER"
    if dtype == "FLOAT":
        return f"FLOAT({precision})" if precision else "FLOAT"
    if dtype in ("CLOB", "BLOB", "NCLOB"):
        return dtype
    return dtype


def _col_signature(row: dict) -> str:
    return _normalize_type(
        row["data_type"],
        row["data_precision"],
        row["data_scale"],
        row["data_length"],
    )


# ---------------------------------------------------------------------------
# Ana run fonksiyonu
# ---------------------------------------------------------------------------

def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="tables")
    sql_tables = tables_sql(cfg.modules.include_temp_tables)
    extra = extra_status(cfg.output.extra_as)

    src_tables = {r["table_name"] for r in fetch_all(src_conn, sql_tables, {"schema": mapping.source})}
    tgt_tables = {r["table_name"] for r in fetch_all(tgt_conn, sql_tables, {"schema": mapping.target})}

    for tbl in sorted(src_tables - tgt_tables):
        summary.add(ValidationResult(
            module="tables", schema=mapping.source,
            object_type="TABLE", object_name=tbl,
            status=Status.FAILED,
            target_value="(yok)",
            note="Target'ta tablo mevcut değil",
        ))

    for tbl in sorted(tgt_tables - src_tables):
        summary.add(ValidationResult(
            module="tables", schema=mapping.source,
            object_type="TABLE", object_name=tbl,
            status=extra,
            source_value="(yok)",
            note="Target'ta fazladan tablo var",
        ))

    src_cols_raw = fetch_all(src_conn, SQL_COLUMNS, {"schema": mapping.source})
    tgt_cols_raw = fetch_all(tgt_conn, SQL_COLUMNS, {"schema": mapping.target})

    def _index(rows):
        idx = {}
        for r in rows:
            idx.setdefault(r["table_name"], {})[r["column_name"]] = r
        return idx

    src_idx = _index(src_cols_raw)
    tgt_idx = _index(tgt_cols_raw)

    for tbl in sorted(src_tables & tgt_tables):
        s_cols = src_idx.get(tbl, {})
        t_cols = tgt_idx.get(tbl, {})

        for col in sorted(set(s_cols) - set(t_cols)):
            summary.add(ValidationResult(
                module="tables", schema=mapping.source,
                object_type="COLUMN", object_name=f"{tbl}.{col}",
                status=Status.FAILED,
                source_value=_col_signature(s_cols[col]),
                target_value="(yok)",
                note="Kolon target'ta eksik",
            ))

        for col in sorted(set(t_cols) - set(s_cols)):
            summary.add(ValidationResult(
                module="tables", schema=mapping.source,
                object_type="COLUMN", object_name=f"{tbl}.{col}",
                status=extra,
                source_value="(yok)",
                target_value=_col_signature(t_cols[col]),
                note="Kolon target'ta fazladan mevcut",
            ))

        for col in sorted(set(s_cols) & set(t_cols)):
            sr = s_cols[col]
            tr = t_cols[col]
            src_sig = _col_signature(sr)
            tgt_sig = _col_signature(tr)
            diffs = []

            if src_sig != tgt_sig:
                if cfg.ignore.lob_storage and sr["data_type"] in ("CLOB","BLOB","NCLOB"):
                    pass
                else:
                    diffs.append(("tip", src_sig, tgt_sig))

            if sr["nullable"] != tr["nullable"]:
                diffs.append(("nullable", sr["nullable"], tr["nullable"]))

            s_def = (sr["data_default"] or "").strip()
            t_def = (tr["data_default"] or "").strip()
            if s_def != t_def:
                diffs.append(("default", s_def or "-", t_def or "-"))

            if diffs:
                summary.add(ValidationResult(
                    module="tables", schema=mapping.source,
                    object_type="COLUMN", object_name=f"{tbl}.{col}",
                    status=Status.NOT_SYNC,
                    source_value=src_sig,
                    target_value=tgt_sig,
                    diffs=diffs,
                ))
            else:
                summary.add(ValidationResult(
                    module="tables", schema=mapping.source,
                    object_type="COLUMN", object_name=f"{tbl}.{col}",
                    status=Status.SYNC,
                    source_value=src_sig,
                    target_value=tgt_sig,
                ))

    return summary
