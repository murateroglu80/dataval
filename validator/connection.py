"""
Oracle bağlantı yönetimi — python-oracledb thin mode.
Oracle Instant Client gerekmez.
SYSDBA bağlantısı desteklenir.
"""

import oracledb
from contextlib import contextmanager
from typing import Optional
from validator.config_loader import ConnectionConfig


# oracledb thin mode — client kurulumu gerektirmez
# (thick mode için: oracledb.init_oracle_client() çağrısı gerekir)


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
        return False, str(e)


def fetch_all(conn: oracledb.Connection, sql: str, params: Optional[dict] = None) -> list[dict]:
    """
    SQL çalıştırır, sonuçları dict listesi olarak döner.
    params: named bind variables — {'schema': 'HR', ...}
    """
    cursor = conn.cursor()
    cursor.execute(sql, params or {})
    cols = [col[0].lower() for col in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def fetch_one(conn: oracledb.Connection, sql: str, params: Optional[dict] = None) -> Optional[dict]:
    """Tek satır döner, sonuç yoksa None."""
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None
