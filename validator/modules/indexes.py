"""
Indexes modülü — index yapısı karşılaştırması.
Storage, compression ve tablespace farklarını config'e göre ignore eder.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status
from validator.config_loader import AppConfig, SchemaMapping

SQL_INDEXES = """
SELECT
    i.index_name,
    i.table_name,
    i.index_type,
    i.uniqueness,
    i.status,
    i.partitioned,
    LISTAGG(ic.column_name || CASE WHEN ic.descend = 'DESC' THEN ':DESC' ELSE '' END,
            ',') WITHIN GROUP (ORDER BY ic.column_position) AS columns
FROM all_indexes     i
JOIN all_ind_columns ic
    ON ic.index_owner = i.owner
   AND ic.index_name  = i.index_name
WHERE i.owner = :schema
  AND i.index_name NOT LIKE 'BIN$%'
  AND i.index_name NOT LIKE 'SYS_%'
GROUP BY i.index_name, i.table_name, i.index_type,
         i.uniqueness, i.status, i.partitioned
ORDER BY i.table_name, i.index_name
"""


def _signature(row: dict) -> str:
    """Index'in karşılaştırmada kullanılacak imzasını oluşturur."""
    return f"{row['index_type']}|{row['uniqueness']}|{row['columns']}"


def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="indexes")

    src_rows = fetch_all(src_conn, SQL_INDEXES, {"schema": mapping.source})
    tgt_rows = fetch_all(tgt_conn, SQL_INDEXES, {"schema": mapping.target})

    # {index_name: row}
    src_idx = {r["index_name"]: r for r in src_rows}
    tgt_idx = {r["index_name"]: r for r in tgt_rows}

    # Alternatif lookup: table+columns ile eşleştirme (isim farklı olabilir)
    # {(table_name, columns, uniqueness): index_name}
    tgt_by_sig = {}
    for r in tgt_rows:
        key = (_signature(r), r["table_name"])
        tgt_by_sig[key] = r["index_name"]

    checked_tgt = set()

    for idx_name, src_row in sorted(src_idx.items()):
        tgt_row = tgt_idx.get(idx_name)

        if tgt_row is None:
            # Aynı isimde yok — imzayla ara
            sig_key = (_signature(src_row), src_row["table_name"])
            alt_name = tgt_by_sig.get(sig_key)
            if alt_name:
                tgt_row = tgt_idx[alt_name]
                checked_tgt.add(alt_name)
                summary.add(ValidationResult(
                    module="indexes", schema=mapping.source,
                    object_type="INDEX", object_name=idx_name,
                    status=Status.WARNING,
                    source_value=idx_name,
                    target_value=alt_name,
                    note="Index yeniden isimlendirilmiş (yapı aynı)",
                ))
                continue
            else:
                summary.add(ValidationResult(
                    module="indexes", schema=mapping.source,
                    object_type="INDEX", object_name=idx_name,
                    status=Status.FAIL,
                    source_value=_signature(src_row),
                    target_value="(yok)",
                    note="Index target'ta eksik",
                ))
                continue

        checked_tgt.add(idx_name)

        # Yapısal karşılaştırma
        diffs = []

        if src_row["index_type"] != tgt_row["index_type"]:
            diffs.append(f"tip: {src_row['index_type']}→{tgt_row['index_type']}")

        if src_row["uniqueness"] != tgt_row["uniqueness"]:
            diffs.append(f"uniqueness: {src_row['uniqueness']}→{tgt_row['uniqueness']}")

        if src_row["columns"] != tgt_row["columns"]:
            diffs.append(f"kolonlar: {src_row['columns']}→{tgt_row['columns']}")

        if diffs:
            summary.add(ValidationResult(
                module="indexes", schema=mapping.source,
                object_type="INDEX", object_name=idx_name,
                status=Status.FAIL,
                source_value=_signature(src_row),
                target_value=_signature(tgt_row),
                note="; ".join(diffs),
            ))
        else:
            # Status kontrolü — UNUSABLE varsa WARNING
            status = Status.PASS
            note = None
            if tgt_row["status"] == "UNUSABLE":
                status = Status.WARNING
                note = "Index target'ta UNUSABLE durumunda"

            summary.add(ValidationResult(
                module="indexes", schema=mapping.source,
                object_type="INDEX", object_name=idx_name,
                status=status,
                source_value=_signature(src_row),
                target_value=_signature(tgt_row),
                note=note,
            ))

    # Target'ta fazladan indexler
    for idx_name in sorted(set(tgt_idx) - checked_tgt):
        tgt_row = tgt_idx[idx_name]
        summary.add(ValidationResult(
            module="indexes", schema=mapping.source,
            object_type="INDEX", object_name=idx_name,
            status=Status.WARNING,
            source_value="(yok)",
            target_value=_signature(tgt_row),
            note="Target'ta fazladan index",
        ))

    return summary
