"""
Code objects modülü — DDL metin karşılaştırması.
DBMS_METADATA.GET_DDL kullanır; whitespace normalize edilerek hash karşılaştırması yapılır.
"""

import re
import hashlib
import oracledb
from validator.connection import fetch_all, fetch_one
from validator.result import ValidationResult, ModuleSummary, Status
from validator.config_loader import AppConfig, SchemaMapping
from validator.debug import dbg

SQL_OBJECTS = """
SELECT object_name, object_type, status
FROM   all_objects
WHERE  owner       = :schema
  AND  object_type IN ({placeholders})
  AND  object_name NOT LIKE 'BIN$%'
ORDER BY object_type, object_name
"""

SQL_DDL = """
SELECT DBMS_METADATA.GET_DDL(:obj_type, :obj_name, :schema) AS ddl
FROM   dual
"""


def _normalize(ddl: str, normalize: bool) -> str:
    """DDL metnini karşılaştırma için normalleştirir."""
    if not ddl:
        return ""
    # Schema adını çıkar (source/target schema adları farklı olabilir)
    text = ddl.upper()
    if normalize:
        text = re.sub(r'\s+', ' ', text).strip()
    # Storage clause'ları kaldır (STORAGE (...) bloğu)
    text = re.sub(r'STORAGE\s*\([^)]*\)', '', text)
    # Tablespace bilgisini kaldır
    text = re.sub(r'TABLESPACE\s+\w+', '', text)
    # SEGMENT CREATION kaldır
    text = re.sub(r'SEGMENT\s+CREATION\s+\w+', '', text)
    # LOB storage kaldır — BASICFILE / SECUREFILE
    text = re.sub(r'(BASICFILE|SECUREFILE)', '', text)
    return text.strip()


def _get_ddl(conn: oracledb.Connection, obj_type: str, obj_name: str, schema: str) -> str | None:
    """DBMS_METADATA üzerinden DDL çeker."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DBMS_METADATA.GET_DDL(:t, :n, :s) FROM dual",
            {"t": obj_type, "n": obj_name, "s": schema}
        )
        row = cursor.fetchone()
        if row and row[0]:
            # LOB nesnesi olabilir
            ddl = row[0]
            if hasattr(ddl, 'read'):
                ddl = ddl.read()
            return str(ddl)
        return None
    except oracledb.DatabaseError:
        return None


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="code_objects")
    mc = cfg.modules

    if not mc.code_objects_enabled:
        return summary

    types = mc.code_object_types
    placeholders = ", ".join(f"'{t}'" for t in types)
    sql = SQL_OBJECTS.format(placeholders=placeholders)

    src_objs = {(r["object_type"], r["object_name"]): r
                for r in fetch_all(src_conn, sql, {"schema": mapping.source})}
    tgt_objs = {(r["object_type"], r["object_name"]): r
                for r in fetch_all(tgt_conn, sql, {"schema": mapping.target})}

    src_keys = set(src_objs)
    tgt_keys = set(tgt_objs)

    # Eksik / fazla objeler
    for key in sorted(src_keys - tgt_keys):
        obj_type, obj_name = key
        summary.add(ValidationResult(
            module="code_objects", schema=mapping.source,
            object_type=obj_type, object_name=obj_name,
            status=Status.FAIL,
            note="Target'ta mevcut değil",
        ))

    for key in sorted(tgt_keys - src_keys):
        obj_type, obj_name = key
        summary.add(ValidationResult(
            module="code_objects", schema=mapping.source,
            object_type=obj_type, object_name=obj_name,
            status=Status.WARNING,
            note="Target'ta fazladan mevcut",
        ))

    # Ortak objeler — DDL karşılaştırması
    for key in sorted(src_keys & tgt_keys):
        obj_type, obj_name = key
        src_row = src_objs[key]
        tgt_row = tgt_objs[key]

        # INVALID status kontrolü
        notes = []
        if src_row["status"] == "INVALID":
            notes.append("Source INVALID")
        if tgt_row["status"] == "INVALID":
            notes.append("Target INVALID")

        # DDL çek ve karşılaştır
        dbg("code", f"{mapping.source}.{obj_name} ({obj_type}) DDL çekiliyor")
        src_ddl = _get_ddl(src_conn, obj_type, obj_name, mapping.source)
        tgt_ddl = _get_ddl(tgt_conn, obj_type, obj_name, mapping.target)

        if src_ddl is None and tgt_ddl is None:
            summary.add(ValidationResult(
                module="code_objects", schema=mapping.source,
                object_type=obj_type, object_name=obj_name,
                status=Status.WARNING,
                note="DDL alınamadı (yetki?)",
            ))
            continue

        src_norm = _normalize(src_ddl or "", mc.normalize_whitespace)
        tgt_norm = _normalize(tgt_ddl or "", mc.normalize_whitespace)

        # Schema adını placeholder ile değiştir (source/target farklı olabilir)
        src_norm = src_norm.replace(mapping.source.upper(), "__SCHEMA__")
        tgt_norm = tgt_norm.replace(mapping.target.upper(), "__SCHEMA__")

        if _hash(src_norm) == _hash(tgt_norm):
            status = Status.WARNING if notes else Status.PASS
            summary.add(ValidationResult(
                module="code_objects", schema=mapping.source,
                object_type=obj_type, object_name=obj_name,
                status=status,
                source_value=_hash(src_norm),
                target_value=_hash(tgt_norm),
                note="; ".join(notes) if notes else None,
            ))
        else:
            notes.append("DDL içeriği farklı")
            summary.add(ValidationResult(
                module="code_objects", schema=mapping.source,
                object_type=obj_type, object_name=obj_name,
                status=Status.FAIL,
                source_value=_hash(src_norm),
                target_value=_hash(tgt_norm),
                note="; ".join(notes),
            ))

    return summary
