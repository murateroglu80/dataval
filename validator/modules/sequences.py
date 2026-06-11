"""
Sequences modülü — sequence parametre karşılaştırması.
LAST_NUMBER için config'den gelen tolerans yüzdesi uygulanır.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status, extra_status
from validator.config_loader import AppConfig, SchemaMapping

SQL_SEQUENCES = """
SELECT
    sequence_name,
    min_value,
    max_value,
    increment_by,
    cycle_flag,
    order_flag,
    cache_size,
    last_number
FROM all_sequences
WHERE sequence_owner = :schema
ORDER BY sequence_name
"""


def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="sequences")
    extra = extra_status(cfg.output.extra_as)

    src_seqs = {r["sequence_name"]: r
                for r in fetch_all(src_conn, SQL_SEQUENCES, {"schema": mapping.source})}
    tgt_seqs = {r["sequence_name"]: r
                for r in fetch_all(tgt_conn, SQL_SEQUENCES, {"schema": mapping.target})}

    # Eksik / fazla
    for name in sorted(set(src_seqs) - set(tgt_seqs)):
        summary.add(ValidationResult(
            module="sequences", schema=mapping.source,
            object_type="SEQUENCE", object_name=name,
            status=Status.FAILED,
            target_value="(yok)",
            note="Target'ta sequence mevcut değil",
        ))

    for name in sorted(set(tgt_seqs) - set(src_seqs)):
        summary.add(ValidationResult(
            module="sequences", schema=mapping.source,
            object_type="SEQUENCE", object_name=name,
            status=extra,
            source_value="(yok)",
            note="Target'ta fazladan sequence var",
        ))

    # Ortak — parametre karşılaştırması
    # LAST_NUMBER toleransı: validation.yaml'da sequences.last_number_tolerance_pct
    # Şimdilik cfg.row_count altında değil ama ileride ayrı bir sequences config eklenebilir
    # Default %10 tolerans
    last_num_tol = 10.0  # ileride config'e taşınabilir

    for name in sorted(set(src_seqs) & set(tgt_seqs)):
        sr = src_seqs[name]
        tr = tgt_seqs[name]

        diffs = []

        for field in ("min_value", "max_value", "increment_by", "cycle_flag",
                      "order_flag", "cache_size"):
            sv = str(sr[field]) if sr[field] is not None else "NULL"
            tv = str(tr[field]) if tr[field] is not None else "NULL"
            if sv != tv:
                diffs.append((field, sv, tv))

        # LAST_NUMBER — tolerans ile kontrol
        last_note = None
        src_last = sr.get("last_number") or 0
        tgt_last = tr.get("last_number") or 0
        if src_last != tgt_last:
            diff_pct = abs(src_last - tgt_last) / max(abs(src_last), 1) * 100
            if diff_pct > last_num_tol:
                diffs.append(("last_number", str(src_last), f"{tgt_last} ({diff_pct:.1f}% fark)"))
            else:
                last_note = f"last_number farkı {diff_pct:.1f}% (tolerans içinde)"

        if diffs:
            summary.add(ValidationResult(
                module="sequences", schema=mapping.source,
                object_type="SEQUENCE", object_name=name,
                status=Status.NOT_SYNC,
                source_value=f"inc={sr['increment_by']},cache={sr['cache_size']}",
                target_value=f"inc={tr['increment_by']},cache={tr['cache_size']}",
                diffs=diffs,
            ))
        else:
            summary.add(ValidationResult(
                module="sequences", schema=mapping.source,
                object_type="SEQUENCE", object_name=name,
                status=Status.SYNC,
                source_value=f"inc={sr['increment_by']},cache={sr['cache_size']}",
                target_value=f"inc={tr['increment_by']},cache={tr['cache_size']}",
                note=last_note,
            ))

    return summary
