"""
ddl_generator.py — Target'ta eksik objelerin DDL scriptlerini üretir.

Desteklenen tipler: SEQUENCE, FUNCTION, PROCEDURE, PACKAGE, PACKAGE BODY,
                    TRIGGER, TYPE, TYPE BODY, SYNONYM, GRANT

Çıktı: UTF-8, SQL*Plus uyumlu (.sql dosyaları)
Dosya adlandırma: {SOURCE_SCHEMA}_{TYPE}.sql
"""

import re
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from validator.connection import fetch_all, fetch_one
from validator.modules.constraints import _normalize_condition
from validator.result import ValidationResult, Status


# ---------------------------------------------------------------------------
# Uygulama sırası — bağımlılık hiyerarşisi
# ---------------------------------------------------------------------------
APPLY_ORDER = [
    "TYPE",
    "TYPE BODY",
    "SEQUENCE",
    "SYNONYM",
    "INDEX",
    "CONSTRAINT",
    "FUNCTION",
    "PROCEDURE",
    "PACKAGE",
    "PACKAGE BODY",
    "TRIGGER",
    "GRANT",
]

# DDL'i ALL_SOURCE'tan kurulan PL/SQL tipleri (INVALID kontrolü bunlara uygulanır).
PLSQL_TYPES = {
    "FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY",
    "TYPE", "TYPE BODY", "TRIGGER",
}

# PACKAGE seçilince PACKAGE BODY da otomatik eklenir
AUTO_BODY_TYPES = {
    "PACKAGE": "PACKAGE BODY",
    "TYPE":    "TYPE BODY",
}


# ---------------------------------------------------------------------------
# SQL*Plus dosya başlığı
# ---------------------------------------------------------------------------
def _file_header(schema: str, obj_type: str, generated_at: str) -> str:
    return (
        "-- ============================================================\n"
        f"-- dataval — DDL Generate Script\n"
        f"-- Schema  : {schema}\n"
        f"-- Type    : {obj_type}\n"
        f"-- Created : {generated_at}\n"
        "-- ============================================================\n"
        "SET DEFINE OFF\n"
        "SET SERVEROUTPUT ON SIZE UNLIMITED\n"
        "WHENEVER SQLERROR CONTINUE\n"
        "\n"
    )


def _object_header(obj_name: str, status: str, note: str = "") -> str:
    lines = [
        "-- ------------------------------------------------------------\n",
        f"-- Object : {obj_name}\n",
        f"-- Status : {status}\n",
    ]
    if note:
        lines.append(f"-- Note   : {note}\n")
    lines.append("-- ------------------------------------------------------------\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Obje geçerlilik kontrolü
# ---------------------------------------------------------------------------
def _get_object_status(conn, schema: str, obj_type: str, obj_name: str) -> str:
    """ALL_OBJECTS'ten objenin STATUS değerini döndürür."""
    # PACKAGE BODY → PACKAGE BODY, TYPE BODY → TYPE BODY gibi tipler ALL_OBJECTS'te farklı
    sql = """
        SELECT STATUS
          FROM ALL_OBJECTS
         WHERE OWNER       = :schema
           AND OBJECT_TYPE = :obj_type
           AND OBJECT_NAME = :obj_name
    """
    row = fetch_one(conn, sql, {"schema": schema, "obj_type": obj_type, "obj_name": obj_name})
    return row["status"] if row else "UNKNOWN"


# ---------------------------------------------------------------------------
# Sequence — native DDL (DBMS_METADATA KULLANILMAZ)
#
# Oracle 11g'de DBMS_METADATA.GET_DDL'in SELECT ... FROM DUAL içinde bind
# değişkenleriyle çağrılması oturumu çökertiyor (ORA-03113). Sequence için tüm
# parametreler ALL_SEQUENCES'tan zaten okunabildiğinden, CREATE/ALTER DDL'i
# elle (native) kuruyoruz → ORA-03113 tamamen by-pass edilir, üstelik MIN/MAX/
# INCREMENT/CACHE/CYCLE/ORDER ve gerçek LAST_NUMBER birebir korunur.
# ---------------------------------------------------------------------------

# Oracle ASC sequence default MAXVALUE = 28 dokuz (10^28 - 1) → NOMAXVALUE yaz.
_SEQ_MAX_DEFAULT = 10 ** 28 - 1

SQL_SEQ_PARAMS = """
    SELECT LAST_NUMBER, INCREMENT_BY, CACHE_SIZE, CYCLE_FLAG,
           ORDER_FLAG, MIN_VALUE, MAX_VALUE
      FROM ALL_SEQUENCES
     WHERE SEQUENCE_OWNER = :schema
       AND SEQUENCE_NAME  = :name
"""


def _fetch_seq_row(conn, schema: str, name: str) -> Optional[dict]:
    """ALL_SEQUENCES'tan bir sequence'in parametre satırını döner (yoksa None)."""
    return fetch_one(conn, SQL_SEQ_PARAMS, {"schema": schema, "name": name})


def _seq_int(v):
    """NUMBER (int/Decimal/float) değeri tam sayı string'ine indirger; olmazsa olduğu gibi."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _seq_clauses(seq_row: dict) -> list[str]:
    """ALTER/CREATE için ortak öznitelik cümlecikleri (START WITH hariç)."""
    inc   = _seq_int(seq_row.get("increment_by") or 1)
    minv  = seq_row.get("min_value")
    maxv  = seq_row.get("max_value")
    cache = seq_row.get("cache_size")
    cycle = (seq_row.get("cycle_flag") or "N").upper() == "Y"
    order = (seq_row.get("order_flag") or "N").upper() == "Y"

    clauses = [f"INCREMENT BY {inc}"]
    if maxv is not None and _seq_int(maxv) >= _SEQ_MAX_DEFAULT:
        clauses.append("NOMAXVALUE")
    elif maxv is not None:
        clauses.append(f"MAXVALUE {_seq_int(maxv)}")
    if minv is not None:
        clauses.append(f"MINVALUE {_seq_int(minv)}")
    clauses.append("NOCACHE" if (not cache or _seq_int(cache) == 0) else f"CACHE {_seq_int(cache)}")
    clauses.append("CYCLE" if cycle else "NOCYCLE")
    clauses.append("ORDER" if order else "NOORDER")
    return clauses


def _build_create_sequence(seq_row: dict, schema: str, name: str) -> str:
    """ALL_SEQUENCES satırından `CREATE SEQUENCE` DDL'i kurar (terminator yok)."""
    start = _seq_int(seq_row.get("last_number") or 1)
    clauses = _seq_clauses(seq_row)
    # START WITH'i en başa (INCREMENT BY'dan sonra) yerleştir — okunurluk için.
    lines = [f"CREATE SEQUENCE {schema}.{name}"]
    lines.append(f"  {clauses[0]}")            # INCREMENT BY
    lines.append(f"  START WITH {start}")
    for c in clauses[1:]:
        lines.append(f"  {c}")
    return "\n".join(lines)


def _build_alter_sequence(seq_row: dict, schema: str, name: str) -> str:
    """
    NOT-SYNC bir sequence'i source değerlerine hizalayan ALTER bloğu üretir.
    Non-destructive (grant/bağımlılık korunur). Geçerli değer target 18c+'de
    `RESTART START WITH` ile sıfırlanır. DROP+CREATE eşdeğeri yorum satırı olarak
    eklenir (pre-18c target ya da ALTER başarısızsa yedek).
    """
    start   = _seq_int(seq_row.get("last_number") or 1)
    clauses = _seq_clauses(seq_row)

    lines = [
        "-- Yapisal hizalama (INCREMENT/MIN/MAX/CACHE/CYCLE/ORDER):",
        f"ALTER SEQUENCE {schema}.{name}",
        "  " + "\n  ".join(clauses) + ";",
        "",
        "-- Gecerli degeri source ile hizala (target 18c+ gerekir):",
        f"ALTER SEQUENCE {schema}.{name} RESTART START WITH {start};",
        "",
        "-- Alternatif (pre-18c target ya da yukaridaki ALTER basarisizsa) -- DROP + CREATE.",
        "-- DIKKAT: DROP SEQUENCE grant'lari siler ve bagimli objeleri INVALID yapar!",
    ]
    drop_create = f"DROP SEQUENCE {schema}.{name};\n{_build_create_sequence(seq_row, schema, name)};"
    lines += ["-- " + ln for ln in drop_create.splitlines()]
    return "\n".join(lines)


def _get_sequence_ddl(conn, obj_name: str, schema: str,
                      eff_schema: Optional[str] = None) -> Optional[str]:
    """
    SEQUENCE için `CREATE SEQUENCE` DDL'ini ALL_SEQUENCES'tan native kurar
    (DBMS_METADATA yok → ORA-03113 yok). LAST_NUMBER `START WITH` olarak yazılır.
    `schema` = ALL_SEQUENCES sahibi (source); `eff_schema` = çıktıda nitelenecek şema.
    """
    seq_row = _fetch_seq_row(conn, schema, obj_name)
    if not seq_row:
        return None
    return _build_create_sequence(seq_row, eff_schema or schema, obj_name)


# ---------------------------------------------------------------------------
# Native DDL — PL/SQL (ALL_SOURCE), TRIGGER (ALL_TRIGGERS), SYNONYM, INDEX,
# CONSTRAINT. Hiçbiri DBMS_METADATA KULLANMAZ → 11g ORA-03113 tamamen by-pass.
# ---------------------------------------------------------------------------

# CREATE başlığında niteliksiz obje adının önüne hedef şemayı enjekte eden dar
# desen — gövdeyi (blunt _replace_schema'nın aksine) ASLA bozmaz.
_HEADER_KEYWORDS = r"(?:(?:NON)?EDITIONABLE\s+)?(?:PACKAGE\s+BODY|TYPE\s+BODY|PACKAGE|TYPE|FUNCTION|PROCEDURE|TRIGGER)"


def _inject_schema_into_header(ddl: str, target_schema: str, name: str) -> str:
    """`CREATE OR REPLACE PACKAGE pkg` → `... PACKAGE target.pkg` (yalnız ilk başlık)."""
    pat = re.compile(
        r"(CREATE\s+OR\s+REPLACE\s+" + _HEADER_KEYWORDS + r"\s+)(\"?)" + re.escape(name) + r"(\"?)",
        re.IGNORECASE,
    )
    return pat.sub(lambda m: f"{m.group(1)}{target_schema}.{m.group(2)}{name}{m.group(3)}", ddl, count=1)


def _replace_qualified_schema(ddl: str, source_schema: str, target_schema: str) -> str:
    """Yalnız `SOURCE.` biçimindeki nitelikli referansları `TARGET.`'a çevirir (gövde güvenli)."""
    if source_schema.upper() == target_schema.upper():
        return ddl
    pat = re.compile(r"\b" + re.escape(source_schema) + r"\s*\.", re.IGNORECASE)
    return pat.sub(target_schema + ".", ddl)


# --- PL/SQL: ALL_SOURCE -----------------------------------------------------
SQL_SOURCE = """
    SELECT TEXT
      FROM ALL_SOURCE
     WHERE OWNER = :schema AND NAME = :name AND TYPE = :obj_type
     ORDER BY LINE
"""


def _get_source_ddl(conn, obj_type: str, schema: str, name: str,
                    target_schema: str, replace_schema: bool) -> Optional[str]:
    """
    PL/SQL objesinin DDL'ini ALL_SOURCE'tan birebir kurar (DBMS_METADATA yok).
    Kaynak metni niteliksizdir; başına `CREATE OR REPLACE` eklenir, hedef şema
    yalnızca CREATE başlığına enjekte edilir + nitelikli çapraz referanslar repoint edilir.
    """
    rows = fetch_all(conn, SQL_SOURCE, {"schema": schema, "name": name, "obj_type": obj_type})
    if not rows:
        return None
    body = "".join(str(r.get("text") or "") for r in rows)
    if not body.strip():
        return None
    ddl = "CREATE OR REPLACE " + body.lstrip()
    if replace_schema and schema.upper() != target_schema.upper():
        ddl = _inject_schema_into_header(ddl, target_schema, name)
        ddl = _replace_qualified_schema(ddl, schema, target_schema)
    return ddl


# --- TRIGGER: ALL_TRIGGERS --------------------------------------------------
SQL_TRIGGER = """
    SELECT DESCRIPTION, TRIGGER_BODY, STATUS
      FROM ALL_TRIGGERS
     WHERE OWNER = :schema AND TRIGGER_NAME = :name
"""


def _get_trigger_ddl(conn, schema: str, name: str,
                     target_schema: str, replace_schema: bool) -> Optional[str]:
    """
    TRIGGER DDL'ini ALL_TRIGGERS'tan kurar: CREATE OR REPLACE TRIGGER + DESCRIPTION
    + TRIGGER_BODY. WHEN_CLAUSE 11g'de DESCRIPTION içine gömülüdür. DISABLED ise
    ALTER TRIGGER ... DISABLE eklenir.
    """
    row = fetch_one(conn, SQL_TRIGGER, {"schema": schema, "name": name})
    if not row:
        return None
    desc = str(row.get("description") or "").strip()
    tbody = str(row.get("trigger_body") or "")
    if not desc:
        return None
    ddl = "CREATE OR REPLACE TRIGGER " + desc + "\n" + tbody
    repl = replace_schema and schema.upper() != target_schema.upper()
    if repl:
        ddl = _inject_schema_into_header(ddl, target_schema, name)
        ddl = _replace_qualified_schema(ddl, schema, target_schema)
    if str(row.get("status") or "").upper() == "DISABLED":
        eff = target_schema if repl else schema
        ddl = ddl.rstrip() + f"\n/\nALTER TRIGGER {eff}.{name} DISABLE;"
    return ddl


# --- SYNONYM: ALL_SYNONYMS --------------------------------------------------
SQL_SYNONYM = """
    SELECT TABLE_OWNER, TABLE_NAME, DB_LINK
      FROM ALL_SYNONYMS
     WHERE OWNER = :schema AND SYNONYM_NAME = :name
"""


def _get_synonym_ddl(conn, schema: str, name: str, eff_schema: str,
                     target_schema: str, replace_schema: bool) -> Optional[str]:
    """SYNONYM DDL'ini ALL_SYNONYMS'tan kurar."""
    row = fetch_one(conn, SQL_SYNONYM, {"schema": schema, "name": name})
    if not row:
        return None
    towner = row.get("table_owner")
    tname  = row.get("table_name")
    dblink = row.get("db_link")
    if replace_schema and towner and towner.upper() == schema.upper():
        towner = target_schema
    ref = f"{towner}.{tname}" if towner else str(tname)
    if dblink:
        ref += f"@{dblink}"
    return f"CREATE OR REPLACE SYNONYM {eff_schema}.{name} FOR {ref}"


# --- INDEX: ALL_INDEXES + ALL_IND_COLUMNS (+ ALL_IND_EXPRESSIONS) -----------
SQL_INDEX_META = """
    SELECT INDEX_TYPE, UNIQUENESS, TABLE_NAME, TABLE_OWNER
      FROM ALL_INDEXES
     WHERE OWNER = :schema AND INDEX_NAME = :name
"""
SQL_INDEX_COLS = """
    SELECT COLUMN_NAME, DESCEND, COLUMN_POSITION
      FROM ALL_IND_COLUMNS
     WHERE INDEX_OWNER = :schema AND INDEX_NAME = :name
     ORDER BY COLUMN_POSITION
"""
# COLUMN_EXPRESSION 11g'de LONG → tek başına, ORDER BY/aggregate olmadan çekilir.
SQL_INDEX_EXPR = """
    SELECT COLUMN_POSITION, COLUMN_EXPRESSION
      FROM ALL_IND_EXPRESSIONS
     WHERE INDEX_OWNER = :schema AND INDEX_NAME = :name
"""


def _get_index_ddl(conn, schema: str, name: str, eff_schema: str) -> Optional[str]:
    """INDEX DDL'ini ALL_INDEXES + kolon/ifade view'larından kurar (native)."""
    meta = fetch_one(conn, SQL_INDEX_META, {"schema": schema, "name": name})
    if not meta:
        return None
    idx_type = str(meta.get("index_type") or "").upper()
    unique   = str(meta.get("uniqueness") or "").upper() == "UNIQUE"
    table    = meta.get("table_name")
    cols = fetch_all(conn, SQL_INDEX_COLS, {"schema": schema, "name": name})
    if not cols:
        return None

    expr_map = {}
    if "FUNCTION-BASED" in idx_type:
        for e in fetch_all(conn, SQL_INDEX_EXPR, {"schema": schema, "name": name}):
            expr_map[e.get("column_position")] = str(e.get("column_expression") or "")

    parts = []
    for c in cols:
        pos = c.get("column_position")
        if pos in expr_map and expr_map[pos]:
            col = expr_map[pos]
        else:
            col = str(c.get("column_name"))
            if str(c.get("descend") or "").upper() == "DESC":
                col += " DESC"
        parts.append(col)

    kind = "BITMAP " if "BITMAP" in idx_type else ("UNIQUE " if unique else "")
    return (
        f"CREATE {kind}INDEX {eff_schema}.{name}\n"
        f"  ON {eff_schema}.{table} ({', '.join(parts)})"
    )


# --- CONSTRAINT: ALL_CONSTRAINTS + ALL_CONS_COLUMNS -------------------------
_LABEL_TO_CTYPE = {"PK": "P", "UK": "U", "FK": "R", "CHECK": "C"}

SQL_CONS_META = """
    SELECT c.constraint_name, c.constraint_type, c.r_owner, c.r_constraint_name,
           c.delete_rule,
           (SELECT LISTAGG(cc.column_name, ',') WITHIN GROUP (ORDER BY cc.position)
              FROM all_cons_columns cc
             WHERE cc.owner = c.owner AND cc.constraint_name = c.constraint_name) AS columns
      FROM all_constraints c
     WHERE c.owner = :schema AND c.table_name = :table
       AND c.constraint_type = :ctype
       AND c.constraint_name NOT LIKE 'BIN$%'
       AND c.constraint_name NOT LIKE 'SYS\\_%' ESCAPE '\\'
"""
# CHECK koşulu (LONG) — sade, ayrı sorgu (ORA-00932 izolasyonu).
SQL_CONS_CHECK = """
    SELECT constraint_name, search_condition
      FROM all_constraints
     WHERE owner = :schema AND table_name = :table AND constraint_type = 'C'
       AND constraint_name NOT LIKE 'BIN$%'
       AND constraint_name NOT LIKE 'SYS\\_%' ESCAPE '\\'
"""
SQL_REF_TABLE = """
    SELECT table_name,
           (SELECT LISTAGG(cc.column_name, ',') WITHIN GROUP (ORDER BY cc.position)
              FROM all_cons_columns cc
             WHERE cc.owner = c.owner AND cc.constraint_name = c.constraint_name) AS columns
      FROM all_constraints c
     WHERE c.owner = :r_owner AND c.constraint_name = :r_name
"""


def _cons_sig_value(columns, ctype: str, search_condition) -> str:
    """constraints.py'deki source_value (=cols or cond) ile birebir aynı imzayı üretir."""
    cols = columns or ""
    cond = _normalize_condition(search_condition) if ctype == "C" else ""
    return cols or cond or ""


def _build_constraint_ddl(row: dict, label: str, eff_schema: str, table: str,
                          check_cond: Optional[str]) -> Optional[str]:
    """PK/UK/CHECK için ALTER TABLE ADD CONSTRAINT üretir (FK ayrı: _build_fk_ddl)."""
    name = row.get("constraint_name")
    cols = row.get("columns") or ""
    head = f"ALTER TABLE {eff_schema}.{table} ADD CONSTRAINT {name}"
    if label == "PK":
        return f"{head} PRIMARY KEY ({cols});"
    if label == "UK":
        return f"{head} UNIQUE ({cols});"
    if label == "CHECK":
        return f"{head} CHECK ({str(check_cond or '').strip()});"
    return None


def _build_fk_ddl(conn, row: dict, eff_schema: str, table: str,
                  source_schema: str, target_schema: str, replace_schema: bool) -> Optional[str]:
    """FK için referans tablo/kolonları çözüp tam ALTER TABLE ... FOREIGN KEY üretir."""
    ref = fetch_one(conn, SQL_REF_TABLE,
                    {"r_owner": row.get("r_owner"), "r_name": row.get("r_constraint_name")})
    if not ref:
        return None
    r_owner = row.get("r_owner")
    # Referans, source şemasını işaret ediyorsa target'a repoint et.
    if replace_schema and r_owner and r_owner.upper() == source_schema.upper():
        r_eff = target_schema
    else:
        r_eff = r_owner
    cols   = row.get("columns") or ""
    rtable = ref.get("table_name")
    rcols  = ref.get("columns") or ""
    rule = str(row.get("delete_rule") or "").upper()
    on_delete = ""
    if rule == "CASCADE":
        on_delete = " ON DELETE CASCADE"
    elif rule == "SET NULL":
        on_delete = " ON DELETE SET NULL"
    return (
        f"ALTER TABLE {eff_schema}.{table} ADD CONSTRAINT {row.get('constraint_name')} "
        f"FOREIGN KEY ({cols}) REFERENCES {r_eff}.{rtable} ({rcols}){on_delete};"
    )


def _get_constraint_ddls(conn, source_schema: str, target_schema: str,
                         replace_schema: bool, specs: list) -> list[tuple]:
    """
    specs: [(table, label, source_value), ...]. Source ALL_CONSTRAINTS'ı (tablo,tip)
    bazında sorgular, imza (source_value) ile eşleşen gerçek constraint'i bulup
    ALTER TABLE ADD CONSTRAINT DDL'i kurar. Döner: [(fk_mi, table, ddl), ...]
    PK/UK/CHECK (0) FK'den (1) önce gelir.
    """
    eff_schema = target_schema if replace_schema else source_schema
    out: list[tuple] = []
    # (table, label) → istenen source_value kümesi
    wanted: dict[tuple, set] = {}
    for table, label, sigval in specs:
        wanted.setdefault((table, label), set()).add(sigval or "")

    for (table, label), sigvals in wanted.items():
        ctype = _LABEL_TO_CTYPE.get(label)
        if not ctype:
            continue
        rows = fetch_all(conn, SQL_CONS_META,
                         {"schema": source_schema, "table": table, "ctype": ctype})
        cond_map = {}
        if ctype == "C":
            for cr in fetch_all(conn, SQL_CONS_CHECK, {"schema": source_schema, "table": table}):
                cond_map[cr.get("constraint_name")] = cr.get("search_condition")

        for row in rows:
            real_cond = cond_map.get(row.get("constraint_name")) if ctype == "C" else None
            sig = _cons_sig_value(row.get("columns"), ctype, real_cond)
            if sig not in sigvals:
                continue
            if label == "FK":
                ddl = _build_fk_ddl(conn, row, eff_schema, table,
                                    source_schema, target_schema, replace_schema)
            else:
                ddl = _build_constraint_ddl(row, label, eff_schema, table, real_cond)
            if ddl:
                out.append((1 if label == "FK" else 0, table, ddl))

    out.sort(key=lambda t: (t[0], t[1]))
    return out


# ---------------------------------------------------------------------------
# GRANT üretimi — ALL_TAB_PRIVS
# ---------------------------------------------------------------------------
def _get_grant_statements(conn, schema: str, target_schema: str,
                          replace_schema: bool) -> list[str]:
    """
    Source schema üzerindeki object grant'larını GRANT ifadesi olarak döndürür.
    Örn: GRANT SELECT ON TARGET_SCHEMA.TABLE_NAME TO APP_USER
    """
    sql = """
        SELECT GRANTEE, TABLE_NAME, PRIVILEGE,
               GRANTABLE, HIERARCHY
          FROM ALL_TAB_PRIVS
         WHERE TABLE_SCHEMA = :schema
           AND GRANTEE NOT IN (
               'SYS','SYSTEM','PUBLIC','WMSYS','XDB',
               'DBSNMP','APPQOSSYS','ORACLE_OCM'
           )
         ORDER BY TABLE_NAME, PRIVILEGE, GRANTEE
    """
    rows = fetch_all(conn, sql, {"schema": schema})
    stmts = []
    eff_schema = target_schema if replace_schema else schema
    for r in rows:
        with_grant = " WITH GRANT OPTION" if r.get("grantable") == "YES" else ""
        hierarchy  = " WITH HIERARCHY OPTION" if r.get("hierarchy") == "YES" else ""
        stmt = (
            f"GRANT {r['privilege']} ON {eff_schema}.{r['table_name']} "
            f"TO {r['grantee']}{with_grant}{hierarchy};"
        )
        stmts.append(stmt)
    return stmts


# ---------------------------------------------------------------------------
# DDL'i SQL*Plus uyumlu hale getir
# ---------------------------------------------------------------------------
def _normalize_ddl(ddl: str) -> str:
    """
    - Baştaki/sondaki boşlukları temizle
    - / ile bittiğinden emin ol (PL/SQL objeleri için)
    - Noktalı virgülden sonra / ekle
    """
    ddl = ddl.strip()
    # Oracle bazen sonuna noktalı virgül koyar, bazen koymaz
    if ddl.endswith(";"):
        ddl = ddl[:-1].rstrip()
    if not ddl.endswith("/"):
        ddl += "\n/"
    return ddl


# ---------------------------------------------------------------------------
# Ana fonksiyon
# ---------------------------------------------------------------------------
def generate_scripts(
    source_conn,
    missing_objects: dict[str, list[str]],   # {obj_type: [obj_name, ...]}
    source_schema: str,
    target_schema: str,
    cfg,                                      # GenerateScriptsConfig
    console=None,                             # rich Console (opsiyonel)
    not_sync_sequences: Optional[list[str]] = None,  # NOT-SYNC sequence adları → ALTER
    missing_constraints: Optional[list] = None,      # [(table, label, source_value), ...]
) -> list[str]:
    """
    Eksik objelerin DDL scriptlerini output_dir altına yazar. Ayrıca NOT-SYNC
    sequence'ler için (SEQUENCE tipi açıkken) hizalayıcı ALTER script'i üretir.
    Döndürür: oluşturulan dosyaların listesi.
    """
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_files: list[str] = []

    # Hangi tipleri işleyeceğiz? (PACKAGE seçilince BODY da otomatik)
    active_types: list[str] = []
    for t in APPLY_ORDER:
        if t == "GRANT":
            if cfg.types.get("GRANT", True):
                active_types.append("GRANT")
            continue
        if t == "CONSTRAINT":
            # CONSTRAINT ayrı yazıcı ile (imza-eşleşmeli) işlenir, döngü dışı.
            continue
        base = t.replace(" BODY", "")
        if cfg.types.get(base, False) or cfg.types.get(t, False):
            if t not in active_types:
                active_types.append(t)
        # AUTO_BODY: PACKAGE → PACKAGE BODY, TYPE → TYPE BODY
        if t in AUTO_BODY_TYPES and cfg.types.get(t, False):
            body = AUTO_BODY_TYPES[t]
            if body not in active_types:
                active_types.append(body)

    # Her tip için ayrı dosya
    for obj_type in active_types:
        if obj_type == "GRANT":
            _write_grant_file(
                source_conn, source_schema, target_schema, cfg,
                output_dir, generated_at, created_files, console,
            )
            continue

        objects = missing_objects.get(obj_type, [])
        if not objects:
            continue

        safe_type  = obj_type.replace(" ", "_")
        filename   = f"{target_schema}_{safe_type}.sql"
        filepath   = output_dir / filename

        lines = [_file_header(target_schema, obj_type, generated_at)]
        written = 0
        skipped = 0
        eff_schema = target_schema if cfg.replace_schema else source_schema

        for obj_name in sorted(objects):
            # INVALID kontrolü yalnız PL/SQL tipleri için anlamlı (ALL_OBJECTS.STATUS).
            if obj_type in PLSQL_TYPES:
                status = _get_object_status(source_conn, source_schema, obj_type, obj_name)
                if status == "INVALID" and not cfg.include_invalid:
                    if console:
                        console.print(
                            f"  [yellow]⚠  SKIP[/yellow] {obj_type} {obj_name} — INVALID "
                            f"(include_invalid=false)"
                        )
                    skipped += 1
                    continue
            else:
                status = "N/A"

            note = "INVALID — lütfen manuel kontrol edin" if status == "INVALID" else ""
            lines.append(_object_header(obj_name, status, note))

            # DDL çek — hepsi NATIVE (DBMS_METADATA YOK → 11g ORA-03113 yok).
            if obj_type == "SEQUENCE":
                ddl = _get_sequence_ddl(source_conn, obj_name, source_schema, eff_schema)
            elif obj_type == "SYNONYM":
                ddl = _get_synonym_ddl(source_conn, source_schema, obj_name, eff_schema,
                                       target_schema, cfg.replace_schema)
            elif obj_type == "INDEX":
                ddl = _get_index_ddl(source_conn, source_schema, obj_name, eff_schema)
            elif obj_type == "TRIGGER":
                ddl = _get_trigger_ddl(source_conn, source_schema, obj_name,
                                       target_schema, cfg.replace_schema)
            else:  # FUNCTION/PROCEDURE/PACKAGE[BODY]/TYPE[BODY] → ALL_SOURCE
                ddl = _get_source_ddl(source_conn, obj_type, source_schema, obj_name,
                                      target_schema, cfg.replace_schema)

            if not ddl:
                lines.append(f"-- !! DDL alınamadı: {obj_name}\n\n")
                skipped += 1
                continue

            ddl = _normalize_ddl(ddl)
            lines.append(ddl + "\n\n")
            written += 1

        if written == 0:
            # Sadece skip varsa dosya oluşturma
            continue

        content = "".join(lines)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        created_files.append(str(filepath))
        if console:
            console.print(
                f"  [green]✅[/green] {filename}  "
                f"([cyan]{written}[/cyan] obje"
                + (f", [yellow]{skipped} atlandı[/yellow]" if skipped else "")
                + ")"
            )

    # NOT-SYNC sequence remediation (ALTER) — SEQUENCE tipi açıksa
    if not_sync_sequences and (cfg.types.get("SEQUENCE", False)):
        _write_sequence_alter_file(
            source_conn, source_schema, target_schema, sorted(set(not_sync_sequences)),
            cfg, output_dir, generated_at, created_files, console,
        )

    # CONSTRAINT (PK/UK/FK/CHECK) — CONSTRAINT tipi açıksa
    if missing_constraints and cfg.types.get("CONSTRAINT", True):
        _write_constraint_file(
            source_conn, source_schema, target_schema, missing_constraints,
            cfg, output_dir, generated_at, created_files, console,
        )

    # Uygulama sırası README'si
    if created_files:
        _write_apply_order(output_dir, target_schema, created_files, generated_at)

    return created_files


# ---------------------------------------------------------------------------
# NOT-SYNC sequence → ALTER dosyası
# ---------------------------------------------------------------------------
def _write_sequence_alter_file(source_conn, source_schema, target_schema, seq_names,
                               cfg, output_dir, generated_at, created_files, console):
    """NOT-SYNC sequence'ler için hizalayıcı ALTER script'i yazar (source değerleriyle)."""
    eff_schema = target_schema if cfg.replace_schema else source_schema

    blocks = []
    written = 0
    for name in seq_names:
        seq_row = _fetch_seq_row(source_conn, source_schema, name)
        if not seq_row:
            blocks.append(f"-- !! Sequence parametreleri alinamadi: {name}\n")
            continue
        blocks.append(_object_header(name, "NOT-SYNC", "source ile hizalama (ALTER)"))
        blocks.append(_build_alter_sequence(seq_row, eff_schema, name) + "\n")
        written += 1

    if written == 0:
        return

    filename = f"{target_schema}_SEQUENCE_ALTER.sql"
    filepath = output_dir / filename
    content = _file_header(target_schema, "SEQUENCE (ALTER / NOT-SYNC)", generated_at) + "\n".join(blocks) + "\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    created_files.append(str(filepath))
    if console:
        console.print(
            f"  [green]✅[/green] {filename}  "
            f"([cyan]{written}[/cyan] NOT-SYNC sequence → ALTER)"
        )


# ---------------------------------------------------------------------------
# CONSTRAINT dosyası — eksik PK/UK/FK/CHECK için ALTER TABLE ADD CONSTRAINT
# ---------------------------------------------------------------------------
def _write_constraint_file(source_conn, source_schema, target_schema, specs,
                           cfg, output_dir, generated_at, created_files, console):
    """Eksik constraint'ler için ALTER TABLE ADD CONSTRAINT script'i yazar (PK/UK→FK sıralı)."""
    ddls = _get_constraint_ddls(
        source_conn, source_schema, target_schema, cfg.replace_schema, specs
    )
    if not ddls:
        return

    lines = [_file_header(target_schema, "CONSTRAINT", generated_at)]
    last_table = None
    for _order, table, ddl in ddls:
        if table != last_table:
            lines.append(_object_header(table, "FAILED", "eksik constraint(ler)"))
            last_table = table
        lines.append(ddl + "\n")

    filename = f"{target_schema}_CONSTRAINT.sql"
    filepath = output_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    created_files.append(str(filepath))
    if console:
        console.print(
            f"  [green]✅[/green] {filename}  "
            f"([cyan]{len(ddls)}[/cyan] constraint)"
        )


# ---------------------------------------------------------------------------
# GRANT dosyası
# ---------------------------------------------------------------------------
def _write_grant_file(source_conn, source_schema, target_schema, cfg,
                      output_dir, generated_at, created_files, console):
    stmts = _get_grant_statements(
        source_conn, source_schema, target_schema, cfg.replace_schema
    )
    if not stmts:
        return

    filename = f"{target_schema}_GRANT.sql"
    filepath  = output_dir / filename

    header = _file_header(target_schema, "GRANT", generated_at)
    content = header + "\n".join(stmts) + "\n"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    created_files.append(str(filepath))
    if console:
        console.print(
            f"  [green]✅[/green] {filename}  "
            f"([cyan]{len(stmts)}[/cyan] grant)"
        )


# ---------------------------------------------------------------------------
# Uygulama sırası dosyası
# ---------------------------------------------------------------------------
def _write_apply_order(output_dir: Path, schema: str,
                       created_files: list[str], generated_at: str):
    filepath = output_dir / "README_apply_order.txt"
    lines = [
        "=" * 60,
        f"  dataval — DDL Apply Order",
        f"  Schema  : {schema}",
        f"  Created : {generated_at}",
        "=" * 60,
        "",
        "Aşağıdaki sırayla uygulayın (bağımlılık hiyerarşisi):",
        "",
    ]
    # Sıralı dosyaları APPLY_ORDER'a göre sırala
    order_map = {t.replace(" ", "_"): i for i, t in enumerate(APPLY_ORDER)}
    def sort_key(f):
        name = Path(f).stem  # SOURCE_SCHEMA_PACKAGE_BODY
        parts = name.split("_", 1)
        type_part = parts[1] if len(parts) > 1 else ""
        return order_map.get(type_part, 99)

    sorted_files = sorted(created_files, key=sort_key)
    for i, f in enumerate(sorted_files, 1):
        lines.append(f"  {i:2}. {Path(f).name}")

    lines += [
        "",
        "SQL*Plus ile uygulama:",
        "  sqlplus user/pass@service @<dosya.sql>",
        "",
        "NOT: INVALID objeleri iceren dosyalar -- INVALID yorumuyla isaretlidir.",
        "     Bu objeleri uyguladiktan sonra derleyin:",
        "     EXEC DBMS_UTILITY.COMPILE_SCHEMA(schema => '" + schema + "');",
    ]

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
