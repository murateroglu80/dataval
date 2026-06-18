"""
Config loader — YAML dosyalarını okur, env var referanslarını çözer.
"""

import os
import re
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Veri sınıfları
# ---------------------------------------------------------------------------

@dataclass
class ConnectionConfig:
    host: str
    port: int
    service: str
    username: str
    password: str
    sysdba: bool = False
    wallet_location: Optional[str] = None
    # read_only: bu bağlantıya hiçbir yazma (DBMS_STATS dahil) gönderilmesini engeller.
    # Source için varsayılan True'dur (production koruması), target için False.
    read_only: bool = True
    # thick_mode / client_lib_dir: GERİYE UYUMLULUK için korunur. Yeni yapılandırma
    # connections.yaml içindeki top-level `oracle_client` bloğudur (bkz. OracleClientConfig).
    thick_mode: bool = False
    client_lib_dir: Optional[str] = None

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.service}"


@dataclass
class OracleClientConfig:
    """
    python-oracledb sürücü modu (process-global).
    mode: "thin"  → Oracle Instant Client gerekmez (Oracle 12.1+).
          "thick" → Instant Client gerekir; Oracle 11g (11.2.0.4 → DPY-3010) için zorunlu.
    lib_dir: Instant Client dizini; None/boş = sistem PATH.
    """
    mode: str = "thin"
    lib_dir: Optional[str] = None


@dataclass
class RowCountConfig:
    mode: str = "auto"
    sample_pct: float = 1.0
    timeout_sec: int = 30
    # parallel_degree: Oracle'in TEK SORGU ICI PARALLEL hint derecesi (intra-query).
    parallel_degree: int = 0
    # parallel_workers: TABLOLAR ARASI thread eszamanliligi (inter-table). 1 = seri
    # (mevcut davranis, varsayilan). >1 → exact/sample sayimlari ThreadPoolExecutor +
    # baglanti havuzu ile paralel calisir.
    parallel_workers: int = 1
    # source_max_workers: source (production) havuzu icin ayri, daha dusuk tavan.
    # Etkin source worker = max(1, min(parallel_workers, source_max_workers)).
    source_max_workers: int = 4
    refresh_stats: bool = False
    stats_max_age_days: int = 7
    overrides: dict = field(default_factory=dict)
    auto_thresholds: dict = field(default_factory=lambda: {
        "exact_below": 1_000_000,
        "sample_below": 100_000_000,
    })


@dataclass
class ModulesConfig:
    inventory: bool = True
    tables: bool = True
    indexes: bool = True
    constraints: bool = True
    # constraint_types: hangi constraint tipleri karşılaştırılır/üretilir.
    # {"PK","UK","FK","CHECK"} (ALL eşdeğeri) veya bir alt küme.
    constraint_types: set = field(default_factory=lambda: {"PK", "UK", "FK", "CHECK"})
    sequences: bool = True
    grants: bool = False
    # users: instance-wide kullanıcı + sistem/rol/object yetki izolasyonu (opt-in).
    users: bool = False
    # include_temp_tables: Global Temporary Table'ları (all_tables.temporary='Y')
    # doğrulama kapsamına alır. Default False → GTT'ler hem tables hem constraints
    # modülünde gürültü yaratmaması için atlanır.
    include_temp_tables: bool = False
    code_objects_enabled: bool = True
    code_object_types: list = field(default_factory=lambda: [
        "FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY",
        "TRIGGER", "TYPE", "TYPE BODY"
    ])
    normalize_whitespace: bool = True


@dataclass
class IgnoreConfig:
    lob_storage: bool = True
    segment_creation: bool = True
    tablespace: bool = True
    index_compression: bool = True


@dataclass
class SchemaMapping:
    source: str
    target: str


@dataclass
class GenerateScriptsConfig:
    enabled: bool = False
    output_dir: str = "./ddl_output"
    only_missing: bool = True
    replace_schema: bool = True
    include_invalid: bool = False
    types: dict = field(default_factory=lambda: {
        "SEQUENCE":     True,
        "FUNCTION":     True,
        "PROCEDURE":    True,
        "PACKAGE":      True,
        "TRIGGER":      True,
        "TYPE":         True,
        "SYNONYM":      False,
        "INDEX":        True,
        "CONSTRAINT":   True,
        "GRANT":        True,
    })


@dataclass
class OutputConfig:
    """
    Çıktı / raporlama ayarları (eski `debug` bloğunun yerine).
    - level (sync/not-sync/failed): HEM terminal tablosu HEM canlı ekran HEM dosya
      logu için TEK eşik. sync=her şey, not-sync=NOT-SYNC+FAILED (default),
      failed=yalnızca FAILED. SKIPPED daima sync seviyesindedir.
    - extra_as (not-sync/sync): target'ta FAZLA (kaynakta yok) objelerin statüsü.
      not-sync=göster (default), sync=gizle.
    - log_file: boş → ./logs/dataval_<zaman>.log otomatik üretilir. Dosya logu
      HER ZAMAN açıktır (level eşiğiyle süzülür).
    - live: True → ek olarak canlı stderr akışı açılır (--debug ile de açılabilir).
    """
    level: str = "not-sync"
    extra_as: str = "not-sync"
    log_file: Optional[str] = None
    live: bool = False


@dataclass
class AppConfig:
    source: ConnectionConfig
    target: ConnectionConfig
    schemas: list[SchemaMapping]
    modules: ModulesConfig
    row_count: RowCountConfig
    ignore: IgnoreConfig
    generate_scripts: GenerateScriptsConfig = field(default_factory=GenerateScriptsConfig)
    oracle_client: OracleClientConfig = field(default_factory=OracleClientConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


# ---------------------------------------------------------------------------
# Yardımcı: env var çözümleme
# ---------------------------------------------------------------------------

def _resolve_env(value: str) -> str:
    """$VAR_NAME formatındaki değerleri environment variable ile değiştirir."""
    if isinstance(value, str) and value.startswith("$"):
        var_name = value[1:]
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise EnvironmentError(
                f"Environment variable '{var_name}' tanımlı değil. "
                f"Lütfen export {var_name}=<password> ile set edin."
            )
        return resolved
    return value


def _parse_connection(raw: dict, is_source: bool = False) -> ConnectionConfig:
    # read_only varsayılanı: source → True (production koruması), target → False.
    # Kullanıcı connections.yaml'da açıkça belirtirse o değer geçerli olur.
    default_read_only = True if is_source else False
    return ConnectionConfig(
        host=raw["host"],
        port=int(raw.get("port", 1521)),
        service=raw["service"],
        username=raw["username"],
        password=_resolve_env(raw["password"]),
        sysdba=raw.get("sysdba", False),
        wallet_location=raw.get("wallet_location"),
        read_only=raw.get("read_only", default_read_only),
        thick_mode=raw.get("thick_mode", False),
        client_lib_dir=raw.get("client_lib_dir") or None,
    )


def _parse_oracle_client(raw: dict, source_cfg: ConnectionConfig) -> OracleClientConfig:
    """
    Top-level `oracle_client` bloğunu okur. Blok yoksa GERİYE UYUMLULUK için
    eski `source.thick_mode` / `source.client_lib_dir` alanlarından türetir.
    """
    if raw:
        return OracleClientConfig(
            mode=str(raw.get("mode", "thin")).lower(),
            lib_dir=raw.get("lib_dir") or None,
        )
    if source_cfg.thick_mode:
        return OracleClientConfig(mode="thick", lib_dir=source_cfg.client_lib_dir)
    return OracleClientConfig()


_ALL_CONSTRAINT_TYPES = {"PK", "UK", "FK", "CHECK"}


def _parse_constraint_types(raw_val) -> set:
    """`ALL`/boş → tüm tipler; liste → upper-normalize alt küme (geçersiz → ALL)."""
    if raw_val is None:
        return set(_ALL_CONSTRAINT_TYPES)
    if isinstance(raw_val, str):
        if raw_val.strip().upper() in ("ALL", ""):
            return set(_ALL_CONSTRAINT_TYPES)
        raw_val = [raw_val]
    sel = {str(t).strip().upper() for t in raw_val}
    sel &= _ALL_CONSTRAINT_TYPES
    return sel or set(_ALL_CONSTRAINT_TYPES)


def _parse_modules(raw: dict) -> ModulesConfig:
    co = raw.get("code_objects", {})
    return ModulesConfig(
        inventory=raw.get("inventory", True),
        tables=raw.get("tables", True),
        indexes=raw.get("indexes", True),
        constraints=raw.get("constraints", True),
        constraint_types=_parse_constraint_types(raw.get("constraint_types", "ALL")),
        sequences=raw.get("sequences", True),
        grants=raw.get("grants", False),
        users=raw.get("users", False),
        include_temp_tables=bool(raw.get("include_temp_tables", False)),
        code_objects_enabled=co.get("enabled", True),
        code_object_types=co.get("types", [
            "FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY",
            "TRIGGER", "TYPE", "TYPE BODY"
        ]),
        normalize_whitespace=co.get("normalize_whitespace", True),
    )


def _parse_row_count(raw: dict) -> RowCountConfig:
    thresholds = raw.get("auto_thresholds", {})
    return RowCountConfig(
        mode=raw.get("mode", "auto"),
        sample_pct=float(raw.get("sample_pct", 1.0)),
        timeout_sec=int(raw.get("timeout_sec", 30)),
        parallel_degree=int(raw.get("parallel_degree", 0)),
        parallel_workers=int(raw.get("parallel_workers", 1)),
        source_max_workers=int(raw.get("source_max_workers", 4)),
        refresh_stats=raw.get("refresh_stats", False),
        stats_max_age_days=int(raw.get("stats_max_age_days", 7)),
        overrides=raw.get("overrides") or {},
        auto_thresholds={
            "exact_below": thresholds.get("exact_below", 1_000_000),
            "sample_below": thresholds.get("sample_below", 100_000_000),
        },
    )


# ---------------------------------------------------------------------------
# Ana loader
# ---------------------------------------------------------------------------

def load_config(
    connections_path: str = "config/connections.yaml",
    validation_path: str = "config/validation.yaml",
) -> AppConfig:
    base = Path(__file__).parent.parent

    with open(base / connections_path, encoding="utf-8") as f:
        conn_raw = yaml.safe_load(f)

    with open(base / validation_path, encoding="utf-8") as f:
        val_raw = yaml.safe_load(f)

    schemas = [
        SchemaMapping(source=s["source"].upper(), target=s["target"].upper())
        for s in val_raw.get("schemas", [])
    ]
    if not schemas:
        raise ValueError("validation.yaml icinde en az bir schema mapping tanimlanmali.")

    source = _parse_connection(conn_raw["source"], is_source=True)
    target = _parse_connection(conn_raw["target"], is_source=False)

    return AppConfig(
        source=source,
        target=target,
        schemas=schemas,
        modules=_parse_modules(val_raw.get("modules", {})),
        row_count=_parse_row_count(val_raw.get("row_count", {})),
        ignore=IgnoreConfig(**val_raw.get("ignore_differences", {})),
        generate_scripts=_parse_generate_scripts(val_raw.get("generate_scripts", {})),
        oracle_client=_parse_oracle_client(conn_raw.get("oracle_client", {}), source),
        output=_parse_output(val_raw.get("output", {}), val_raw.get("debug", {})),
    )


def _parse_output(raw: dict, legacy_debug: dict) -> OutputConfig:
    """
    `output:` bloğunu okur. Yoksa GERİYE UYUMLULUK için eski `debug:` bloğundan türetir
    (enabled→live, log_file taşınır; log_level — INFO/WARNING/ERROR — artık YOK SAYILIR).
    Geçersiz level/extra_as değerleri sessizce default'a düşürülür.
    """
    valid_levels = {"sync", "not-sync", "failed"}
    valid_extra = {"sync", "not-sync"}

    src = raw if raw else {}
    level = str(src.get("level", "not-sync")).lower()
    extra_as = str(src.get("extra_as", "not-sync")).lower()
    log_file = src.get("log_file")
    # `output` yoksa eski debug bloğundan live/log_file devral.
    live = bool(src.get("live", legacy_debug.get("enabled", False)))
    if log_file is None:
        log_file = legacy_debug.get("log_file")

    return OutputConfig(
        level=level if level in valid_levels else "not-sync",
        extra_as=extra_as if extra_as in valid_extra else "not-sync",
        log_file=log_file or None,
        live=live,
    )


def _parse_generate_scripts(raw: dict) -> GenerateScriptsConfig:
    # Tip varsayılanları TEK kaynaktan gelir (GenerateScriptsConfig.types) —
    # ikinci bir kopya tutmak drift'e yol açar (örn. INDEX/CONSTRAINT). YAML'deki
    # değerler bu varsayılanların üzerine deep-merge edilir; bilinmeyen anahtarlar
    # da korunur (sessizce düşürülmez), büyük/küçük harf normalize edilir.
    default_types = GenerateScriptsConfig().types
    types_raw = {str(k).upper(): v for k, v in (raw.get("types", {}) or {}).items()}
    merged = dict(default_types)
    merged.update(types_raw)
    return GenerateScriptsConfig(
        enabled=raw.get("enabled", False),
        output_dir=raw.get("output_dir", "./ddl_output"),
        only_missing=raw.get("only_missing", True),
        replace_schema=raw.get("replace_schema", True),
        include_invalid=raw.get("include_invalid", False),
        types=merged,
    )
