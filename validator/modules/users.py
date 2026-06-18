"""
Users modülü — instance-wide kullanıcı + yetki (privilege) izolasyonu (11g → 19c).

Schema-scoped `grants.py`'den **bağımsızdır**: bu modül object-grant değil, **veritabanı
kullanıcılarını** ve onlara verilen **sistem yetkisi / rol / object grant**'larını karşılaştırır.
Yalnız **uygulama** kullanıcıları kapsanır — sistem/altyapı user'ları (SYS, SYSTEM, XDB,
APEX_*, SPATIAL_* …) dinamik olarak elenir (`_non_system_users`).

Statüler:
  - source'ta user var, target'ta yok                         → FAILED (eksik user)
  - target'ta var, source'ta yok                              → extra
  - iki tarafta da var ama account_status/tablespace/profile  → NOT-SYNC
  - source yetkisi (sys-priv/rol/obj-grant) target'ta yok     → FAILED
  - admin_option / default_role farkı                         → NOT-SYNC
  - birebir aynı                                              → SYNC

Tüm sorgular yalnız SELECT'tir — source asla yazılmaz.

Parola hash'i (verifier) **default'ta okunmaz**. Yalnız `modules.password_sync=true` iken
ortak user'lar için verifier okunup karşılaştırılır (farklı → NOT-SYNC) ve generation
sırasında `IDENTIFIED BY VALUES` üretmek için kullanılır. Hash değeri HİÇBİR zaman
loglanmaz/print edilmez — raporda yalnız maskelenir (`_mask_hash`), yalnız üretilen
`.sql` dosyasına yazılır.
"""

import oracledb
from validator.connection import fetch_all
from validator.result import ValidationResult, ModuleSummary, Status, extra_status
from validator.config_loader import AppConfig

# 11g + 19c default sistem/altyapı kullanıcıları (oracle_maintained yoksa statik fallback).
_SYSTEM_USERS = {
    "SYS", "SYSTEM", "OUTLN", "XDB", "DBSNMP", "WMSYS", "CTXSYS", "MDSYS",
    "ORDSYS", "ORDPLUGINS", "ORDDATA", "EXFSYS", "APPQOSSYS", "ANONYMOUS",
    "SI_INFORMTN_SCHEMA", "OLAPSYS", "FLOWS_FILES", "OWBSYS", "OWBSYS_AUDIT",
    "GSMADMIN_INTERNAL", "GSMCATUSER", "GSMUSER", "AUDSYS", "LBACSYS", "DVSYS",
    "DVF", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "GGSYS", "DBSFWUSER",
    "REMOTE_SCHEDULER_AGENT", "SYS$UMF", "APEX_PUBLIC_USER", "MGMT_VIEW",
    "SPATIAL_CSW_ADMIN_USR", "SPATIAL_WFS_ADMIN_USR", "MDDATA", "DIP", "TSMSYS",
    "ORACLE_OCM", "XS$NULL", "PUBLIC", "AURORA$ORB$UNAUTHENTICATED",
}

# Sürüm/komponente göre ad ekli (numaralı) sistem şemaları — prefix ile elenir.
_SYSTEM_PREFIXES = ("APEX_", "FLOWS_", "SPATIAL_", "C##", "XDB$", "SYS$")


def _is_system_user(username: str) -> bool:
    u = (username or "").upper()
    if u in _SYSTEM_USERS:
        return True
    return any(u.startswith(p) for p in _SYSTEM_PREFIXES)


def _user_source(conn) -> str:
    """
    Hangi katalog görünümü + filtreleme stratejisi kullanılacağını saptar:
      'maintained' → DBA_USERS.oracle_maintained='N' (12c+/19c, en doğru)
      'dba'        → DBA_USERS + statik filtre (11g, kolon yok)
      'all'        → ALL_USERS + statik filtre (DBA_USERS erişimi yok)
    Yalnız SELECT probe.
    """
    try:
        fetch_all(conn, "SELECT oracle_maintained FROM dba_users WHERE ROWNUM = 1")
        return "maintained"
    except Exception as e:
        err = str(e)
        if "ORA-00904" in err:          # kolon yok → 11g
            return "dba"
        if "ORA-00942" in err or "ORA-01031" in err:  # DBA_USERS erişimi yok
            return "all"
        raise


def _non_system_users(conn) -> set[str]:
    """Yalnız uygulama (non-system) kullanıcı adlarını döndürür. Paylaşılan helper."""
    mode = _user_source(conn)
    if mode == "maintained":
        rows = fetch_all(
            conn, "SELECT username FROM dba_users WHERE oracle_maintained = 'N'"
        )
        return {r["username"] for r in rows if not _is_system_user(r["username"])}
    view = "dba_users" if mode == "dba" else "all_users"
    rows = fetch_all(conn, f"SELECT username FROM {view}")
    return {r["username"] for r in rows if not _is_system_user(r["username"])}


# ---------------------------------------------------------------------------
# Parola verifier okuma (cross-version) — yalnız password_sync açıkken kullanılır.
# Hash ASLA loglanmaz/print edilmez; yalnız diff için maskelenir, yalnız generation
# dosyasına yazılmak üzere döner.
# ---------------------------------------------------------------------------
def _verifier_source(conn) -> str:
    """
    Verifier'ın nereden okunabileceğini saptar (SELECT probe):
      'user$' → SYS.USER$ erişimi var (SYS/sysdba/SELECT ON SYS.USER$).
                11g: password=10g DES, spare4=11g SHA-1; 12c+: spare4='S:…;T:…'.
      'dba'   → SYS.USER$ yok ama DBA_USERS.PASSWORD okunur (yalnız 11g; 12c+ NULL).
      'none'  → ikisi de yok → placeholder yolu.
    """
    try:
        fetch_all(conn, "SELECT password, spare4 FROM sys.user$ WHERE ROWNUM = 1")
        return "user$"
    except Exception as e:
        err = str(e)
        if "ORA-00942" in err or "ORA-01031" in err:
            try:
                fetch_all(conn, "SELECT password FROM dba_users WHERE ROWNUM = 1")
                return "dba"
            except Exception:
                return "none"
        raise


def fetch_user_verifier(conn, username: str) -> dict | None:
    """{'password': <DES|None>, 'spare4': <SHA verifier|None>} döndürür (sürüm-duyarlı).
    Bulunamazsa None. Dönüş ASLA loglanmaz/print edilmez."""
    src = _verifier_source(conn)
    if src == "user$":
        rows = fetch_all(
            conn, "SELECT password, spare4 FROM sys.user$ WHERE name = :u",
            {"u": username},
        )
        if rows:
            return {"password": rows[0].get("password"), "spare4": rows[0].get("spare4")}
        return None
    if src == "dba":
        rows = fetch_all(
            conn, "SELECT password FROM dba_users WHERE username = :u",
            {"u": username},
        )
        if rows:
            return {"password": rows[0].get("password"), "spare4": None}
        return None
    return None


def verifier_value(verifier: dict | None) -> str | None:
    """IDENTIFIED BY VALUES için kullanılacak verifier string'i: spare4 (varsa) → password."""
    if not verifier:
        return None
    return verifier.get("spare4") or verifier.get("password") or None


def _mask_hash(h) -> str:
    """Hash'i rapora/loga güvenli biçimde maskeler — gerçek değer asla yüzeye çıkmaz."""
    if not h:
        return "(yok)"
    s = str(h)
    return (s[:6] + "…") if len(s) > 6 else "******"


def fetch_user_attrs_one(conn, username: str) -> dict:
    """Tek bir user'ın DBA_USERS özniteliklerini döndürür (generation için). Yoksa {}."""
    return _fetch_user_attrs(conn, {username}).get(username, {})


# ---------------------------------------------------------------------------
# Katalog fetch'leri (hepsi non-system grantee/username ile süzülür)
# ---------------------------------------------------------------------------
def _fetch_user_attrs(conn, app_users: set[str]) -> dict[str, dict]:
    """{username: {account_status, default_tablespace, temporary_tablespace, profile}}.
    DBA_USERS yoksa ALL_USERS'a düşer (öznitelikler None → existence-only diff)."""
    try:
        rows = fetch_all(
            conn,
            "SELECT username, account_status, default_tablespace, "
            "       temporary_tablespace, profile FROM dba_users",
        )
    except Exception as e:
        if "ORA-00942" in str(e) or "ORA-01031" in str(e):
            rows = fetch_all(conn, "SELECT username FROM all_users")
        else:
            raise
    out = {}
    for r in rows:
        u = r["username"]
        if u in app_users:
            out[u] = r
    return out


def _fetch_sys_privs(conn, app_users: set[str]) -> dict[tuple, dict]:
    """DBA_SYS_PRIVS → {(grantee, privilege): {admin_option}}."""
    rows = fetch_all(
        conn, "SELECT grantee, privilege, admin_option FROM dba_sys_privs"
    )
    return {
        (r["grantee"], r["privilege"]): r
        for r in rows if r["grantee"] in app_users
    }


def _fetch_role_privs(conn, app_users: set[str]) -> dict[tuple, dict]:
    """DBA_ROLE_PRIVS → {(grantee, granted_role): {admin_option, default_role}}."""
    rows = fetch_all(
        conn,
        "SELECT grantee, granted_role, admin_option, default_role FROM dba_role_privs",
    )
    return {
        (r["grantee"], r["granted_role"]): r
        for r in rows if r["grantee"] in app_users
    }


def _fetch_obj_grants(conn, app_users: set[str]) -> dict[tuple, dict]:
    """Grantee-merkezli object grant (instance-wide):
    {(grantee, owner, table_name, privilege): {grantable}}. DBA→ALL fallback."""
    sql_dba = ("SELECT grantee, owner, table_name, privilege, grantable "
               "FROM dba_tab_privs")
    sql_all = ("SELECT grantee, table_schema AS owner, table_name, privilege, grantable "
               "FROM all_tab_privs")
    try:
        rows = fetch_all(conn, sql_dba)
    except Exception as e:
        if "ORA-00942" in str(e) or "ORA-01031" in str(e):
            rows = fetch_all(conn, sql_all)
        else:
            raise
    return {
        (r["grantee"], r["owner"], r["table_name"], r["privilege"]): r
        for r in rows if r["grantee"] in app_users
    }


# ---------------------------------------------------------------------------
# Diff yardımcıları
# ---------------------------------------------------------------------------
def _yn(v) -> str:
    return (v or "NO").upper()


def _diff_membership(summary, schema, obj_type, src, tgt, extra,
                     name_fn, attr_diffs_fn=None):
    """Generik küme diff'i: src∖tgt FAILED, tgt∖src extra, ortak → attr farkı NOT-SYNC."""
    for k in sorted(src.keys() - tgt.keys()):
        summary.add(ValidationResult(
            module="users", schema=schema, object_type=obj_type,
            object_name=name_fn(k), status=Status.FAILED,
            source_value="var", target_value="(yok)",
            note="Target'ta eksik",
        ))
    for k in sorted(tgt.keys() - src.keys()):
        summary.add(ValidationResult(
            module="users", schema=schema, object_type=obj_type,
            object_name=name_fn(k), status=extra,
            source_value="(yok)", target_value="var",
            note="Target'ta fazladan",
        ))
    for k in sorted(src.keys() & tgt.keys()):
        diffs = attr_diffs_fn(src[k], tgt[k]) if attr_diffs_fn else []
        if diffs:
            summary.add(ValidationResult(
                module="users", schema=schema, object_type=obj_type,
                object_name=name_fn(k), status=Status.NOT_SYNC,
                diffs=diffs,
            ))
        else:
            summary.add(ValidationResult(
                module="users", schema=schema, object_type=obj_type,
                object_name=name_fn(k), status=Status.SYNC,
            ))


def _user_attr_diffs(s: dict, t: dict) -> list:
    diffs = []
    for col, label in (("account_status", "account_status"),
                       ("default_tablespace", "default_tablespace"),
                       ("profile", "profile")):
        sv, tv = s.get(col), t.get(col)
        if sv is None and tv is None:
            continue  # ALL_USERS fallback — öznitelik bilinmiyor
        if (sv or "") != (tv or ""):
            diffs.append((label, sv or "(yok)", tv or "(yok)"))
    return diffs


def _db_label(conn) -> str:
    """Instance/DB etiketi (schema-bağımsız sonuçlar için). Erişilemezse 'INSTANCE'."""
    try:
        rows = fetch_all(conn, "SELECT name FROM v$database")
        if rows and rows[0].get("name"):
            return rows[0]["name"]
    except Exception:
        pass
    return "INSTANCE"


def run(
    src_conn: oracledb.Connection,
    tgt_conn: oracledb.Connection,
    cfg: AppConfig,
) -> ModuleSummary:
    """Instance-wide (şema-bağımsız) kullanıcı + yetki karşılaştırması.
    `cfg.schemas`'a hiç bakmaz; DB seviyesinde bir kez koşar."""

    summary = ModuleSummary(module="users")
    extra = extra_status(cfg.output.extra_as)
    # Sonuçların schema etiketi: target DB adı (schema-bağımsız global akış).
    schema = _db_label(tgt_conn)

    # Sistem-user filtresi her iki taraf için ayrı (sürüm farkı olabilir);
    # karşılaştırma her iki tarafın non-system birleşimi üzerinden yürür.
    src_users = _non_system_users(src_conn)
    tgt_users = _non_system_users(tgt_conn)
    all_app = src_users | tgt_users

    # 1) User existence + öznitelik
    src_attr = _fetch_user_attrs(src_conn, all_app)
    tgt_attr = _fetch_user_attrs(tgt_conn, all_app)
    # existence kümeleri: gerçekten var olan user'lar (attr fetch'inde görünenler)
    s_exist = {u: src_attr.get(u, {"username": u}) for u in src_users}
    t_exist = {u: tgt_attr.get(u, {"username": u}) for u in tgt_users}
    _diff_membership(
        summary, schema, "USER", s_exist, t_exist, extra,
        name_fn=lambda u: u, attr_diffs_fn=_user_attr_diffs,
    )

    # Yalnız ortak (iki tarafta da var olan) user'lar için yetki diff'i
    common = src_users & tgt_users

    def _only_common(d: dict) -> dict:
        return {k: v for k, v in d.items() if k[0] in common}

    # 2) Sistem yetkileri (DBA_SYS_PRIVS)
    _diff_membership(
        summary, schema, "SYS_PRIV",
        _only_common(_fetch_sys_privs(src_conn, common)),
        _only_common(_fetch_sys_privs(tgt_conn, common)),
        extra,
        name_fn=lambda k: f"{k[1]} → {k[0]}",
        attr_diffs_fn=lambda s, t: (
            [("admin_option", _yn(s.get("admin_option")), _yn(t.get("admin_option")))]
            if _yn(s.get("admin_option")) != _yn(t.get("admin_option")) else []
        ),
    )

    # 3) Rol grant'ları (DBA_ROLE_PRIVS)
    def _role_diffs(s, t):
        d = []
        if _yn(s.get("admin_option")) != _yn(t.get("admin_option")):
            d.append(("admin_option", _yn(s.get("admin_option")), _yn(t.get("admin_option"))))
        if _yn(s.get("default_role")) != _yn(t.get("default_role")):
            d.append(("default_role", _yn(s.get("default_role")), _yn(t.get("default_role"))))
        return d

    _diff_membership(
        summary, schema, "ROLE",
        _only_common(_fetch_role_privs(src_conn, common)),
        _only_common(_fetch_role_privs(tgt_conn, common)),
        extra,
        name_fn=lambda k: f"{k[1]} → {k[0]}",
        attr_diffs_fn=_role_diffs,
    )

    # 4) Object grant'lar (grantee-merkezli, instance-wide)
    _diff_membership(
        summary, schema, "OBJ_PRIV",
        _only_common(_fetch_obj_grants(src_conn, common)),
        _only_common(_fetch_obj_grants(tgt_conn, common)),
        extra,
        name_fn=lambda k: f"{k[3]} ON {k[1]}.{k[2]} → {k[0]}",
        attr_diffs_fn=lambda s, t: (
            [("grantable", _yn(s.get("grantable")), _yn(t.get("grantable")))]
            if _yn(s.get("grantable")) != _yn(t.get("grantable")) else []
        ),
    )

    # 5) Parola verifier farkı (yalnız password_sync açıkken — hassas yüzey opt-in).
    #    Ortak user'larda src/tgt verifier okunur; FARKLIYSA NOT-SYNC (maskeli diff).
    #    Eşleşiyorsa kayıt eklenmez (mevcut USER existence SYNC'i ile tekrarı önler).
    #    Hash değeri buraya/loga ASLA yazılmaz — yalnız _mask_hash gösterimi.
    if getattr(cfg.modules, "password_sync", False):
        for u in sorted(common):
            sv = verifier_value(fetch_user_verifier(src_conn, u))
            tv = verifier_value(fetch_user_verifier(tgt_conn, u))
            if (sv or "") != (tv or ""):
                summary.add(ValidationResult(
                    module="users", schema=schema, object_type="USER",
                    object_name=u, status=Status.NOT_SYNC,
                    diffs=[("password", _mask_hash(sv), _mask_hash(tv))],
                    note="Parola verifier farkı (password_sync)",
                ))

    return summary
