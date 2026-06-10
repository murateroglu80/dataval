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

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.service}"


@dataclass
class RowCountConfig:
    mode: str = "auto"
    sample_pct: float = 1.0
    timeout_sec: int = 30
    parallel_degree: int = 0
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
    sequences: bool = True
    grants: bool = False
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
    only_missing: bool = True          # sadece target'ta eksik olanları üret
    replace_schema: bool = True        # DDL içinde source schema → target schema
    include_invalid: bool = False      # INVALID durumdaki objeleri de üret (WARNING ekler)
    types: dict = field(default_factory=lambda: {
        "SEQUENCE":     True,
        "FUNCTION":     True,
        "PROCEDURE":    True,
        "PACKAGE":      True,
        "TRIGGER":      True,
        "TYPE":         True,
        "SYNONYM":      False,
        "GRANT":        True,
    })


@dataclass
class AppConfig:
    source: ConnectionConfig
    target: ConnectionConfig
    schemas: list[SchemaMapping]
    modules: ModulesConfig
    row_count: RowCountConfig
    ignore: IgnoreConfig
    generate_scripts: GenerateScriptsConfig = field(default_factory=GenerateScriptsConfig)


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


def _parse_connection(raw: dict) -> ConnectionConfig:
    return ConnectionConfig(
        host=raw["host"],
        port=int(raw.get("port", 1521)),
        service=raw["service"],
        username=raw["username"],
        password=_resolve_env(raw["password"]),
        sysdba=raw.get("sysdba", False),
        wallet_location=raw.get("wallet_location"),
    )


def _parse_modules(raw: dict) -> ModulesConfig:
    co = raw.get("code_objects", {})
    return ModulesConfig(
        inventory=raw.get("inventory", True),
        tables=raw.get("tables", True),
        indexes=raw.get("indexes", True),
        constraints=raw.get("constraints", True),
        sequences=raw.get("sequences", True),
        grants=raw.get("grants", False),
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
        raise ValueError("validation.yaml içinde en az bir schema mapping tanımlanmalı.")

    return AppConfig(
        source=_parse_connection(conn_raw["source"]),
        target=_parse_connection(conn_raw["target"]),
        schemas=schemas,
        modules=_parse_modules(val_raw.get("modules", {})),
        row_count=_parse_row_count(val_raw.get("row_count", {})),
        ignore=IgnoreConfig(**val_raw.get("ignore_differences", {})),
        generate_scripts=_parse_generate_scripts(val_raw.get("generate_scripts", {})),
    )


def _parse_generate_scripts(raw: dict) -> GenerateScriptsConfig:
    default_types = {
        "SEQUENCE":  True,
        "FUNCTION":  True,
        "PROCEDURE": True,
        "PACKAGE":   True,
        "TRIGGER":   True,
        "TYPE":      True,
        "SYNONYM":   False,
        "GRANT":     True,
    }
    types_raw = raw.get("types", {})
    merged = {k: types_raw.get(k, v) for k, v in default_types.items()}
    return GenerateScriptsConfig(
        enabled=raw.get("enabled", False),
        output_dir=raw.get("output_dir", "./ddl_output"),
        only_missing=raw.get("only_missing", True),
        replace_schema=raw.get("replace_schema", True),
        include_invalid=raw.get("include_invalid", False),
        types=merged,
    )
