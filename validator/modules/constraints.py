"""
Constraints modülü — PK/UK/FK/CHECK constraint karşılaştırması (11g → 19c).

Eşleştirme **isim-bağımsız**dır: anahtar `(tip, kolon kümesi[, ref tablo / CHECK koşulu])`.
Böylece "farklı isim, aynı kolon" yanlış FAILED üretmez. Ek olarak target tekliği bir
**UNIQUE INDEX (+ NOT NULL)** ile sağlanıyorsa (legacy pseudo-PK/UK), bu constraint
**FAILED değil NOT-SYNC** olarak raporlanır (enforce farkı). Statüler:
  - SYNC      → her iki tarafta da var, ad/durum/sıra aynı
  - NOT-SYNC  → var ama ad / enforce (constraint↔index) / durum / kolon sırası farklı
  - FAILED    → target'ta hiçbir biçimde yok (ne constraint ne covering unique index)
  - extra     → target'ta fazladan (extra_as'a göre)

`modules.constraint_types` ile tip filtrelenebilir (ALL veya alt küme).
`run.py` router'ı yalnız `constraints` bayrağı/`--modules constraints` aktifken çağırır.
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

# Constraint envanteri — PK/UK/FK/CHECK. search_condition (LONG) AYRI çekilir (ORA-00932).
# SYS_% DIŞLANMAZ: eşleştirme kolon-bazlı olduğundan sistem-isimli PK/UK de kapsanır;
# yalnız geri-dönüşüm kovası (BIN$) dışlanır.
SQL_CONSTRAINTS = """
SELECT
    c.table_name,
    c.constraint_name,
    c.constraint_type,
    c.status,
    c.r_constraint_name,
    c.delete_rule,
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
ORDER BY c.table_name, c.constraint_type, c.constraint_name
"""

# CHECK koşulları — yalnız LONG kolon, sade sorgu (11g'de güvenli).
SQL_CHECK_CONDITIONS = """
SELECT constraint_name, search_condition
FROM all_constraints
WHERE owner = :schema
  AND constraint_type = 'C'
  AND constraint_name NOT LIKE 'BIN$%'
"""

# Target pseudo-constraint türetme: unique index'ler + NOT NULL kolonlar.
SQL_UNIQUE_INDEXES = """
SELECT i.index_name, i.table_name,
       (SELECT LISTAGG(ic.column_name, ',') WITHIN GROUP (ORDER BY ic.column_position)
          FROM all_ind_columns ic
         WHERE ic.index_owner = i.owner AND ic.index_name = i.index_name) AS columns
  FROM all_indexes i
 WHERE i.owner = :schema
   AND i.uniqueness = 'UNIQUE'
   AND i.index_name NOT LIKE 'BIN$%'
"""

SQL_NOT_NULL_COLS = """
SELECT table_name, column_name
  FROM all_tab_columns
 WHERE owner = :schema AND nullable = 'N'
"""

CTYPE_LABEL = {"P": "PK", "U": "UK", "R": "FK", "C": "CHECK"}
LABEL_CTYPE = {v: k for k, v in CTYPE_LABEL.items()}


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

    # constraint_types filtresi → ctype kümesi
    allowed_labels = getattr(cfg.modules, "constraint_types", None) or set(LABEL_CTYPE)
    allowed_ctypes = {LABEL_CTYPE[l] for l in allowed_labels if l in LABEL_CTYPE}

    # Ortak tablo kümesi (tables_sql ile AYNI kapsam — GTT filtresi include_temp_tables'a bağlı).
    sql_tables = tables_sql(cfg.modules.include_temp_tables)
    src_tables = {r["table_name"] for r in fetch_all(src_conn, sql_tables, {"schema": mapping.source})}
    tgt_tables = {r["table_name"] for r in fetch_all(tgt_conn, sql_tables, {"schema": mapping.target})}
    common_tables = src_tables & tgt_tables

    _compare_constraints(src_conn, tgt_conn, mapping, common_tables, summary,
                         extra_status(cfg.output.extra_as), allowed_ctypes)
    return summary


def _normalize_condition(cond) -> str:
    """CHECK koşulunu sürümler-arası karşılaştırma için normalleştirir (tırnak/boşluk/case)."""
    if cond is None:
        return ""
    text = str(cond)
    text = text.replace('"', '')
    text = re.sub(r"\s+", " ", text)
    return text.strip().upper()


def _is_system_name(name) -> bool:
    """Oracle sistem-üretimli constraint adı mı (SYS_C...)? Ad-farkı bunlarda anlamsızdır."""
    return bool(name) and str(name).upper().startswith("SYS_")


def _match_key(rec: dict, cond: str, name_to_table: dict):
    """İsim-bağımsız eşleştirme anahtarı. P/U: kolon kümesi; R: +ref tablo; C: koşul."""
    ctype = rec["constraint_type"]
    if ctype == "C":
        return ("C", cond)
    cset = frozenset(c for c in (rec.get("columns") or "").split(",") if c)
    if ctype == "R":
        ref_tbl = name_to_table.get(rec.get("r_constraint_name"), "")
        return ("R", cset, ref_tbl)
    return (ctype, cset)


def _diffs(s: dict, t: dict) -> list:
    """Eşleşen iki constraint arasındaki granüler farklar (boşsa SYNC)."""
    diffs = []
    sn, tn = s.get("constraint_name"), t.get("constraint_name")
    if sn != tn and not (_is_system_name(sn) and _is_system_name(tn)):
        diffs.append(("ad", sn, tn))
    if (s.get("status") or "") != (t.get("status") or ""):
        diffs.append(("durum", s.get("status"), t.get("status")))
    if s["constraint_type"] == "R" and (s.get("delete_rule") or "") != (t.get("delete_rule") or ""):
        diffs.append(("delete_rule", s.get("delete_rule"), t.get("delete_rule")))
    if (s.get("columns") or "") != (t.get("columns") or ""):
        diffs.append(("kolon sırası", s.get("columns"), t.get("columns")))
    return diffs


def _build_maps(rows, cond_map, allowed_ctypes, common_tables, name_to_table):
    """rows → {table: {match_key: rec}} (yalnız allowed_ctypes ve ortak tablolar)."""
    out = {}
    for r in rows:
        if r["constraint_type"] not in allowed_ctypes:
            continue
        if r["table_name"] not in common_tables:
            continue
        cond = cond_map.get(r["constraint_name"], "") if r["constraint_type"] == "C" else ""
        mk = _match_key(r, cond, name_to_table)
        out.setdefault(r["table_name"], {})[mk] = r
    return out


def _build_pseudo(uidx_rows, notnull, common_tables, allowed_ctypes):
    """Target unique index'lerinden pseudo-PK/UK türetir: {table: {match_key: index_name}}."""
    out = {}
    want_pk = "P" in allowed_ctypes
    want_uk = "U" in allowed_ctypes
    for r in uidx_rows:
        tbl = r["table_name"]
        if tbl not in common_tables:
            continue
        cset = frozenset(c for c in (r.get("columns") or "").split(",") if c)
        if not cset:
            continue
        if want_uk:
            out.setdefault(tbl, {})[("U", cset)] = r["index_name"]
        if want_pk and cset <= notnull.get(tbl, set()):
            out.setdefault(tbl, {})[("P", cset)] = r["index_name"]
    return out


def _compare_constraints(src_conn, tgt_conn, mapping, common_tables, summary, extra, allowed_ctypes):
    src_rows = fetch_all(src_conn, SQL_CONSTRAINTS, {"schema": mapping.source})
    tgt_rows = fetch_all(tgt_conn, SQL_CONSTRAINTS, {"schema": mapping.target})

    src_cond = {r["constraint_name"]: _normalize_condition(r["search_condition"])
                for r in fetch_all(src_conn, SQL_CHECK_CONDITIONS, {"schema": mapping.source})}
    tgt_cond = {r["constraint_name"]: _normalize_condition(r["search_condition"])
                for r in fetch_all(tgt_conn, SQL_CHECK_CONDITIONS, {"schema": mapping.target})}

    # FK referans çözümü için ad→tablo haritası (filtrelenmemiş tüm satırlardan).
    src_n2t = {r["constraint_name"]: r["table_name"] for r in src_rows}
    tgt_n2t = {r["constraint_name"]: r["table_name"] for r in tgt_rows}

    src_map = _build_maps(src_rows, src_cond, allowed_ctypes, common_tables, src_n2t)
    tgt_map = _build_maps(tgt_rows, tgt_cond, allowed_ctypes, common_tables, tgt_n2t)

    # Target pseudo-constraint'ler (yalnız PK/UK istenmişse sorgula).
    tgt_pseudo = {}
    if {"P", "U"} & allowed_ctypes:
        uidx_rows = fetch_all(tgt_conn, SQL_UNIQUE_INDEXES, {"schema": mapping.target})
        notnull = {}
        for r in fetch_all(tgt_conn, SQL_NOT_NULL_COLS, {"schema": mapping.target}):
            notnull.setdefault(r["table_name"], set()).add(r["column_name"])
        tgt_pseudo = _build_pseudo(uidx_rows, notnull, common_tables, allowed_ctypes)

    for tbl in sorted(common_tables):
        s_map = src_map.get(tbl, {})
        t_map = tgt_map.get(tbl, {})
        p_map = tgt_pseudo.get(tbl, {})
        matched = set()

        for mk, s in s_map.items():
            label = CTYPE_LABEL[s["constraint_type"]]
            sig = s.get("columns") or src_cond.get(s["constraint_name"], "") or ""

            if mk in t_map:
                matched.add(mk)
                diffs = _diffs(s, t_map[mk])
                if diffs:
                    summary.add(ValidationResult(
                        module="constraints", schema=mapping.source,
                        object_type=f"CONSTRAINT({label})", object_name=tbl,
                        status=Status.NOT_SYNC, source_value=sig,
                        target_value=t_map[mk].get("columns") or "", diffs=diffs,
                    ))
                else:
                    summary.add(ValidationResult(
                        module="constraints", schema=mapping.source,
                        object_type=f"CONSTRAINT({label})", object_name=tbl,
                        status=Status.SYNC, source_value=sig,
                        target_value=t_map[mk].get("columns") or "",
                    ))
            elif mk in p_map:
                # Hedefte teklik constraint yerine unique index ile sağlanıyor.
                summary.add(ValidationResult(
                    module="constraints", schema=mapping.source,
                    object_type=f"CONSTRAINT({label})", object_name=tbl,
                    status=Status.NOT_SYNC, source_value=sig,
                    target_value=f"UNIQUE INDEX {p_map[mk]}",
                    diffs=[("enforce", "CONSTRAINT", f"UNIQUE INDEX {p_map[mk]}")],
                    note="Hedefte teklik unique index ile sağlanıyor (constraint değil)",
                ))
            else:
                summary.add(ValidationResult(
                    module="constraints", schema=mapping.source,
                    object_type=f"CONSTRAINT({label})", object_name=tbl,
                    status=Status.FAILED, source_value=sig, target_value="(yok)",
                    note=f"{label} constraint target'ta eksik",
                ))

        for mk, t in t_map.items():
            if mk in matched:
                continue
            label = CTYPE_LABEL[t["constraint_type"]]
            summary.add(ValidationResult(
                module="constraints", schema=mapping.source,
                object_type=f"CONSTRAINT({label})", object_name=tbl,
                status=extra, source_value="(yok)",
                target_value=t.get("columns") or tgt_cond.get(t["constraint_name"], "") or "",
                note=f"{label} constraint target'ta fazladan mevcut",
            ))
