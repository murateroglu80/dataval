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
- **Grants (yetki) doğrulama** — source ↔ target object privilege karşılaştırması; `DBA_TAB_PRIVS` tercih, `ALL_TAB_PRIVS` fallback; `GRANTABLE`/`HIERARCHY` farkı NOT-SYNC olarak raporlanır (opt-in)
- **Users (kullanıcı + yetki izolasyonu)** — **şema-bağımsız, instance-wide global** akış (schema döngüsünden önce bir kez koşar, `cfg.schemas`'a bakmaz): app kullanıcılarının varlığı/öznitelikleri, sistem yetkileri (`DBA_SYS_PRIVS`), rol grant'ları (`DBA_ROLE_PRIVS`) ve grantee-merkezli object grant'lar karşılaştırılır. Sistem user'ları (SYS, XDB, APEX_*, SPATIAL_* …) **dinamik** elenir (19c `oracle_maintained`, 11g statik fallback). `--modules users` → diğer modüller (`tables`/`indexes`/…) `true` olsa bile **atlanır** (Execution Isolation). Eksik user'lar için **dry-run / yorumlu** `CREATE USER` + grant iskeleti üretilir; `password_sync: true` ile **çalıştırılabilir** `CREATE/ALTER USER ... IDENTIFIED BY VALUES`'a (11g→19c verifier taşıma) ve ortak user'larda **parola-farkı NOT-SYNC** tespitine yükseltilir (opt-in)
- **Akıllı row count** — `auto / exact / sample / stats / skip` modları; sorgu timeout; paralel hint
- **DDL script üretimi** — Target'ta eksik objelerin SQL*Plus uyumlu create scriptlerini otomatik oluşturur. **Tamamen native** (`ALL_SOURCE` / `ALL_TRIGGERS` / `ALL_SYNONYMS` / `ALL_INDEXES` / `ALL_CONSTRAINTS`) — `DBMS_METADATA` kullanılmaz, böylece Oracle 11g `ORA-03113` riski yoktur. SEQUENCE, PL/SQL, TRIGGER, SYNONYM, **INDEX**, **CONSTRAINT**, GRANT (yalnız eksik) ve **USER** (dry-run/yorumlu) üretilir. Üretim **Execution Guard**'a tabidir: bir tip yalnızca onu sahiplenen validation modülü açıksa üretilir
- **Migration statü jargonu** — her sonuç `SYNC` (eşit) / `NOT-SYNC` (var ama farklı) / `FAILED` (target'ta eksik/doğrulanamadı) olarak sınıflanır; tek eşik (`level`) terminal + log gürültüsünü kısar
- **11g → 19c toleransı** — BASICFILE→SECUREFILE, SEGMENT CREATION DEFERRED gibi bilinen farklar ignore edilir, sorun sayılmaz
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
> gönderilmez (yalnızca target yenilenir, sonuca açıklama notu eklenir). Source'ta taze satır
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
  constraints: true        # PK/UK/FK/CHECK karşılaştırması — AYRI modül (aşağıya bkz)
  indexes: true
  sequences: true
  grants: false            # object privilege karşılaştırması (opt-in; DBA_TAB_PRIVS tercih)
  users: false             # instance-wide kullanıcı + sistem/rol/object yetki izolasyonu (opt-in)
                           # şema-bağımsız global akış; --modules users → diğer modüller atlanır
  password_sync: false     # users + generate ile: 11g→19c parola hash (IDENTIFIED BY VALUES)
                           # senkronu + parola-farkı NOT-SYNC. HASSAS — bkz aşağıdaki not (opt-in)
  include_temp_tables: false  # Global Temporary Table'ları (temporary='Y') kapsa (default: hayır)
  code_objects:
    enabled: true
    types: [FUNCTION, PROCEDURE, PACKAGE, PACKAGE BODY, TRIGGER]

row_count:
  mode: auto          # auto | exact | sample | stats | skip
  timeout_sec: 30
  sample_pct: 1
  parallel_workers: 1     # tablolar arası eşzamanlılık (1 = seri)
  source_max_workers: 4   # source (production) için ayrı, daha düşük tavan
```

> **Constraints ayrı bir modüldür.** PK/UK/FK/CHECK karşılaştırması `modules.constraints`
> bayrağıyla yönetilir ve `tables`'tan bağımsız çalışır/kapanır. `constraints: false` →
> hiç constraint kontrolü yapılmaz (kendi paneli de basılmaz). `--modules constraints` ile
> tek başına da çalıştırılabilir. (Önceden bu kontrol `tables` modülüne gömülüydü ve bu
> bayrağı yok sayıyordu; artık düzeltildi.)

> **Geçici tablolar.** Global Temporary Table'lar (`all_tables.temporary='Y'`) migration'da
> çoğu zaman yapısal kopya değildir; default olarak `tables` ve `constraints` kapsamından
> çıkarılır. Kontrol etmek istiyorsan `modules.include_temp_tables: true` yap.

### 3. DDL script üretimi ayarları

```yaml
generate_scripts:
  enabled: false                # true yapınca --generate-missing ile aktif olur
  output_dir: ./ddl_output      # scriptlerin yazılacağı klasör
  only_missing: true            # sadece target'ta eksik olanlar
  replace_schema: true          # DDL içinde source schema adını target ile değiştir
  include_invalid: false        # INVALID durumdaki objeleri de üret (script'e not eklenir)

  types:
    SEQUENCE:  true
    FUNCTION:  true
    PROCEDURE: true
    PACKAGE:   true             # PACKAGE BODY ayrı dosyada otomatik üretilir
    TRIGGER:   true
    TYPE:      true             # TYPE BODY ayrı dosyada otomatik üretilir
    SYNONYM:   false
    INDEX:     true             # eksik index'ler (ALL_INDEXES native; UNIQUE/BITMAP/DESC/FBI)
    CONSTRAINT: true            # eksik PK/UK/FK/CHECK (ALTER TABLE ADD; PK/UK→FK sıralı)
    GRANT:     true             # yalnız EKSİK object grant'lar (modules.grants açıksa)
```

> **GRANT artık yalnız eksik üretilir (v0.9.1+).** Üretim source↔target diff'ine dayanır —
> target'ta zaten var olan (SYNC) grant'lar script'e **girmez**. Ayrıca **Execution Guard**'a
> tabidir: `modules.grants: false` ise `types.GRANT: true` olsa bile GRANT scripti üretilmez
> (fetch/diff/stdout/DDL hiç çalışmaz). Aynı kural `USER` üretimini `modules.users`'a bağlar.

> **Tamamen native üretim (v0.7.0+).** DDL üretimi artık `DBMS_METADATA` kullanmaz; her tip
> ilgili sözlük görünümünden (`ALL_SOURCE`, `ALL_TRIGGERS`, `ALL_SYNONYMS`, `ALL_INDEXES`,
> `ALL_CONSTRAINTS`, `ALL_SEQUENCES`) elle kurulur. Bu sayede `DBMS_METADATA.GET_DDL`'in 11g'de
> oturumu çökerttiği `ORA-03113` sorunu tamamen ortadan kalkar ve PL/SQL kaynağı `ALL_SOURCE`'tan
> birebir alınır.

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

**Desteklenen tipler:** SEQUENCE, FUNCTION, PROCEDURE, PACKAGE, PACKAGE BODY, TRIGGER, TYPE, TYPE BODY, SYNONYM, **INDEX**, **CONSTRAINT** (PK/UK/FK/CHECK), GRANT (yalnız eksik), **USER** (dry-run/yorumlu)

**Önemli notlar:**
- Üretim **tamamen native**'dir — hiçbir tip `DBMS_METADATA` kullanmaz (11g `ORA-03113` riski yok).
- TABLE kasıtlı olarak dışarıda bırakılmıştır; 11g→19c arasında TABLESPACE/STORAGE farklılıkları manuel müdahale gerektirir. **INDEX artık üretilir** (`ALL_INDEXES`'ten native; UNIQUE/BITMAP/`DESC`/function-based).
- **CONSTRAINT** (PK/UK/FK/CHECK) eksik bulunduğunda `ALTER TABLE ADD CONSTRAINT` olarak üretilir; bağımlılık için PK/UK önce, FK sonra sıralanır. FK referans tablo/kolonları ve `DELETE_RULE` (`ON DELETE CASCADE/SET NULL`) korunur.
- SEQUENCE scriptleri `LAST_NUMBER` değerini korur — script `START WITH <mevcut_değer>` ile üretilir.
- PL/SQL DDL'i `ALL_SOURCE`'tan birebir alınır; hedef şema yalnız `CREATE` başlığına enjekte edilir, gövde metni bozulmaz.
- PACKAGE seçildiğinde PACKAGE BODY ayrı dosyada otomatik oluşturulur. TYPE için TYPE BODY de aynı şekilde.
- INVALID durumdaki objeler `include_invalid: false` (varsayılan) ile atlanır ve uyarı verilir (INVALID kontrolü yalnız PL/SQL tiplerine uygulanır).
- **GRANT yalnız eksik (FAILED) grant'lar için** üretilir (source∖target diff); SYNC grant'lar script'e girmez. `modules.grants: false` ise hiç üretilmez (Execution Guard).
- **USER (kullanıcı provisioning)** yalnız `modules.users` açıkken ve eksik (FAILED) user/yetki bulununca üretilir. Üretim **global/şema-bağımsızdır** → tek `<DB_ADI>_USER.sql` dosyası (her şema için tekrar üretilmez).
  - `password_sync: false` (default) → çıktı **tamamen yorumlu / dry-run**'dır: `CREATE USER … IDENTIFIED BY "<<PAROLAYI_BELIRLEYIN>>"` iskeleti + reconstructed `GRANT`. **Parola hash'i hiç okunmaz/loglanmaz**; bilinçli gözden geçirip açmadan hiçbir satır çalışmaz.
  - `password_sync: true` → çıktı **çalıştırılabilir**: `CREATE USER … IDENTIFIED BY VALUES '<verifier>'` (source'tan `SYS.USER$.SPARE4`/`PASSWORD` ile 11g→19c verifier taşınır) + gerçek `DEFAULT TABLESPACE`/`PROFILE` + reconstructed `GRANT`. Ortak user'larda **parola farkı NOT-SYNC** olarak da raporlanır (rapor/loga yalnız **maskeli** ilk 6 karakter gider, gerçek hash asla). ⚠️ **HASSAS DOSYA:** üretilen `<DB>_USER.sql` canlı verifier içerir — izinlerini kısıtlayın (chmod 600), repoya commit'lemeyin, uyguladıktan sonra silin. 19c'de uygulanması `SEC_CASE_SENSITIVE_LOGON` / `SQLNET.ALLOWED_LOGON_VERSION_SERVER` ayarı gerektirebilir (dosya başında uyarı vardır).
- Tüm dosyalar UTF-8 encoding ve `SET DEFINE OFF` başlığı ile SQL*Plus uyumlu üretilir.

**Çıktı dosya yapısı:**

```
ddl_output/
├── TARGET_SCHEMA_TYPE.sql
├── TARGET_SCHEMA_TYPE_BODY.sql
├── TARGET_SCHEMA_SEQUENCE.sql
├── TARGET_SCHEMA_SEQUENCE_ALTER.sql   # NOT-SYNC sequence hizalama (ALTER, opsiyonel)
├── TARGET_SCHEMA_SYNONYM.sql
├── TARGET_SCHEMA_INDEX.sql
├── TARGET_SCHEMA_CONSTRAINT.sql       # PK/UK/FK/CHECK (ALTER TABLE ADD)
├── TARGET_SCHEMA_FUNCTION.sql
├── TARGET_SCHEMA_PROCEDURE.sql
├── TARGET_SCHEMA_PACKAGE.sql
├── TARGET_SCHEMA_PACKAGE_BODY.sql
├── TARGET_SCHEMA_TRIGGER.sql
├── TARGET_SCHEMA_GRANT.sql            # yalnız eksik object grant'lar (modules.grants)
├── <DB_ADI>_USER.sql                  # kullanıcı + yetki provisioning — GLOBAL/şema-bağımsız (modules.users)
│                                       #   password_sync=false → DRY-RUN/yorumlu · true → çalıştırılabilir/HASSAS
└── README_apply_order.txt
```

**Uygulama sırası** (bağımlılık hiyerarşisi):

```
TYPE → TYPE BODY → SEQUENCE → SYNONYM → INDEX → CONSTRAINT →
FUNCTION → PROCEDURE → PACKAGE → PACKAGE BODY → TRIGGER → GRANT
```

> `USER` dosyası tamamen dry-run/yorumlu (advisory) olduğundan apply-order listesinin **sonunda**
> yer alır; uygulanmadan önce elle gözden geçirilmesi beklenir.

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

### Paralel sayım

`exact`/`sample` sayımları varsayılan olarak **seri** çalışır. Çok tablolu şemalarda
`parallel_workers` ile tablolar arası eşzamanlılık açılır:

```yaml
row_count:
  mode: exact
  parallel_workers: 8       # target: 8 eşzamanlı COUNT(*)
  source_max_workers: 4     # source (production): en fazla 4
```
veya CLI: `python run.py --modules row_counts --count-mode exact --parallel-workers 8`

Nasıl çalışır:
- Source ve target için **ayrı `python-oracledb` bağlantı havuzu** + ayrı
  `ThreadPoolExecutor` kullanılır. **Havuz boyutu = worker sayısı** (her worker kendi
  bağlantısını alır, beklemez).
- **Source koruması:** source havuzu `source_max_workers` ile ayrıca sınırlanır
  (etkin = `min(parallel_workers, source_max_workers)`), böylece production 11g
  eşzamanlı `COUNT(*)` yükünden korunur; target tam hızda sayılır.
- `timeout_sec` her bağlantıda `callTimeout` olarak uygulanır → kilitli/iri tablo bir
  worker'ı sonsuza dek bloklamaz (TIMEOUT). Bir tablonun hatası (ör. ORA-00942) diğerlerini
  durdurmaz; o tablo **ERROR** olarak raporlanır.
- Tablo/şema adları SQL'e gömülmeden önce `^[A-Za-z][A-Za-z0-9_$#]*$` ile doğrulanır;
  geçersiz ad **ERROR** olur (SQL injection savunması).
- `parallel_workers: 1` (varsayılan) → davranış ve çıktı eski seri yolla birebir aynı.

> ⚠️ **`parallel_workers` ≠ `parallel_degree`.** `parallel_degree`, Oracle'ın *tek bir
> sorgu içindeki* `/*+ PARALLEL(t, N) */` hint derecesidir. `parallel_workers` ise
> *tablolar arası* thread eşzamanlılığıdır. İkisi bağımsızdır ve birlikte kullanılabilir.

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
│       ├── constraints.py
│       ├── indexes.py
│       ├── sequences.py
│       ├── code_objects.py
│       ├── grants.py
│       ├── users.py
│       ├── row_counts.py
│       └── ddl_generator.py
├── run.py
└── requirements.txt
```

---

## Gerekli Yetkiler

`valuser`, **başka şemaların** (ör. `CTROMSADMIN`) objelerini doğrular. Oracle'da
`ALL_*` sözlük görünümleri yalnızca kullanıcının **yetkili olduğu** objelerin satırlarını
gösterir; dolayısıyla başka bir şemayı görebilmek için **sistem (ANY) yetkileri** gerekir.

> ⚠️ **Sık yapılan hata:** Yalnızca `GRANT SELECT ON ALL_TABLES ...` vermek **yetmez** —
> bu görünümler zaten PUBLIC'e açıktır ve satır görünürlüğünü değiştirmez. Sonuç: araç
> "0 obje" görür ve yanıltıcı şekilde **TEMIZ** raporlar. Aşağıdaki ANY yetkileri gereklidir.

Aşağıdaki script kullanıcıyı oluşturup **salt-okuma** doğrulama için gereken yetkileri verir:

```sql
CREATE USER valuser
  IDENTIFIED BY "ChangeMe123!"
  DEFAULT TABLESPACE USERS
  TEMPORARY TABLESPACE TEMP
  PROFILE DEFAULT
  ACCOUNT UNLOCK;

GRANT CREATE SESSION TO valuser;

-- Diğer şemaların objelerini ALL_* görünümlerinde GÖREBİLMEK için (metadata görünürlüğü)
GRANT SELECT ANY TABLE      TO valuser;   -- tablo/view metadata + row_counts COUNT(*)
GRANT SELECT ANY SEQUENCE   TO valuser;   -- sequence'ler
GRANT EXECUTE ANY PROCEDURE TO valuser;   -- procedure / function / package görünürlüğü
GRANT EXECUTE ANY TYPE      TO valuser;   -- type görünürlüğü

-- Native DDL üretimi (ALL_SOURCE/ALL_INDEXES/ALL_CONSTRAINTS …) + katalog erişimi +
-- grants/users modüllerinin DBA_* görünümlerini okuyabilmesi (DBA_TAB_PRIVS, DBA_USERS,
-- DBA_SYS_PRIVS, DBA_ROLE_PRIVS + 19c oracle_maintained; yoksa ALL_* fallback / statik filtre)
GRANT SELECT_CATALOG_ROLE   TO valuser;   -- alternatif: GRANT SELECT ANY DICTIONARY

-- (YALNIZCA modules.password_sync=true ile parola hash senkronu kullanacaksanız —
--  verifier okuması SYS.USER$ gerektirir. En düşük ayrıcalık: aşağıdaki tek grant.
--  Yoksa DBA_USERS.PASSWORD (yalnız 11g) denenir; o da yoksa placeholder üretilir.)
GRANT SELECT ON SYS.USER$   TO valuser;   -- ya da source'a `sysdba` ile bağlanın

-- (YALNIZCA target'ta --refresh-stats kullanacaksanız. Source read-only olduğundan
--  orada DBMS_STATS asla çalıştırılmaz — kod düzeyinde de engellenir.)
GRANT ANALYZE ANY           TO valuser;
```

**Notlar:**
- Tüm bu yetkiler **salt-okuma**dır (SELECT / EXECUTE / ANALYZE) — veri değiştirmez.
  `SELECT ANY TABLE` yalnızca okuma (COUNT/SELECT) sağlar, yazma içermez.
- `connections.yaml`'da `username: valuser` olarak ayarlayın.
- **Alternatif (daha kolay):** DBA yetkili bir kullanıcıyla ya da `connections.yaml`'da
  `sysdba: true` ile bağlanırsanız bu ANY yetkilerine gerek kalmaz.
- Bir şema **0 obje** döndürürse araç artık sessizce TEMIZ demez; `SCHEMA ... ❌ FAILED —
  görünür obje yok (şema adı/yetki kontrol et)` uyarısı verir.

### Paralel sayım — oturum gereksinimleri

Paralel sayım (`parallel_workers > 1`) tek bağlantı yerine bir **bağlantı havuzu** açar.
Bir şema işlenirken tepe eşzamanlı oturum sayısı:

| Taraf | Tepe oturum | Örnek (`parallel_workers: 8`, `source_max_workers: 4`) |
|-------|-------------|--------------------------------------------------------|
| Source | `min(parallel_workers, source_max_workers) + 1` | **5** |
| Target | `parallel_workers + 1` | **9** |

> Şemalar sırayla işlenir → bu sayı şemalar arası **toplanmaz**, tepe yukarıdaki gibidir.
> `+1` her tarafta istatistikleri okuyan temel bağlantıdır.

**1) `SESSIONS_PER_USER` profil sınırı.** `valuser`'ın profilinde bu sınır tepe değerin
altındaysa paralel modda **ORA-02391: exceeded simultaneous SESSIONS_PER_USER limit**
alırsınız. Kontrol edip gerekirse yükseltin:

```sql
SELECT limit FROM dba_profiles
 WHERE profile = (SELECT profile FROM dba_users WHERE username = 'VALUSER')
   AND resource_name = 'SESSIONS_PER_USER';

-- UNLIMITED değilse: SESSIONS_PER_USER >= parallel_workers + 2 olmalı
ALTER PROFILE <profil> LIMIT SESSIONS_PER_USER 16;
```
Pratik kural: **`SESSIONS_PER_USER ≥ parallel_workers + 2`** (target en yüksek taraftır).

**2) Oturum izleme (`v$session`).** Çalışma sırasında oturumları doğrulamak/izlemek için
(`V$SESSION`, `V$SESSION_LONGOPS`, `V$SQL`) **ek yetki gerekmez** — bunlar yukarıda
verilen **`SELECT_CATALOG_ROLE`** ile zaten gelir. Daha dar bir alternatif isterseniz:

```sql
GRANT SELECT ON V_$SESSION         TO valuser;  -- V$SESSION'ın arkasındaki view
GRANT SELECT ON V_$SESSION_LONGOPS TO valuser;
```
Örnek izleme sorgusu (paralel çalışma sırasında oturum sayımı):
```sql
SELECT username, COUNT(*) FROM v$session
 WHERE username = 'VALUSER' GROUP BY username;
```

**3) Oturum sonlandırma yetkisi VERİLMEZ.** `ALTER SYSTEM KILL SESSION` bir *yazma*
işlemidir ve production source'un salt-okuma ilkesine aykırıdır. Kaçak bir `COUNT(*)`
zaten `timeout_sec` (`callTimeout`) ile kendiliğinden TIMEOUT'a düşer; gerçekten kill
gerekiyorsa DBA bunu araç dışında yapmalıdır.

---

## Statüler, Çıktı Eşiği ve Loglama

### Statüler (migration jargonu)

| Statü | Anlamı |
|-------|--------|
| `SYNC` ✅     | Obje/veri iki tarafta da birebir aynı (eşit) |
| `NOT-SYNC` ⚠️ | Obje iki tarafta da var ama yapısı/özellikleri farklı |
| `FAILED` ❌   | Obje source'ta var, target'ta eksik **veya** doğrulanamadı (timeout/hata) |
| `SKIPPED` ⏭️  | Kontrol atlandı (config gereği) |

`NOT-SYNC` sonuçları **granüler** basılır — farkın hangi öznitelikten geldiği tek tek görünür:

```
[NOT-SYNC]  COLUMN  HR.MUSTERI.BAKIYE
      tip       Source: NUMBER     Target: VARCHAR2
      nullable  Source: N          Target: Y
[FAILED]    TABLE   HR.SIPARIS_KALEM  — Target'ta tablo mevcut değil
```

### Tek eşik: `level`

Terminal tablosu, canlı ekran ve dosya logu **aynı** eşikle (`level`) süzülür. Böylece
yüzlerce `SYNC` satırı ekranı boğmaz; yalnızca eylem gerektiren sonuçlar görünür.

| `level` | Gösterilen / loglanan |
|---------|------------------------|
| `sync`     | her şey (SYNC + SKIPPED dahil) |
| `not-sync` | **NOT-SYNC + FAILED** (default) |
| `failed`   | yalnızca FAILED |

`config/validation.yaml`:

```yaml
output:
  level: not-sync     # sync | not-sync | failed
  extra_as: not-sync  # target'ta FAZLA objeler: not-sync (göster) | sync (gizle)
  log_file: ""        # boş = ./logs/dataval_<zaman>.log otomatik
  live: false         # canlı stderr akışını aç (dosya logu zaten her zaman açık)
```

veya CLI ile (YAML'ı override eder):

```bash
python run.py --level failed     # ekran + log: yalnızca FAILED (target'ta eksikler)
python run.py --level sync        # her şey (tüm SYNC dahil)
python run.py --debug             # canlı stderr akışı (aynı eşikle)
```

> Dosya logu **HER ZAMAN** açıktır; zaman damgalı `./logs/dataval_<zaman>.log` üretilir ve
> seçilen `level` eşiğiyle süzülür (`🗒️  Log: …  (eşik: not-sync)`). Sadece sorunları görmek
> için `level: failed`, her şeyi görmek için `level: sync` kullan.

> **Hedefte fazla objeler** (kaynakta yok, hedefte var) `extra_as` ile yönetilir: `not-sync`
> (default, göster) veya `sync` (gizle — migration'da hedef fazlalığı çoğu zaman zararsızdır).

**Çıktı:**
- **Ekran (stderr, --debug):** `· [constraints] HR.ORDERS  ❌ FAILED  PK constraint target'ta eksik`
- **Log dosyası:** `2026-06-11 18:45:00  [constraints] HR.ORDERS source=ID target=(yok) FAILED — PK constraint target'ta eksik`

Canlı akış ayrı bir akışta (stderr) olduğundan asıl rapor (stdout) bozulmaz; istersen
ayırabilirsin:

```bash
python run.py --debug 2> debug_ekran.log    # ekran akışını ayrı dosyaya
```

> `logs/` ve `*.log` `.gitignore`'da olduğundan log dosyaları commit edilmez.

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
| `FAILED` (timeout notlu) | büyük tabloda exact sayım | `--skip-tables` / `--count-mode sample` |

## Roadmap

- [x] **Native DDL üretimi (v0.7.0)** — tüm tipler `DBMS_METADATA`'sız üretilir (11g `ORA-03113`
  yok); INDEX ve CONSTRAINT üretimi eklendi.
- [x] **Grants doğrulama modülü (v0.8.0)** — source↔target object privilege karşılaştırması;
  `DBA_TAB_PRIVS` tercih + `ALL_TAB_PRIVS` fallback.
- [x] **Semantic constraint eşleştirme (v0.9.0)** — isim yerine kolon+tip bazlı; pseudo-PK
  (UNIQUE+NOT NULL) tespiti; `constraint_types` filtresi; conflict-safe DDL (`USING INDEX`).
- [x] **User & Grant izolasyon modülü (v0.10.0)** — instance-wide kullanıcı + sistem/rol/object
  yetki diff; dinamik sistem-user bypass; dry-run/yorumlu `CREATE USER` üretimi (parola loglanmaz).
- [x] **Global user akışı + parola senkronu + Execution Isolation (v0.11.0)** — users modülü
  şema-bağımsız global faza taşındı (`--modules users` → diğer modüller atlanır); opt-in
  `password_sync` ile 11g→19c `IDENTIFIED BY VALUES` üretimi + parola-farkı NOT-SYNC (maskeli).
- [ ] **Auto-Sync / Remediation** — `NOT-SYNC` için hedefe yönelik `ALTER` (granüler `diffs`'ten);
  dry-run default, yalnızca target'a yazar (source read-only). (`NOT-SYNC` sequence ALTER mevcut.)
- [ ] PostgreSQL desteği
- [ ] JSON çıktı modu (--output json)
- [ ] CI/CD entegrasyonu için exit code yönetimi

---

## Lisans

MIT
