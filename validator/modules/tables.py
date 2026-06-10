"""
Tables modülü — kolon yapısı ve constraint karşılaştırması.
11g → 19c bilinen farklar (LOB storage, segment creation) WARNING olarak işaretlenir.
"""

import re
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

SQL_CONSTRAINTS = """
SELECT
    c.table_name,
    c.constraint_name,
    c.constraint_type,
    c.status,
    c.search_condition,
    (
        SELECT LISTAGG(cc2.column_name, ',') WITHIN GROUP (ORDER BY cc2.position)
        FROM   all_cons_columns cc2
        WHERE  cc2.owner           = c.owner
           AND cc2.constraint_name = c.constraint_name
    ) AS columns
FROM all_constraints c
WHERE c.owner  = :schema
  AND c.constraint_type IN ('P', 'U', 'R', 'C')
  AND c.constraint_name NOT LIKE 'BIN$%'
  AND c.constraint_name NOT LIKE 'SYS_%'
ORDER BY c.table_name, c.constraint_type, c.constraint_name
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
    """
    Karşılaştırma için veri tipini normalize eder.
    Örnek: NUMBER(38,0) ↔ INTEGER gibi farkları tolere eder.
    """
    dtype = (dtype or "").upper().strip()

    # VARCHAR2 / NVARCHAR2 uzunluk bilgisi ile birlikte
    if dtype in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return f"{dtype}({length})"

    # NUMBER — precision/scale
    if dtype == "NUMBER":
        if precision is not None and scale is not None:
            return f"NUMBER({precision},{scale})"
        if precision is not None:
            return f"NUMBER({precision})"
        return "NUMBER"

    # FLOAT
    if dtype == "FLOAT":
        return f"FLOAT({precision})" if precision else "FLOAT"

    # CLOB / BLOB — storage farkını ignore (11g BASICFILE, 19c SECUREFILE)
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

    # Source ve target tabloları al
    src_tables = {r["table_name"] for r in fetch_all(src_conn, SQL_TABLES, {"schema": mapping.source})}
    tgt_tables = {r["table_name"] for r in fetch_all(tgt_conn, SQL_TABLES, {"schema": mapping.target})}

    # Tablo varlık kontrolü
    for tbl in sorted(src_tables - tgt_tables):
        summary.add(ValidationResult(
            module="tables", schema=mapping.source,
            object_type="TABLE", object_name=tbl,
            status=Status.FAIL,
            note="Target'ta tablo mevcut değil",
        ))

    for tbl in sorted(tgt_tables - src_tables):
        summary.add(ValidationResult(
            module="tables", schema=mapping.source,
            object_type="TABLE", object_name=tbl,
            status=Status.WARNING,
            note="Target'ta fazladan tablo var",
        ))

    # Ortak tablolar — kolon karşılaştırması
    src_cols_raw = fetch_all(src_conn, SQL_COLUMNS, {"schema": mapping.source})
    tgt_cols_raw = fetch_all(tgt_conn, SQL_COLUMNS, {"schema": mapping.target})

    # {table_name: {col_name: row}}
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

        # Eksik kolonlar
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

        # Ortak kolonlar — tip/nullable karşılaştırması
        for col in sorted(set(s_cols) & set(t_cols)):
            sr = s_cols[col]
            tr = t_cols[col]

            src_sig = _col_signature(sr)
            tgt_sig = _col_signature(tr)

            diffs = []

            if src_sig != tgt_sig:
                # LOB storage farkını config'e göre değerlendir
                if cfg.ignore.lob_storage and sr["data_type"] in ("CLOB","BLOB","NCLOB"):
                    pass  # ignore
                else:
                    diffs.append(f"tip: {src_sig}→{tgt_sig}")

            if sr["nullable"] != tr["nullable"]:
                diffs.append(f"nullable: {sr['nullable']}→{tr['nullable']}")

            # default değer — boşlukları trim edip karşılaştır
            s_def = (sr["data_default"] or "").strip()
            t_def = (tr["data_default"] or "").strip()
            if s_def != t_def:
                diffs.append(f"default: '{s_def}'→'{t_def}'")

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

    # ------------------------------------------------------------------
    # Constraint karşılaştırması
    # ------------------------------------------------------------------
    _compare_constraints(src_conn, tgt_conn, mapping, src_tables & tgt_tables, summary)

    return summary


def _compare_constraints(src_conn, tgt_conn, mapping, common_tables, summary):
    src_rows = fetch_all(src_conn, SQL_CONSTRAINTS, {"schema": mapping.source})
    tgt_rows = fetch_all(tgt_conn, SQL_CONSTRAINTS, {"schema": mapping.target})

    TYPE_LABEL = {"P": "PK", "U": "UK", "R": "FK", "C": "CHECK"}

    # {table_name: [(type, columns, search_condition), ...]}
    def _key(r):
        return (r["constraint_type"], r["columns"] or "", r["search_condition"] or "")

    def _index(rows):
        idx = {}
        for r in rows:
            if r["table_name"] not in common_tables:
                continue
            idx.setdefault(r["table_name"], set()).add(_key(r))
        return idx

    src_c = _index(src_rows)
    tgt_c = _index(tgt_rows)

    for tbl in sorted(common_tables):
        s_set = src_c.get(tbl, set())
        t_set = tgt_c.get(tbl, set())

        for ctype, cols, cond in sorted(s_set - t_set):
            label = TYPE_LABEL.get(ctype, ctype)
            summary.add(ValidationResult(
                module="tables", schema=mapping.source,
                object_type=f"CONSTRAINT({label})", object_name=tbl,
                status=Status.FAIL,
                source_value=cols or cond or "",
                target_value="(yok)",
                note=f"{label} constraint target'ta eksik",
            ))

        for ctype, cols, cond in sorted(t_set - s_set):
            label = TYPE_LABEL.get(ctype, ctype)
            summary.add(ValidationResult(
                module="tables", schema=mapping.source,
            