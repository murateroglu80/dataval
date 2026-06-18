"""
Grants modülü — object privilege (yetki) karşılaştırması (11g → 19c).

Source ve target şemalardaki object grant'larını karşılaştırır:
  - source'ta var, target'ta yok           → FAILED  (eksik yetki)
  - target'ta var, source'ta yok            → extra   (extra_as'a göre NOT-SYNC/SYNC)
  - iki tarafta da var ama GRANTABLE/HIERARCHY farklı → NOT-SYNC
  - birebir aynı                            → SYNC

Görünürlük: `ALL_TAB_PRIVS` yalnız bağlı kullanıcının grantor/grantee/owner olduğu
satırları döndürür → başka kullanıcıların verdiği grant'lar kaçabilir. Bu yüzden
önce `DBA_TAB_PRIVS` denenir (DBA / SELECT_CATALOG_ROLE), erişim yoksa `ALL_TAB_PRIVS`'e
düşülür. Her iki sorgu da yalnız SELECT'tir — source asla yazılmaz.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status, extra_status
from validator.config_loader import AppConfig, SchemaMapping

# Sistem/altyapı grantee'leri gürültü yaratmasın diye dışlanır (generation ile aynı liste).
_EXCLUDED_GRANTEES = (
    "'SYS','SYSTEM','PUBLIC','WMSYS','XDB','DBSNMP','APPQOSSYS','ORACLE_OCM'"
)

# Kolon adları normalize edilir (DBA: owner / ALL: table_schema → tek şema parametresi).
SQL_GRANTS_DBA = f"""
SELECT grantee, table_name, privilege, grantable, hierarchy
  FROM dba_tab_privs
 WHERE owner = :schema
   AND grantee NOT IN ({_EXCLUDED_GRANTEES})
"""

SQL_GRANTS_ALL = f"""
SELECT grantee, table_name, privilege, grantable, hierarchy
  FROM all_tab_privs
 WHERE table_schema = :schema
   AND grantee NOT IN ({_EXCLUDED_GRANTEES})
"""


def fetch_object_grants(conn, schema: str) -> list[dict]:
    """
    Şemadaki object grant'larını döndürür. Önce DBA_TAB_PRIVS (tam görünürlük),
    erişim yoksa (ORA-00942 / ORA-01031) ALL_TAB_PRIVS'e düşer. Yalnız SELECT.
    Generation (ddl_generator) ile paylaşılan tek fetch katmanı.
    """
    try:
        return fetch_all(conn, SQL_GRANTS_DBA, {"schema": schema})
    except Exception as e:
        err = str(e)
        if "ORA-00942" in err or "ORA-01031" in err:
            return fetch_all(conn, SQL_GRANTS_ALL, {"schema": schema})
        raise


def missing_grant_rows(
    src_conn, tgt_conn, src_schema: str, tgt_schema: str
) -> list[dict]:
    """
    Yalnız **eksik** grant'ların kaynak satırlarını döndürür: source'ta olup
    target'ta olmayan (anahtar = grantee+obje+privilege). Generation bu listeyi
    kullanır → SYNC/extra grant'lar asla script'e girmez (FAILED-only sözleşmesi).
    Her iki taraf da yalnız SELECT ile okunur; source asla yazılmaz.
    """
    src_rows = fetch_object_grants(src_conn, src_schema)
    tgt_keys = {_key(r) for r in fetch_object_grants(tgt_conn, tgt_schema)}
    return [r for r in src_rows if _key(r) not in tgt_keys]


def _key(r: dict) -> tuple:
    """Bir grant'ı benzersiz kılan anahtar: (grantee, obje, privilege)."""
    return (r["grantee"], r["table_name"], r["privilege"])


def _attrs(r: dict) -> tuple:
    """Karşılaştırılan öznitelikler: (grantable, hierarchy) — boş → 'NO'."""
    return ((r.get("grantable") or "NO").upper(), (r.get("hierarchy") or "NO").upper())


def _fmt(r: dict) -> str:
    """source/target_value için kısa gösterim: PRIVILEGE [+GRANT] [+HIER]."""
    s = r["privilege"]
    g, h = _attrs(r)
    if g == "YES":
        s += " +GRANT"
    if h == "YES":
        s += " +HIER"
    return s


def _obj_name(key: tuple) -> str:
    grantee, table, priv = key
    return f"{table}.{priv} → {grantee}"


def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    mapping: SchemaMapping,
    cfg: AppConfig,
) -> ModuleSummary:

    summary = ModuleSummary(module="grants")
    extra = extra_status(cfg.output.extra_as)

    src = {_key(r): r for r in fetch_object_grants(src_conn, mapping.source)}
    tgt = {_key(r): r for r in fetch_object_grants(tgt_conn, mapping.target)}

    # Eksik (source'ta var, target'ta yok) → FAILED
    for k in sorted(src.keys() - tgt.keys()):
        summary.add(ValidationResult(
            module="grants", schema=mapping.source,
            object_type="GRANT", object_name=_obj_name(k),
            status=Status.FAILED,
            source_value=_fmt(src[k]), target_value="(yok)",
            note="Target'ta yetki eksik",
        ))

    # Fazla (target'ta var, source'ta yok) → extra
    for k in sorted(tgt.keys() - src.keys()):
        summary.add(ValidationResult(
            module="grants", schema=mapping.source,
            object_type="GRANT", object_name=_obj_name(k),
            status=extra,
            source_value="(yok)", target_value=_fmt(tgt[k]),
            note="Target'ta fazladan yetki",
        ))

    # Ortak → öznitelik farkı NOT-SYNC, aynısı SYNC
    for k in sorted(src.keys() & tgt.keys()):
        sa, ta = _attrs(src[k]), _attrs(tgt[k])
        if sa != ta:
            diffs = []
            if sa[0] != ta[0]:
                diffs.append(("grantable", sa[0], ta[0]))
            if sa[1] != ta[1]:
                diffs.append(("hierarchy", sa[1], ta[1]))
            summary.add(ValidationResult(
                module="grants", schema=mapping.source,
                object_type="GRANT", object_name=_obj_name(k),
                status=Status.NOT_SYNC,
                source_value=_fmt(src[k]), target_value=_fmt(tgt[k]),
                diffs=diffs,
            ))
        else:
            summary.add(ValidationResult(
                module="grants", schema=mapping.source,
                object_type="GRANT", object_name=_obj_name(k),
                status=Status.SYNC,
                source_value=_fmt(src[k]), target_value=_fmt(tgt[k]),
            ))

    return summary
