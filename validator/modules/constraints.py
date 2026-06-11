"""
Constraints modülü — PK/UK/FK/CHECK constraint karşılaştırması (11g → 19c).

Önceden bu mantık `tables` modülünün içine gömülüydü ve config'teki
`modules.constraints` bayrağını bypass ediyordu. Artık first-class bir modül:
kendi `ModuleSummary`'sini üretir, `run.py`'deki router tarafından yalnızca
`constraints` bayrağı/`--modules constraints` aktifken çağrılır.
"""

import re
import oracledb
from validator.connection import fetch_all
from validator.modules.tables import tables_sql
from validator.result import ValidationResult, ModuleSummary, Status, extra_status
from validator.config_loader import AppConfig, SchemaMapping

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Constraint envanteri — PK/UK/FK/CHECK.
# ÖNEMLİ: search_condition burada SELECT EDİLMEZ. Oracle 11g'de search_condition
# LONG tipindedir; LONG bir kolon, aynı SELECT içinde scalar subquery / LISTAGG /
# ORDER BY ile birlikte taşınamaz → execute anında ORA-00932 ("expected CHAR got
# LONG"). Bu yüzden CHECK koşulları ayrı, sade bir sorguyla çekilir (aşağıya bkz).
SQL_CONSTRAINTS = """
SELECT
    c.table_name,
    c.constraint_name,
    c.constraint_type,
    c.status,
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

# CHECK koşulları — yalnızca LONG kolon (search_condition), tek tablodan, subquery /
# aggregate / ORDER BY OLMADAN. Bu sade biçim 11g'de LONG'u güvenle döndürür.
# Sonuç, _compare_constraints içinde constraint_name ile ana sorguya eşlenir.
SQL_CHECK_CONDITIONS = """
SELECT constraint_name, search_condition
FROM all_constraints
WHERE owner = :schema
  AND constraint_type = 'C'
  AND constraint_name NOT LIKE 'BIN$%'
  AND constraint_name NOT LIKE 'SYS_%'
"""


# ---------------------------------------------------------------------------
# Ana run fonksiyonu
# ---------------------------------------------------------------------------

def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="constraints")

    # Ortak tablo kümesi: constraint'ler yalnızca iki tarafta da var olan tablolar
    # için karşılaştırılır. Eksik/fazla tablolar zaten `tables` modülünde raporlanır.
    # tables_sql ile AYNI kapsam (GTT filtresi include_temp_tables'a bağlı).
    sql_tables = tables_sql(cfg.modules.include_temp_tables)
    src_tables = {r["table_name"] for r in fetch_all(src_conn, sql_tables, {"schema": mapping.source})}
    tgt_tables = {r["table_name"] for r in fetch_all(tgt_conn, sql_tables, {"schema": mapping.target})}
    common_tables = src_tables & tgt_tables

    _compare_constraints(src_conn, tgt_conn, mapping, common_tables, summary,
                         extra_status(cfg.output.extra_as))
    return summary


def _normalize_condition(cond) -> str:
    """
    CHECK koşul metnini sürümler-arası karşılaştırma için normalleştirir.
    11g ↔ 19c arasında boşluk, çift tırnak ve harf büyüklüğü farklılıkları
    yanlış FAIL üretebilir; bunları sönümler.
    """
    if cond is None:
        return ""
    text = str(cond)
    text = text.replace('"', '')           # identifier tırnaklarını kaldır
    text = re.sub(r"\s+", " ", text)        # ardışık boşlukları tekille
    return text.strip().upper()


def _compare_constraints(src_conn, tgt_conn, mapping, common_tables, summary, extra):
    src_rows = fetch_all(src_conn, SQL_CONSTRAINTS, {"schema": mapping.source})
    tgt_rows = fetch_all(tgt_conn, SQL_CONSTRAINTS, {"schema": mapping.target})

    # CHECK koşulları ayrı sorguyla (LONG izolasyonu — ORA-00932 önlenir),
    # constraint_name → normalize edilmiş koşul olarak eşlenir.
    src_cond = {
        r["constraint_name"]: _normalize_condition(r["search_condition"])
        for r in fetch_all(src_conn, SQL_CHECK_CONDITIONS, {"schema": mapping.source})
    }
    tgt_cond = {
        r["constraint_name"]: _normalize_condition(r["search_condition"])
        for r in fetch_all(tgt_conn, SQL_CHECK_CONDITIONS, {"schema": mapping.target})
    }

    TYPE_LABEL = {"P": "PK", "U": "UK", "R": "FK", "C": "CHECK"}

    def _key(r, conditions):
        cond = conditions.get(r["constraint_name"], "") if r["constraint_type"] == "C" else ""
        return (r["constraint_type"], r["columns"] or "", cond)

    def _index(rows, conditions):
        idx = {}
        for r in rows:
            if r["table_name"] not in common_tables:
                continue
            idx.setdefault(r["table_name"], set()).add(_key(r, conditions))
        return idx

    src_c = _index(src_rows, src_cond)
    tgt_c = _index(tgt_rows, tgt_cond)

    for tbl in sorted(common_tables):
        s_set = src_c.get(tbl, set())
        t_set = tgt_c.get(tbl, set())

        for ctype, cols, cond in sorted(s_set - t_set):
            label = TYPE_LABEL.get(ctype, ctype)
            summary.add(ValidationResult(
                module="constraints", schema=mapping.source,
                object_type=f"CONSTRAINT({label})", object_name=tbl,
                status=Status.FAILED,
                source_value=cols or cond or "",
                target_value="(yok)",
                note=f"{label} constraint target'ta eksik",
            ))

        for ctype, cols, cond in sorted(t_set - s_set):
            label = TYPE_LABEL.get(ctype, ctype)
            summary.add(ValidationResult(
                module="constraints", schema=mapping.source,
                object_type=f"CONSTRAINT({label})", object_name=tbl,
                status=extra,
                source_value="(yok)",
                target_value=cols or cond or "",
                note=f"{label} constraint target'ta fazladan mevcut",
            ))
