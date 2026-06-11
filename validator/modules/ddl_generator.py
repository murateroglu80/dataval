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
from validator.result import ValidationResult, Status


# ---------------------------------------------------------------------------
# Uygulama sırası — bağımlılık hiyerarşisi
# ---------------------------------------------------------------------------
APPLY_ORDER = [
    "TYPE",
    "TYPE BODY",
    "SEQUENCE",
    "SYNONYM",
    "FUNCTION",
    "PROCEDURE",
    "PACKAGE",
    "PACKAGE BODY",
    "TRIGGER",
    "GRANT",
]

# DBMS_METADATA tip adı eşleştirmesi
METADATA_TYPE_MAP = {
    "TYPE":         "TYPE",
    "TYPE BODY":    "TYPE_BODY",
    "SEQUENCE":     "SEQUENCE",
    "SYNONYM":      "SYNONYM",
    "FUNCTION":     "FUNCTION",
    "PROCEDURE":    "PROCEDURE",
    "PACKAGE":      "PACKAGE",
    "PACKAGE BODY": "PACKAGE_BODY",
    "TRIGGER":      "TRIGGER",
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
# DBMS_METADATA.GET_DDL
# ---------------------------------------------------------------------------
def _get_ddl_raw(conn, meta_type: str, obj_name: str, schema: str) -> Optional[str]:
    """DBMS_METADATA.GET_DDL ile ham DDL'i çeker."""
    sql = """
        SELECT DBMS_METADATA.GET_DDL(:obj_type, :obj_name, :schema) AS ddl
          FROM DUAL
    """
    try:
        row = fetch_one(conn, sql, {"obj_type": meta_type, "obj_name": obj_name, "schema": schema})
        if row and row.get("ddl"):
            return str(row["ddl"])
    except Exception as e:
        err = str(e)
        # ORA-31603: obje bulunamadı — sessizce None dön
        if "ORA-31603" in err or "ORA-04043" in err:
            return None
        # Ölümcül kopma (11g DBMS_METADATA-in-SQL hatası vb.): ham traceback yerine
        # None dön ki tek bir obje tüm CLI'yi çökertmesin. SEQUENCE artık bu yolu
        # KULLANMAZ (native üretilir); bu koruma diğer tipler içindir.
        if any(code in err for code in ("ORA-03113", "ORA-03114", "DPY-4011", "DPI-1080")):
            return None
        raise
    return None


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


def _get_sequence_ddl(conn, obj_name: str, schema: str) -> Optional[str]:
    """
    SEQUENCE için `CREATE SEQUENCE` DDL'ini ALL_SEQUENCES'tan native kurar
    (DBMS_METADATA yok → ORA-03113 yok). LAST_NUMBER `START WITH` olarak yazılır.
    """
    seq_row = _fetch_seq_row(conn, schema, obj_name)
    if not seq_row:
        return None
    return _build_create_sequence(seq_row, schema, obj_name)


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
# DDL içindeki schema adını replace et
# ---------------------------------------------------------------------------
def _replace_schema(ddl: str, source_schema: str, target_schema: str) -> str:
    if source_schema.upper() == target_schema.upper():
        return ddl
    # Büyük-küçük harf duyarsız, word boundary ile
    pattern = re.compile(re.escape(source_schema), re.IGNORECASE)
    return pattern.sub(target_schema, ddl)


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

        for obj_name in sorted(objects):
            status = _get_object_status(source_conn, source_schema, obj_type, obj_name)

            if status == "INVALID" and not cfg.include_invalid:
                if console:
                    console.print(
                        f"  [yellow]⚠  SKIP[/yellow] {obj_type} {obj_name} — INVALID "
                        f"(include_invalid=false)"
                    )
                skipped += 1
                continue

            note = "INVALID — lütfen manuel kontrol edin" if status == "INVALID" else ""
            lines.append(_object_header(obj_name, status, note))

            # DDL çek
            if obj_type == "SEQUENCE":
                ddl = _get_sequence_ddl(source_conn, obj_name, source_schema)
            else:
                meta_type = METADATA_TYPE_MAP.get(obj_type, obj_type)
                ddl = _get_ddl_raw(source_conn, meta_type, obj_name, source_schema)

            if not ddl:
                lines.append(f"-- !! DDL alınamadı: {obj_name}\n\n")
                skipped += 1
                continue

            if cfg.replace_schema:
                ddl = _replace_schema(ddl, source_schema, target_schema)

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
