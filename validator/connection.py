"""
Oracle bağlantı yönetimi — python-oracledb thin/thick mode.
Thin mode: Oracle Instant Client gerekmez (Oracle 12.1+).
Thick mode: Oracle Instant Client gerekir (Oracle 11g dahil tüm versiyonlar).
SYSDBA bağlantısı desteklenir.
"""

import oracledb
from contextlib import contextmanager
from typing import Optional
from validator.config_loader import ConnectionConfig

_thick_mode_initialized = False


def init_thick_mode(lib_dir: Optional[str] = None) -> None:
    """
    Thick mode'u etkinleştirir — tüm bağlantılar için geçerlidir.
    Oracle 11g gibi eski versiyonlar için gereklidir.

    lib_dir: Oracle Instant Client dizini.
             None ise sistem PATH'inden bulunmaya çalışılır.

    Örnek:
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


def build_connection(cfg: ConnectionConfig) -> oracledb.Connection:
    """
    Verilen config'e göre Oracle bağlantısı oluşturur.
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
def get_connection(cfg: ConnectionConfig, timeout_ms: Optional[int] = None):
    """
    Context manager — bağlantıyı açar, işi bitince kapatır.

    timeout_ms: Her sorgu için maksimum süre (ms).
                Aşılırsa ORA-03136 fırlatılır.
    """
    conn = build_connection(cfg)
    try:
        if timeout_ms is not None:
            conn.callTimeout = timeout_ms
        yield conn
    finally:
        conn.close()


def test_connection(cfg: ConnectionConfig) -> tuple[bool, str]:
    """
    Bağlantıyı test eder.
    Dönüş: (başarılı_mı, mesaj)
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
        