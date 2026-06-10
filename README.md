# dataval

**dataval** — Database migration validation tool.  
Oracle 11g → 19c (ve ötesi) schema migration'larını CLI üzerinden hızlıca doğrular ve eksik objelerin DDL scriptlerini otomatik üretir.

---

## Özellikler

- **Obje envanteri** — TABLE, INDEX, SEQUENCE, PROCEDURE, FUNCTION, PACKAGE, TRIGGER, TYPE ve daha fazlası için source/target sayı karşılaştırması
- **Tablo yapısı** — Kolon adı, tipi, nullable, default ve constraint (PK/UK/FK/CHECK) diff
- **Index validasyonu** — Tip, kolon ve uniqueness karşılaştırması; yeniden isimlendirme tespiti
- **Sequence kontrolü** — INCREMENT_BY, MIN/MAX, CACHE, CYCLE parametreleri; LAST_NUMBER toleransı
- **Kod objeleri** — DDL hash karşılaştırması (whitespace normalize, schema adı soyutlanır)
- **Akıllı row count** — `auto / exact / sample / stats / skip` modları; sorgu timeout; paralel hint
- **DDL script üretimi** — Target'ta eksik objelerin SQL*Plus uyumlu create scriptlerini otomatik oluşturur
- **11g → 19c toleransı** — BASICFILE→SECUREFILE, SEGMENT CREATION DEFERRED gibi bilinen farklar WARNING olarak işaretlenir, FAIL değil
- **Source read-only koruma** — source production kabul edilir; varsayılan olarak bu bağlantıya `DBMS_STATS` dahil **hiçbir yazma** yapılmaz
- **Thin / Thick mode** — `python-oracledb` thin mode (Oracle 12.1+, Instant Client gerekmez) veya thick mode (Oracle 11g için zorunlu). Tek satır config ile seçilir
- **SYSDBA desteği** — `connections.yaml`'da `sysdba: true` ile DBA bağlantısı

---

## Kurulum

```bash
# GitHub
git clone https://github.com/murateroglu80/dataval.git

# veya Bitbucket
git clone https://bitbucket.org/mipsoftdev/dataval.git

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
# Oracle Client modu (process-global)
#   thin  → Instant Client gerekmez (Oracle 12.1+)
#   thick → Instant Client gerekir; Oracle 11g için ZORUNLU (11.2.0.4 → DPY-3010)
oracle_client:
  mode: thick
  lib_dir: ""                   # boş = sistem PATH; örn: C:/oracle/instantclient_21_9

source:
  host: source-db.example.com
  port: 1521
  service: ORCL11G
  username: valuser
  password: "$SOURCE_DB_PASS"   # veya düz metin
  # read_only varsayılan TRUE — source'a hiçbir yazma yapılmaz.
  # read_only: false            # yalnızca gerçekten gerekiyorsa

target:
  host: target-db.example.com
  port: 1521
  service: ORCL19C
  username: valuser
  password: "$TARGET_DB_PASS"
```

> **Source read-only koruması:** Source production kabul edildiğinden `read_only`
> varsayılanı **true**'dur. `--refresh-stats` verseniz bile source'a `DBMS_STATS`
> gönderilmez (yalnızca target yenilenir, sonuca WARNING eklenir). Source'ta taze satır
> sayısı için `--count-mode exact` (salt-okuma `COUNT(*)`) kullanın.

> **Thin vs Thick:** Oracle 11.2 source thin mode'da `DPY-3010` verir; bu yüzden 11g→19c
> doğrulamasında `oracle_client.mode: thick` ve Oracle Instant Client gereklidir. Thick
> mode process-global olduğundan her iki bağlantı da thick üzerinden çalışır (19c thick'i
> tam destekler). Instant Client kurulumu için bkz.
> [docs/troubleshooting.md](docs/troubleshooting.md).

Şifreleri environment variable ile geçirmek için:

**Linux / macOS:**
```bash
export SOURCE_DB_PASS=MyPassword
export TARGET_DB_PASS=MyPassword
```

**Windows (PowerShell):**
```powershell
$env:SOURCE_DB_PASS = "MyPassword"
$env:TARGET_DB_PASS = "MyPassword"
```

**Windows (CMD):**
```cmd
set SOURCE_DB_PASS=MyPassword
set TARGET_DB_PASS=MyPassword
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

### 3. DDL script üretimi ayarları

```yaml
generate_scripts:
  enabled: false                # true yapınca --generate-missing ile aktif olur
  output_dir: ./ddl_output      # scriptlerin yazılacağı klasör
  only_missing: true            # sadece target'ta eksik olanlar
  replace_schema: true          # DDL içinde source schema adını target ile değiştir
  include_invalid: false        # INVALID durumdaki objeleri de üret (WARNING eklenir)

  types:
    SEQUENCE:  true
    FUNCTION:  true
    PROCEDURE: true
    PACKAGE:   true             # PACKAGE BODY ayrı dosyada otomatik üretilir
    TRIGGER:   true
    TYPE:      true             # TYPE BODY ayrı dosyada otomatik üretilir
    SYNONYM:   false
    GRANT:     true             # source schema üzerindeki object grant'ları
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

# Validation + eksik objelerin DDL scriptlerini üret
python run.py --generate-missing

# Farklı klasöre yaz
python run.py --generate-missing --output-dir ./scripts/missing
```

---

## DDL Script Üretimi

`--generate-missing` flag'i validation sonucunda target'ta eksik bulunan objelerin SQL*Plus uyumlu create scriptlerini otomatik oluşturur.

**Desteklenen tipler:** SEQUENCE, FUNCTION, PROCEDURE, PACKAGE, PACKAGE BODY, TRIGGER, TYPE, TYPE BODY, SYNONYM, GRANT

**Önemli notlar:**
- TABLE ve INDEX kasıtlı olarak dışarıda bırakılmıştır. Bu tipler 11g→19c arasında TABLESPACE ve STORAGE farklılıkları içerdiğinden manuel müdahale gerektirir.
- SEQUENCE scriptleri `LAST_NUMBER` değerini korur — script `START WITH <mevcut_değer>` ile üretilir.
- PACKAGE seçildiğinde PACKAGE BODY ayrı dosyada otomatik oluşturulur. TYPE için TYPE BODY de aynı şekilde.
- INVALID durumdaki objeler `include_invalid: false` (varsayılan) ile atlanır ve uyarı verilir.
- Tüm dosyalar UTF-8 encoding ve `SET DEFINE OFF` başlığı ile SQL*Plus uyumlu üretilir.

**Çıktı dosya yapısı:**

```
ddl_output/
├── SOURCE_SCHEMA_TYPE.sql
├── SOURCE_SCHEMA_TYPE_BODY.sql
├── SOURCE_SCHEMA_SEQUENCE.sql
├── SOURCE_SCHEMA_FUNCTION.sql
├── SOURCE_SCHEMA_PROCEDURE.sql
├── SOURCE_SCHEMA_PACKAGE.sql
├── SOURCE_SCHEMA_PACKAGE_BODY.sql
├── SOURCE_SCHEMA_TRIGGER.sql
├── SOURCE_SCHEMA_SYNONYM.sql
├── SOURCE_SCHEMA_GRANT.sql
└── README_apply_order.txt
```

**Uygulama sırası** (bağımlılık hiyerarşisi):

```
TYPE → TYPE BODY → SEQUENCE → SYNONYM → FUNCTION →
PROCEDURE → PACKAGE → PACKAGE BODY → TRIGGER → GRANT
```

---

## Row Count Stratejileri

| Mod | Ne yapar | Ne zaman kullan |
|-----|----------|-----------------|
| `exact` | SELECT COUNT(*) | < 1M satır |
| `sample` | COUNT(*) SAMPLE(pct%) | 1M–100M satır |
| `stats` | ALL_TABLES.NUM_ROWS | > 100M satır, sıfır I/O |
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

## Proje Yapısı

```
dataval/
├── config/
│   ├── connections.yaml.example
│   └── validation.yaml
├── validator/
│   ├── config_loader.py
│   ├── connection.py
│   ├── result.py
│   └── modules/
│       ├── inventory.py
│       ├── tables.py
│       ├── indexes.py
│       ├── sequences.py
│       ├── code_objects.py
│       ├── row_counts.py
│       └── ddl_generator.py
├── run.py
└── requirements.txt
```

---

## Gerekli Yetkiler

Varsayılan kullanıcı adı `valuser`'dır. Aşağıdaki script kullanıcıyı oluşturup gerekli yetkileri verir.

```sql
CREATE USER valuser
  IDENTIFIED BY "ChangeMe123!"
  DEFAULT TABLESPACE USERS
  TEMPORARY TABLESPACE TEMP
  PROFILE DEFAULT
  ACCOUNT UNLOCK;

GRANT CREATE SESSION TO valuser;

GRANT SELECT ON ALL_OBJECTS      TO valuser;
GRANT SELECT ON ALL_TABLES       TO valuser;
GRANT SELECT ON ALL_TAB_COLUMNS  TO valuser;
GRANT SELECT ON ALL_CONSTRAINTS  TO valuser;
GRANT SELECT ON ALL_CONS_COLUMNS TO valuser;
GRANT SELECT ON ALL_INDEXES      TO valuser;
GRANT SELECT ON ALL_IND_COLUMNS  TO valuser;
GRANT SELECT ON ALL_SEQUENCES    TO valuser;
GRANT SELECT ON ALL_USERS        TO valuser;
GRANT SELECT ON ALL_TAB_PRIVS    TO valuser;
GRANT EXECUTE ON DBMS_METADATA   TO valuser;
GRANT EXECUTE ON DBMS_STATS      TO valuser;
```

> **Not:** `connections.yaml`'da `username: valuser` olarak ayarlayın.

---

## Bilinen Sorunlar / Troubleshooting

11g → 19c taşımalarında karşılaşılan farklar ve hatalar ayrı belgelerde toplanmıştır:

- **[docs/migration-11g-to-19c.md](docs/migration-11g-to-19c.md)** — bilinen 11g→19c
  farkları ve dataval'in her birini nasıl ele aldığı (LONG kolonlar, LOB storage, segment
  creation, CHECK koşulu biçimi, INVALID objeler, sequence LAST_NUMBER, …).
- **[docs/troubleshooting.md](docs/troubleshooting.md)** — sık ORA/DPY hataları ve
  çözümleri (DPY-3010, ORA-00932, thick mode başlatma, ORA-00942/01031 yetki, timeout).

Hızlı referans:

| Belirti | Olası neden | Bakınız |
|---------|-------------|---------|
| `DPY-3010` bağlantıda | 11g'ye thin mode | troubleshooting → thick mode |
| `ORA-00932 expected CHAR got LONG` | data dictionary LONG kolonu | migration §2 (giderildi) |
| `DPI-1047 / cannot locate Oracle Client` | Instant Client yok/PATH dışı | troubleshooting → thick |
| `PermissionError: Read-only baglantida...` | source koruması (beklenen) | troubleshooting → read-only |
| `TIMEOUT` statüsü | büyük tabloda exact sayım | `--skip-tables` / `--count-mode sample` |

## Roadmap

- [ ] Paralel tablo sayımı (ThreadPoolExecutor)
- [ ] PostgreSQL desteği
- [ ] JSON çıktı modu (--output json)
- [ ] CI/CD entegrasyonu için exit code yönetimi

---

## Lisans

MIT
