"""
Inventory modülü — her obje tipinde source vs target sayı karşılaştırması.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status
from validator.config_loader import AppConfig, SchemaMapping

# ALL_OBJECTS'te görünen, migration kapsamında olan obje tipleri
TRACKED_OBJECT_TYPES = [
    "TABLE",
    "VIEW",
    "INDEX",
    "SEQUENCE",
    "PROCEDURE",
    "FUNCTION",
    "PACKAGE",
    "PACKAGE BODY",
    "TRIGGER",
    "TYPE",
    "TYPE BODY",
    "SYNONYM",
    "DATABASE LINK",
    "MATERIALIZED VIEW",
    "JOB",
]

SQL_OBJECT_COUNTS = """
SELECT object_type, COUNT(*) AS cnt
FROM   all_objects
WHERE  owner = :schema
  AND  object_type IN ({placeholders})
  AND  object_name NOT LIKE 'BIN$%'
GROUP BY object_type
ORDER BY object_type
"""


def _get_counts(conn: oracledb.Connection, schema: str) -> dict[str, int]:
    placeholders = ", ".join(f"'{t}'" for t in TRACKED_OBJECT_TYPES)
    sql = SQL_OBJECT_COUNTS.format(placeholders=placeholders)
    rows = fetch_all(conn, sql, {"schema": schema})
    return {r["object_type"]: r["cnt"] for r in rows}


def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="inventory")

    src_counts = _get_counts(src_conn, mapping.source)
    tgt_counts = _get_counts(tgt_conn, mapping.target)

    all_types = sorted(set(src_counts) | set(tgt_counts))

    for obj_type in all_types:
        src_n = src_counts.get(obj_type, 0)
        tgt_n = tgt_counts.get(obj_type, 0)

        if src_n == tgt_n:
            status = Status.PASS
            note   = None
        elif src_n > tgt_n:
            status = Status.FAIL
            note   = f"Target'ta {src_n - tgt_n} adet eksik"
        else:
            status = Status.WARNING
            note   = f"Target'ta {tgt_n - src_n} adet fazla"

        summary.add(ValidationResult(
            module="inventory",
            schema=mapping.source,
            object_type=obj_type,
            object_name="(toplam)",
            status=status,
            source_value=str(src_n),
            target_value=str(tgt_n),
            note=note,
        ))

    return summary
