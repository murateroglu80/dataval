# dataval

**dataval** — Database migration validation tool.  
Oracle 11g → 19c (ve ötesi) schema migration'larını CLI üzerinden hızlıca doğrular.

---

## Özellikler

- **Obje envanteri** — TABLE, INDEX, SEQUENCE, PROCEDURE, FUNCTION, PACKAGE, TRIGGER, TYPE ve daha fazlası için source/target sayı karşılaştırması
- **Tablo yapısı** — Kolon adı, tipi, nullable, default ve constraint (PK/UK/FK/CHECK) diff
- **Index validasyonu** — Tip, kolon ve uniqueness karşılaştırması; yeniden isimlendirme tespiti
- **Sequence kontrolü** — INCREMENT_BY, MIN/MAX, CACHE, CYCLE parametreleri; LAST_NUMBER toleransı
- **Kod objeleri** — DDL hash karşılaştırması (whitespace normalize, schema adı soyutlanır)
- **Akıllı row count** — `auto / exact / sample / stats / skip` modları; sorgu timeout; paralel hint
- **11g → 19c toleransı** — BASICFILE→SECUREFILE, SEGMENT CREATION DEFERRED gibi bilinen farklar WARNING olarak işaretlenir, FAIL değil
- **Sıfır Oracle Client** — `python-oracledb` thin mode; Oracle Instant Client kurulumu gerekmez
- **SYSDBA desteği** — `connections.yaml`'da `sysdba: true` ile DBA bağlantısı

---

## Kurulum

```bash
git clone https://github.com/murateroglu80/dataval.git
cd dataval
pip install -r requirements.txt
```

Python 3.10+ gerektirir.

---

## Yapılandırma

### 1. Bağlantı ayarları

```bash
cp config/connections.yaml.example config/connections.yaml
```

`config/connections.yaml` dosyasını düzenleyin:

```yaml
source:
  host: source-db.example.com
  port: 1521
  service: ORCL11G
  username: validator_user
  password: "$SOURCE_DB_PASS"   # veya düz metin

target:
  host: target-db.example.com
  port: 1521
  service: ORCL19C
  username: validator_user
  password: "$TARGET_DB_PASS"
```

Şifreleri environment variable ile geçirmek için:

```bash
export SOURCE_DB_PASS=MyPassword
export TARGET_DB_PASS=MyPassword
```

### 2. Validation ayarları

`config/validation.yaml` ile hangi schema'ların ve modüllerin çalışacağını belirleyin:

```yaml
schemas:
  - source: HR
    target: HR_NEW

modules:
  inventory: true
  tables: true
  indexes: true
  sequences: true
  code_objects:
    enabled: true
    types: [FUNCTION, PROCEDURE, PACKAGE, PACKAGE BODY, TRIGGER]

row_count:
  mode: auto          # auto | exact | sample | stats | skip
  timeout_sec: 30
  sample_pct: 1
```

---

## Kullanım

```bash
# Tüm modüller, config'deki schema ile
python run.py

# Schema override
python run.py -s HR -t HR_NEW

# Belirli modüller
python run.py -s HR -t HR_NEW --modules inventory,tables,indexes

# Row count — sample modu
python run.py -s HR -t HR_NEW --modules row_counts --count-mode sample --sample-pct 0.5

# Büyük tabloları atla, timeout düşür
python run.py --skip-tables AUDIT_LOG,BIG_EVENTS --query-timeout 15

# Sadece belirli tabloları say
python run.py --modules row_counts --only-tables ORDERS,CUSTOMERS

# İstatistik tazele, sonra say
python run.py --modules row_counts --count-mode exact --refresh-stats

# SYSDBA bağlantısı için connections.yaml'da sysdba: true ekleyin
```

---

## Row Count Stratejileri

| Mod | Ne yapar | Ne zaman kullan |
|-----|----------|-----------------|
| `exact` | `SELECT COUNT(*)` | < 1M satır |
| `sample` | `COUNT(*) ... SAMPLE(pct%)` | 1M–100M satır |
| `stats` | `ALL_TABLES.NUM_ROWS` | > 100M satır, sıfır I/O |
| `auto` | Threshold'a göre otomatik seçer | Genel kullanım |
| `skip` | Bu tabloyu atlar | Kritik olmayan büyük tablolar |

`auto` modunda eşikler `validation.yaml` → `row_count.auto_thresholds` ile yapılandırılır.

Tablo bazlı override:
```yaml
row_count:
  overrides:
    AUDIT_LOG: skip
    ORDERS: sample
```

---

## Örnek Çıktı

```
╔══════════════════════════════════╗
║  Oracle Migration Validator      ║
║  11g → 19c Schema Validation     ║
╚══════════════════════════════════╝

Bağlantı testi yapılıyor...
  ✅ SOURCE  source-db:1521/ORCL11G  — Oracle Database 11g Release 11.2.0.4.0
  ✅ TARGET  target-db:1521/ORCL19C  — Oracle Database 19c Enterprise Edition

──────────── Schema: HR → HR_NEW ────────────

╭─ INVENTORY  ✅ 12  ❌ 0  ⚠️  1  ⏭️  0 ───────────────────╮
│ TABLE       (toplam)   ✅ PASS    142      142             │
│ INDEX       (toplam)   ⚠️  WARN   387      391  +4 fazla   │
│ SEQUENCE    (toplam)   ✅ PASS    18       18              │
╰────────────────────────────────────────────────────────────╯

╭─ ROW_COUNTS  ✅ 138  ❌ 1  ⚠️  2  ⏱️  1 ──────────────────╮
│ TABLE  EMPLOYEES   ✅ PASS   107 [EXACT]      107 [EXACT]  │
│ TABLE  ORDERS      ✅ PASS   2,1M [SAMPLE]    2,1M [SAMPLE]│
│ TABLE  AUDIT_LOG   ⏱️  TIMEOUT               >30s          │
╰────────────────────────────────────────────────────────────╯

GENEL ÖZET
  ✅ PASS     152
  ❌ FAIL       1
  ⚠️  WARNING    3
  ⏱️  TIMEOUT    1
```

---

## Proje Yapısı

```
dataval/
├── config/
│   ├── connections.yaml.example   # Şablon — bağlantı bilgileri
│   └── validation.yaml            # Modül ve parametre ayarları
├── validator/
│   ├── config_loader.py           # YAML okuma, env var çözümleme
│   ├── connection.py              # oracledb thin mode bağlantı yönetimi
│   ├── result.py                  # ValidationResult veri modeli
│   └── modules/
│       ├── inventory.py           # Obje sayım karşılaştırması
│       ├── tables.py              # Kolon + constraint diff
│       ├── indexes.py             # Index yapısı karşılaştırması
│       ├── sequences.py           # Sequence parametre kontrolü
│       ├── code_objects.py        # DDL hash karşılaştırması
│       └── row_counts.py          # Akıllı row count stratejisi
├── run.py                         # CLI entry point
└── requirements.txt
```

---

## Gerekli Yetkiler

Validation kullanıcısının aşağıdaki yetkilere ihtiyacı vardır:

```sql
GRANT SELECT ON ALL_OBJECTS    TO validator_user;
GRANT SELECT ON ALL_TABLES     TO validator_user;
GRANT SELECT ON ALL_TAB_COLUMNS TO validator_user;
GRANT SELECT ON ALL_CONSTRAINTS TO validator_user;
GRANT SELECT ON ALL_CONS_COLUMNS TO validator_user;
GRANT SELECT ON ALL_INDEXES    TO validator_user;
GRANT SELECT ON ALL_IND_COLUMNS TO validator_user;
GRANT SELECT ON ALL_SEQUENCES  TO validator_user;
GRANT SELECT ON ALL_USERS      TO validator_user;
GRANT EXECUTE ON DBMS_METADATA TO validator_user;  -- kod objesi DDL için
-- İstatistik toplamak için (opsiyonel):
GRANT EXECUTE ON DBMS_STATS    TO validator_user;
```

---

## Roadmap

- [ ] `grants.py` — Object privilege karşılaştırması
- [ ] Paralel tablo sayımı (`ThreadPoolExecutor`)
- [ ] PostgreSQL desteği
- [ ] JSON çıktı modu (`--output json`)
- [ ] CI/CD entegrasyonu için exit code yönetimi

---

## Lisans

MIT
