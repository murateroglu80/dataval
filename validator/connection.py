"""
Oracle baglanti yonetimi -- python-oracledb thin/thick mode.
Thin mode: Oracle Instant Client gerekmez (Oracle 12.1+).
Thick mode: Oracle Instant Client gerekir (Oracle 11g dahil tum versiyonlar).
SYSDBA baglantisi desteklenir.
"""

import oracledb
from contextlib import contextmanager
from typing import Optional
from validator.config_loader import ConnectionConfig

_thick_mode_initialized = False


def init_thick_mode(lib_dir=None):
    """
    Thick mode'u etkinlestirir -- tum baglantilar icin gecerlidir.
    Oracle 11g gibi eski versiyonlar icin gereklidir.

    lib_dir: Oracle Instant Client dizini.
             None ise sistem PATH'inden bulunmaya calisilir.

    Ornek:
      Windows: C:/oracle/instantclient_21_9
      Linux:   /opt/oracle/instantclient_21_9
    """
    global _thick_mode_initialized
    if _thick_mode_initialized:
        return
    if lib_dir:
        oracledb.init_oracle_client(lib_dir=lib_dir)
    else:
        oracledb.init_oracle_client()
    _thick_mode_initialized = True


def assert_writable(conn_cfg, operation: str):
    """
    Read-only işaretli bir bağlantıya yazma denemesini sert şekilde engeller.
    Source (production) koruması için savunma katmanıdır — herhangi bir kod yolu
    yanlışlıkla source'a yazmaya kalkarsa burada PermissionError fırlatılır.
    """
    if getattr(conn_cfg, "read_only", False):
        raise PermissionError(
            f"Read-only baglantida yazma engellendi: {operation} "
            f"({conn_cfg.dsn}). Source korumasi aktif — degistirmek icin "
            f"connections.yaml'da ilgili baglanti altina read_only: false yazin."
        )


def build_connection(cfg):
    """
    Verilen config'e gore Oracle baglantisi olusturur.
    SYSDBA modu desteklenir.
    """
    kwargs = dict(
        host=cfg.host,
        port=cfg.port,
        service_name=cfg.service,
        user=cfg.username,
        password=cfg.password,
    )

    if cfg.sysdba:
        kwargs["mode"] = oracledb.AUTH_MODE_SYSDBA

    if cfg.wallet_location:
        kwargs["wallet_location"] = cfg.wallet_location

    return oracledb.connect(**kwargs)


@contextmanager
def get_connection(cfg, timeout_ms=None):
    """
    Context manager -- baglantiyi acar, isi bitince kapatir.

    timeout_ms: Her sorgu icin maksimum sure (ms).
                Asilirsa ORA-03136 firlatilir.
    """
    conn = build_connection(cfg)
    try:
        if timeout_ms is not None:
            conn.callTimeout = timeout_ms
        yield conn
    finally:
        conn.close()


def test_connection(cfg):
    """
    Baglantiyi test eder.
    Donus: (basarili_mi, mesaj)
    """
    try:
        with get_connection(cfg) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT version FROM v$instance"
                if cfg.sysdba
                else "SELECT banner FROM v$version WHERE banner LIKE 'Oracle%'"
            )
            row = cursor.fetchone()
            version_info = row[0].strip() if row else "bilinmiyor"
        return True, version_info
    except oracledb.DatabaseError as e:
        (error,) = e.args
        return False, f"ORA-{error.code}: {error.message.strip()}"
    except Exception as e:
        return False, str(e)


def fetch_all(conn, sql, params=None):
    """
    SQL calistirir, sonuclari dict listesi olarak doner.
    params: named bind variables -- {'schema': 'HR', ...}
    """
    cursor = conn.cursor()
    cursor.execute(sql, params or {})
    cols = [col[0].lower() for col in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def fetch_one(conn, sql, params=None):
    """Tek satir doner, sonuc yoksa None."""
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None
