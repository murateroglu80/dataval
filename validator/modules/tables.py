"""
Tables modülü — tablo varlığı ve kolon yapısı karşılaştırması.
11g → 19c bilinen farklar (LOB storage, segment creation) WARNING olarak işaretlenir.

Constraint karşılaştırması artık ayrı `constraints` modülündedir (config'teki
`modules.constraints` bayrağıyla yönetilir). `SQL_TABLES` burada tanımlı kalır;
`constraints` modülü ortak tablo kümesini hesaplamak için onu import eder.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status
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
ORDER BY table_name
"""

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

    src_tables = {r["table_name"] for r in fetch_all(src_conn, SQL_TABLES, {"schema": mapping.source})}
    tgt_tables = {r["table_name"] for r in fetch_all(tgt_conn, SQL_TABLES, {"schema": mapping.target})}

    for tbl in sorted(src_tables - tgt_tables):
        summary.add(ValidationResult(
            module="tables", schema=mapping.source,
            object_type="TABLE", object_name=tbl,
            status=Status.FAIL,
            note="Target'ta tablo mevcut degil",
        ))

    for tbl in sorted(tgt_tables - src_tables):
        summary.add(ValidationResult(
            module="tables", schema=mapping.source,
            object_type="TABLE", object_name=tbl,
            status=Status.WARNING,
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
                status=Status.FAIL,
                source_value=_col_signature(s_cols[col]),
                target_value="(yok)",
                note="Kolon target'ta eksik",
            ))

        for col in sorted(set(t_cols) - set(s_cols)):
            summary.add(ValidationResult(
                module="tables", schema=mapping.source,
                object_type="COLUMN", object_name=f"{tbl}.{col}",
                status=Status.WARNING,
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
                    diffs.append(f"tip: {src_sig}->{tgt_sig}")

            if sr["nullable"] != tr["nullable"]:
                diffs.append(f"nullable: {sr['nullable']}->{tr['nullable']}")

            s_def = (sr["data_default"] or "").strip()
            t_def = (tr["data_default"] or "").strip()
            if s_def != t_def:
                diffs.append(f"default: '{s_def}'->'{t_def}'")

            if diffs:
                summary.add(ValidationResult(
                    module="tables", schema=mapping.source,
                    object_type="COLUMN", object_name=f"{tbl}.{col}",
                    status=Status.FAIL,
                    source_value=src_sig,
                    target_value=tgt_sig,
                    note="; ".join(diffs),
                ))
            else:
                summary.add(ValidationResult(
                    module="tables", schema=mapping.source,
                    object_type="COLUMN", object_name=f"{tbl}.{col}",
                    status=Status.PASS,
                    source_value=src_sig,
                    target_value=tgt_sig,
                ))

    return summary
