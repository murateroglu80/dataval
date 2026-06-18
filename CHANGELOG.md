# Değişiklik Günlüğü

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenir.

Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) temel alınarak tutulur
ve proje [Semantic Versioning](https://semver.org/lang/tr/) kurallarını izler.

## [0.12.0] - 2026-06-18

### Eklenenler
- **NOT-SYNC kolon remediation — `ALTER TABLE … MODIFY` üretimi.** `tables` modülünün
  tespit ettiği veri tipi/boyut ve nullable farkları artık hizalayıcı DDL'e dönüşüyor.
  Yeni `<TARGET>_TABLE_ALTER.sql` dosyası (NOT-SYNC sequence/constraint akışıyla simetrik):
  - **Tip/boyut + nullable** farkları için tek MODIFY ifadesi; tablo başına gruplanır.
    Default-değer farkı raporlanır ama MODIFY üretilmez (kapsam dışı).
  - **Risk yönetimi:** güvenli yön (genişletme, `NOT NULL→NULL` gevşetme) **çalıştırılabilir**;
    riskli yön (boyut **küçültme**, **base-tip** değişimi, `NULL→NOT NULL` sıkılaştırma) `--` ile
    **yorumlanır** + neden (ORA-01441/01439/01440/02296). Bir kolonda herhangi bir parça riskliyse
    tüm ifade yorumlanır (muhafazakâr).
  - **Yeni SQL yok** — tüm veri validation sonuçlarından (`ValidationResult`) gelir; source
    read-only korunur.
  - **Execution Guard:** yeni `generate_scripts.types.TABLE` anahtarı (default `true`) + `tables`
    modülü açık olmalı.
- **CHAR/BYTE notu:** tip imzası CHAR/BYTE semantiğini taşımaz (`_normalize_type` sınırı) — dosya
  başı notu uygulamadan önce doğrulama uyarısı verir.

## [0.11.1] - 2026-06-18

### Düzeltmeler
- **Global-only koşuda config çökmesi.** `validation.yaml`'de boş `schemas:` bloğu (boş
  `source:`/`target:`) `None` üretip `s["source"].upper()`'da `'NoneType' object has no
  attribute 'upper'` hatası veriyordu. Şema parse'ı artık tamamen boş placeholder satırlarını
  **atlıyor** (yalnız `users` gibi global modüllerle şema gerektirmeyen koşuya izin); yarım dolu
  mapping (yalnız source ya da yalnız target) için net hata verir. Boş schemas için hard `raise`
  kaldırıldı.
- **Scoped modül + boş schemas uyarısı.** Şema-bağımlı bir modül (tables/indexes/…) aktifken hiç
  schema mapping yoksa, sessizce atlamak yerine `run.py` artık sarı uyarı basar.

## [0.11.0] - 2026-06-18

### Eklenenler
- **Şema-bağımsız global user akışı + Execution Isolation.** `users` modülü artık schema
  döngüsünden **önce, döngü dışında bir kez** koşar (`run(src_conn, tgt_conn, cfg)` — `mapping`
  bağımlılığı kaldırıldı); sonuçların schema etiketi target DB adıdır (`v$database`, erişilemezse
  `INSTANCE`). Modüller `GLOBAL_MODULES = {"users"}` ile global/scoped olarak ikiye ayrılır:
  `--modules users` verildiğinde `scoped_active` boş kalır ve **schema döngüsüne hiç girilmez** —
  `tables`/`indexes`/… flag'leri `true` olsa bile o akışta hiçbir tablo/index taranmaz.
- **Cross-version parola hash senkronu (`modules.password_sync`, opt-in, default `false`).**
  - Yeni `users.fetch_user_verifier` / `_verifier_source`: sürüm-duyarlı verifier okuma —
    `SYS.USER$` (`password`=10g DES, `spare4`=SHA verifier) → erişim yoksa `DBA_USERS.PASSWORD`
    (yalnız 11g) → ikisi de yoksa placeholder. Probe `ORA-00942/01031` yakalayıp düşer.
  - **Parola-farkı tespiti:** ortak (iki tarafta var) user'larda verifier farklıysa `USER` için
    `NOT-SYNC`. Hash değeri rapora/loga **asla** yazılmaz — yalnız maskeli (ilk 6 karakter + `…`).
  - **Çalıştırılabilir USER DDL'i:** `password_sync=true` + generate ile `<DB>_USER.sql` artık
    `CREATE USER … IDENTIFIED BY VALUES '<verifier>'` (11g→19c taşıma) + gerçek
    `DEFAULT TABLESPACE`/`TEMPORARY`/`PROFILE` (`DBA_USERS`) + parola-farkı için
    `ALTER USER … IDENTIFIED BY VALUES …` + reconstructed `GRANT` üretir. Dosya başında **HASSAS**
    uyarı bloğu (izinleri kısıtla / commit'leme / uygulama sonrası sil; 19c
    `SEC_CASE_SENSITIVE_LOGON` / `SQLNET.ALLOWED_LOGON_VERSION_SERVER` notu).
  - `password_sync=false` (default) → **v0.10.0 ile bire bir aynı** güvenli dry-run: hash hiç
    okunmaz, tüm satırlar yorumdur, `<<PAROLAYI_BELIRLEYIN>>` placeholder.
- **USER üretimi global'e taşındı.** `ddl_generator.generate_user_script` (tek `<DB>_USER.sql`)
  schema döngüsünden önce bir kez üretilir; per-mapping `_write_user_file` çağrısı `generate_scripts`
  içinden kaldırıldı. Scoped generate, user-family (`USER/SYS_PRIV/ROLE/OBJ_PRIV`) tiplerini toplamaz.

### Güvenlik
- Parola verifier (hash) **hiçbir koşulda** konsola/log dosyasına yazılmaz; yalnız `password_sync=true`
  iken üretilen `.sql` dosyasına gider, raporda maskelenir. Source read-only korunur (tüm okuma SELECT).
  `password_sync=false` (default) hash yüzeyini tamamen kapalı tutar.

## [0.10.0] - 2026-06-18

### Eklenenler
- **Users (kullanıcı & yetki izolasyon) modülü.** Yeni `validator/modules/users.py` —
  **instance-wide** (schema-scoped `grants.py`'den bağımsız). Karşılaştırır:
  - **User existence + öznitelik:** app kullanıcılarının varlığı (src∖tgt → `FAILED`,
    tgt∖src → extra) ve `account_status` / `default_tablespace` / `profile` farkı → `NOT-SYNC`.
  - **Sistem yetkileri** (`DBA_SYS_PRIVS`), **rol grant'ları** (`DBA_ROLE_PRIVS`,
    `admin_option`/`default_role` diff) ve **grantee-merkezli object grant'lar** (`DBA_TAB_PRIVS`,
    `ALL_TAB_PRIVS` fallback): küme farkı → `FAILED`, öznitelik farkı → `NOT-SYNC`, birebir → `SYNC`.
  - `object_type ∈ {USER, SYS_PRIV, ROLE, OBJ_PRIV}`. `modules.users` (veya `--modules users`)
    ile açılır (opt-in, default kapalı). Instance-wide olduğundan şema döngüsünde **bir kez** çalışır.
- **Dinamik sistem-kullanıcı filtresi (`_non_system_users`).** 12c+/19c'de
  `DBA_USERS.oracle_maintained = 'N'`; 11g'de (kolon yok → ORA-00904) curated statik liste +
  prefix (`APEX_`, `FLOWS_`, `SPATIAL_`, `C##`, …) fallback; `DBA_USERS` erişimi yoksa (ORA-00942)
  `ALL_USERS`'a düşer. SYS/SYSTEM/XDB/APEX_* gibi altyapı user'ları hiçbir sorguda görünmez.
- **Dry-run / yorumlu `CREATE USER` üretimi.** FAILED user + yetkiler için advisory `<TGT>_USER.sql`:
  `CREATE USER … IDENTIFIED BY "<<PAROLAYI_BELIRLEYIN>>"` iskeleti + reconstructed `GRANT` ifadeleri.
  **Tüm satırlar yorumdur** (bilinçli açılmadan çalışmaz) ve **parola hash'i okunmaz/loglanmaz**
  (11g `DBA_USERS.PASSWORD` ↔ 19c `USER$.SPARE4` taşınabilir değil). Üretim Execution Guard'a tabidir
  (`modules.users=false` ⇒ dosya yok).

## [0.9.1] - 2026-06-18

### Düzeltilenler
- **`modules.grants: false` baypas ediliyordu — GRANT scriptleri yine de üretiliyordu.**
  `--generate-missing` akışı `modules.grants` yerine ayrı bir bayrağı
  (`generate_scripts.types.GRANT`, varsayılan açık) okuyordu ve grants modülü kapalıyken bile
  `<TGT>_GRANT.sql` üretip ekrana basıyordu. Artık üretim **Execution Guard** ile sahibi modüle
  bağlı: `modules.grants=false` ⇒ grant fetch / diff / stdout / DDL %100 engellenir.
- **Filtresiz GRANT dökümü (SYNC olanlar dahil binlerce grant).** `_get_grant_statements` tüm
  source grant'larını koşulsuz döküyordu (örn. 6463 satır, target'ta zaten var olanlar dahil).
  Artık yalnız **eksik** (source'ta var / target'ta yok) grant'lar yeni
  `grants.missing_grant_rows` diff'i ile üretilir — `only_missing` / FAILED-only sözleşmesi GRANT
  için de geçerli. Target bağlantısı yoksa diff yapılamayacağından üretim güvenli şekilde atlanır.

### Değiştirilenler
- **Merkezi Execution Guard (üretim).** `generate_scripts` artık `enabled_modules` alır; üretim
  tipleri sahibi modüle bağlanır (`TYPE_MODULE`: `GRANT→grants`, `CONSTRAINT→constraints`,
  `SEQUENCE→sequences`, `INDEX→indexes`). Sahibi kapalı modül olan bir tip hiç işlenmez (PL/SQL ve
  SYNONYM eksikliği `inventory`'den geldiği için kapsam dışı bırakıldı).

## [0.9.0] - 2026-06-18

### Eklenenler
- **Pseudo-constraint farkındalığı (semantic constraint validation).** Target tekliği bir
  **UNIQUE INDEX (+ NOT NULL)** ile sağlanıyorsa (legacy pseudo-PK/UK), ilgili constraint artık
  yanlışlıkla `FAILED` değil **NOT-SYNC** olarak raporlanır (`enforce: CONSTRAINT → UNIQUE INDEX`).
  Target unique index'ler + `NOT NULL` kolon bilgisinden türetilir.
- **`constraint_types` filtre parametresi.** `modules.constraint_types: ALL` veya alt küme
  (`[PK, UK, FK, CHECK]`) — hem doğrulama hem DDL üretimi tek kaynaktan bu filtreye uyar.
- **Conflict-safe CONSTRAINT DDL üretimi.** Eksik PK/UK için DDL üretmeden önce target **probe**
  edilir (yalnız SELECT): aynı kolonlarda covering unique index varsa
  `ALTER TABLE ... ADD CONSTRAINT ... USING INDEX <ix>` üretilir (**ORA-02261 önlenir**); aynı
  kolonda zaten constraint varsa DDL yerine `-- Zaten mevcut … atlandı` yorumu yazılır.

### Değiştirilenler
- **Constraint eşleştirmesi tamamen isim-bağımsız + granüler.** Anahtar `(tip, kolon kümesi
  [, ref tablo / CHECK koşulu])`; eşleşen çiftlerde ad / durum / `delete_rule` / kolon sırası
  farkları granüler `diffs` ile **NOT-SYNC** raporlanır, birebir eşleşme **SYNC**.
- **`SYS_%` isimli constraint'ler artık atlanmıyor** (kolon-bazlı eşleştirmede ad önemsiz);
  ad farkı yalnız kullanıcı-adlı constraint'lerde raporlanır (iki taraf da `SYS_` ise gürültü yok).

## [0.8.0] - 2026-06-18

### Eklenenler
- **Grants (object privilege) doğrulama modülü.** Yeni `validator/modules/grants.py` source ↔ target
  object grant'larını `(grantee, obje, privilege)` anahtarıyla karşılaştırır:
  source'ta var/target'ta yok → **FAILED**; target'ta fazla → **extra** (`extra_as`'a göre
  NOT-SYNC/SYNC); ortak ama `GRANTABLE`/`HIERARCHY` farkı → **NOT-SYNC**; birebir → **SYNC**.
  `modules.grants` (veya `--modules grants`) ile açılır (opt-in).

### Düzeltilenler
- **Grant verisi eksik çekiliyordu (`ALL_TAB_PRIVS` görünürlük sınırı).** Yeni
  `fetch_object_grants` önce **`DBA_TAB_PRIVS`** (tam görünürlük) dener, erişim yoksa
  (`ORA-00942`/`ORA-01031`) **`ALL_TAB_PRIVS`**'e düşer. Hem yeni doğrulama modülü hem
  `ddl_generator` grant üretimi bu tek fetch katmanını paylaşır → başka kullanıcıların verdiği
  grant'lar artık kaçmıyor.
- **`modules.grants` orphan flag'i decouple edildi.** Bayrak `config_loader`'da parse ediliyordu
  ama hiçbir router tarafından tüketilmiyordu (işlevsizdi); artık grants doğrulama modülünü
  yönlendiriyor. Sequence/diğer üretim akışlarının grant bayrağına gerçek/algılanan bir bağımlılığı
  yok — modüler izolasyon `docs/refactor-grants-decoupling-plan.md`'de belgelendi.

## [0.7.0] - 2026-06-18

### Eklenenler
- **Native DDL üretimi tüm obje tiplerine yayıldı — `DBMS_METADATA` tamamen kaldırıldı.** v0.6.2'de
  yalnız SEQUENCE native idi; artık `FUNCTION/PROCEDURE/PACKAGE/PACKAGE BODY/TYPE/TYPE BODY` →
  `ALL_SOURCE`'tan birebir (`CREATE OR REPLACE` + kaynak metni), `TRIGGER` → `ALL_TRIGGERS`
  (DESCRIPTION + TRIGGER_BODY; DISABLED ise `ALTER TRIGGER ... DISABLE`), `SYNONYM` →
  `ALL_SYNONYMS`. Hiçbir tip `DBMS_METADATA` kullanmıyor → Oracle 11g `ORA-03113` riski **tamamen**
  ortadan kalktı (yalnızca by-pass değil; üretici %100 DBMS_METADATA'sız).
- **INDEX script üretimi (yeni tip).** Eksik index'ler `ALL_INDEXES` + `ALL_IND_COLUMNS`'tan native
  üretiliyor; UNIQUE/BITMAP, `DESC` kolonlar ve function-based index ifadeleri
  (`ALL_IND_EXPRESSIONS`) destekleniyor.
- **CONSTRAINT script üretimi (yeni tip).** Eksik PK/UK/FK/CHECK → `ALTER TABLE ADD CONSTRAINT`
  (`<target>_CONSTRAINT.sql`); PK/UK önce, FK sonra (bağımlılık sırası). FK referans tablo/kolonları
  ve `DELETE_RULE` (`ON DELETE CASCADE/SET NULL`) çözülüyor, referans sahibi `replace_schema` ile
  repoint ediliyor. Eksik constraint'ler `constraints` modülünün yapısal imzasıyla (ad değil)
  eşleştiriliyor.

### Değiştirilenler
- **Şema niteleme güvenli hale getirildi.** PL/SQL DDL'inde blunt global string-replace yerine yalnız
  `CREATE` başlığına hedef şema enjekte ediliyor + nitelikli (`SOURCE.`) çapraz referanslar repoint
  ediliyor; gövde metni bozulmuyor.
- **`generate_scripts.types` varsayılanları tek kaynağa indirildi.** `config_loader`'daki ikinci
  kopya kaldırıldı (drift kaynağıydı); YAML override artık deep-merge, büyük/küçük harf duyarsız ve
  bilinmeyen anahtarı sessizce düşürmüyor. `INDEX` ve `CONSTRAINT` tipleri eklendi (varsayılan açık).
- **INVALID kontrolü yalnız PL/SQL tiplerine uygulanıyor** (INDEX/CONSTRAINT/SYNONYM için
  `ALL_OBJECTS.STATUS` semantiği farklı olduğundan atlanıyor).

### Kaldırılanlar
- Ölü `DBMS_METADATA` kod yolu: `_get_ddl_raw`, `METADATA_TYPE_MAP` ve blunt `_replace_schema`.

## [0.6.2] - 2026-06-11

### Düzeltilenler
- **DDL script üretimi Oracle 11g'de oturumu çökertiyordu (`ORA-03113`).** `DBMS_METADATA.GET_DDL`'in
  `SELECT ... FROM DUAL` içinde bind değişkenleriyle çağrılması, 11g'de bilinen bir
  `DBMS_METADATA`-in-SQL hatasıyla sunucu oturumunu öldürüyordu (`DPY-4011 / DPI-1080 / ORA-03113`).
  **Sequence DDL artık `DBMS_METADATA` olmadan native (ALL_SEQUENCES'tan) üretiliyor** → ORA-03113
  tamamen by-pass; üstelik MIN/MAX/INCREMENT/CACHE/CYCLE/ORDER ve gerçek `LAST_NUMBER` (`START WITH`)
  birebir korunuyor.
- **Diğer obje tipleri için zarif düşüş.** `_get_ddl_raw` artık ölümcül kopma hatalarını
  (`ORA-03113/03114`, `DPY-4011`, `DPI-1080`) yakalayıp ilgili objeyi atlıyor (`-- !! DDL alınamadı`)
  — tek bir obje tüm CLI'yi ham traceback ile çökertmiyor.

### Eklenenler
- **NOT-SYNC sequence remediation (ALTER).** `--generate-scripts` artık yalnızca eksik (FAILED)
  objeleri değil, **NOT-SYNC sequence'leri** de ele alıyor: source değerlerine hizalayan
  non-destructive `ALTER SEQUENCE` (+ target 18c+ için `RESTART START WITH <last_number>`) üretip
  ayrı `<target>_SEQUENCE_ALTER.sql` dosyasına yazıyor. Yıkıcı `DROP + CREATE` eşdeğeri yalnızca
  yorum satırında yedek olarak bulunuyor (kazara çalışma riski yok); grant ve bağımlılıklar korunur.

## [0.6.1] - 2026-06-11

### Düzeltilenler
- **DDL script üretimi çöküyordu (`KeyError: 'STATUS'`).** `validator/modules/ddl_generator.py`
  satır sözlüklerine büyük harf anahtarlarla (`row["STATUS"]`, `"DDL"`, `"LAST_NUMBER"`,
  `"GRANTEE"` …) erişiyordu; oysa `connection.fetch_all` tüm kolon adlarını küçük harfe indirir.
  `_get_object_status` köşeli-parantez erişiminde `KeyError` fırlatıp `--generate-scripts`
  akışını kırıyordu. Aynı kök neden `.get()` kullanan diğer erişimleri **sessizce** bozuyordu:
  `_get_ddl_raw` her objede `None` dönüyor (SEQUENCE dışında hiçbir tip için DDL üretilmiyordu),
  sequence `START WITH` gerçek `last_number` ile düzeltilemiyor, GRANT üretimi başarısız oluyordu.
  Tüm anahtar erişimleri `fetch_all` sözleşmesine (küçük harf) uyumlu hale getirildi.

## [0.6.0] - 2026-06-11

> **Kırıcı (breaking):** Statü adları ve çıktı eşiği değişti; `INFO/WARNING/ERROR` log
> seviyeleri kaldırıldı. `debug:` config bloğu yerini `output:` bloğuna bıraktı.

### Değişenler
- **Migration statü jargonu.** `Status` enum'u üç-değerli migration modeline taşındı:
  `SYNC` (eşit), `NOT-SYNC` (iki tarafta var ama farklı), `FAILED` (target'ta eksik veya
  doğrulanamadı) + operasyonel `SKIPPED`. Eski `PASS→SYNC`; eski `FAIL` ikiye ayrıldı
  (eksik→`FAILED`, farklı→`NOT-SYNC`); eski `WARNING` (kolon/constraint farkı)→`NOT-SYNC`;
  `TIMEOUT`/`ERROR`→`FAILED` (ayrıntı not'ta). Tüm modüller yeni jargona geçirildi.
- **`INFO/WARNING/ERROR` seviye katmanı kaldırıldı → `sync|not-sync|failed` eşiği.** Tek
  `level` eşiği artık **hem terminal tablosunu, hem canlı ekranı, hem dosya logunu** birlikte
  süzer (sıra: `sync` < `not-sync` < `failed`; default `not-sync`). CLI: `--log-level` yerine
  `--level`. Config: `debug.log_level` yerine `output.level`. Böylece yüzlerce `SYNC` satırı
  ekranı/logu boğmaz; yalnızca eylem gerektiren `NOT-SYNC`/`FAILED` görünür.
- **Merkezi Reporter (`validator/reporter.py`).** Terminal tabloları (run.py), dosya logu ve
  canlı ekran (debug.py) tek bir `Reporter` sınıfında toplandı; `register_observer` ile sonuç
  choke-point'ine bağlanır. `validator/debug.py` ince bir `dbg` köprüsüne indirildi.
- **Granüler `NOT-SYNC` çıktısı.** Farklar artık `ValidationResult.diffs` (öznitelik, source,
  target) üçlüleriyle taşınır ve hiyerarşik basılır: `tip  Source: NUMBER  Target: VARCHAR2`
  (tables/indexes/sequences).

### Eklenenler
- **`output.extra_as`** (`not-sync`/`sync`, default `not-sync`): target'ta FAZLA (kaynakta
  yok) objelerin statüsünü belirler — göster ya da gizle.
- **`modules.include_temp_tables`** (default `false`): Global Temporary Table'ları
  (`temporary='Y'`) kapsama dahil eder. Default'ta `tables` ve `constraints` GTT'leri atlar.

## [0.5.0] - 2026-06-10

### Düzeltilenler
- **Constraints config bypass:** `modules.constraints: false` yok sayılıyordu. Constraint
  (PK/UK/FK/CHECK) karşılaştırması `tables` modülüne gömülü ve **koşulsuz** çalışıyordu;
  `constraints` bayrağının router'da karşılığı yoktu. Constraint mantığı artık first-class
  bir `constraints` modülüne taşındı ve `modules.constraints` bayrağıyla (veya
  `--modules constraints` ile) yönetiliyor. `false` → hiç constraint kontrolü yapılmaz.
- **Ölü `grants` bayrağı:** `modules.grants: true` sessizce hiçbir şey yapmıyordu (runner'ı
  yoktu). Yanıltıcı router eşlemesi kaldırıldı; gerçek grants doğrulama modülü gelecek iştir.

### Değişenler
- **Loglama — her zaman açık dosya logu + seviye katmanı:** Artık her çalıştırmada (debug
  gerekmeden) eksiksiz bir log dosyası üretilir; kontrol edilen her obje (PASS dahil, tüm
  modüller) dosyaya yazılır — terminal raporunun kalıcı aynası. `--debug`/`debug.enabled`
  yalnızca canlı stderr akışını açar. Yeni `log_level` (INFO/WARNING/ERROR, CLI: `--log-level`)
  **yalnızca canlı ekran** ayrıntısını kısar; dosya logu daima eksiksiz kalır.

## [0.4.0] - 2026-06-10

### Eklenenler
- **Paralel tablo sayımı:** `exact`/`sample` `COUNT(*)` sayımları artık `parallel_workers`
  ile tablolar arası paralel çalışabilir. Source ve target için **ayrı `python-oracledb`
  bağlantı havuzu + ayrı `ThreadPoolExecutor`** kullanılır (havuz boyutu = worker sayısı).
- **Source (production) koruması:** `source_max_workers` ile source havuzu ayrıca
  sınırlanır (etkin = `min(parallel_workers, source_max_workers)`); target tam hızda sayılır.
- **SQL injection savunması:** tablo/şema adları SQL'e gömülmeden önce
  `^[A-Za-z][A-Za-z0-9_$#]*$` regex'iyle doğrulanır (`is_valid_identifier` / `safe_table_ref`);
  geçersiz ad sessizce atlanmaz, **ERROR** olarak raporlanır.
- CLI: `--parallel-workers`, `--source-workers`.

### Değişenler
- `callTimeout` her paralel sayım bağlantısında uygulanır → kilitli/iri tablo bir worker'ı
  süresiz bloklayamaz; tablo bazında `oracledb.DatabaseError`/timeout ayrıştırılıp
  **TIMEOUT**/**ERROR** olarak raporlanır, diğer tablolar etkilenmez.
- Genel özette artık **ERROR** (ve varsa TIMEOUT) görünür ve ERROR "sorun" sayılır —
  doğrulanamayan obje yanlışlıkla "TEMIZ" raporlanmaz. `parallel_workers: 1` (varsayılan)
  ile çıktı eski seri davranışla birebir aynıdır.

## [0.3.2] - 2026-06-10

### Eklenenler
- `CHANGELOG.md` — tüm sürümlerin değişiklik geçmişi tek dosyada toplandı.

## [0.3.1] - 2026-06-10

### Düzeltilenler
- **Cross-schema yetki sorunu (yanlış "TEMIZ" raporu):** `ALL_*` sözlük view'ları
  yalnızca yetki verilen objeleri gösterdiğinden, doğrulama kullanıcısı kaynak şemayı
  boş görüp tüm modülleri sıfır objeyle "TEMIZ" raporluyordu. README'deki "Gerekli
  Yetkiler" bölümü doğru `ANY` ayrıcalıklarıyla yeniden yazıldı (`CREATE SESSION`,
  `SELECT ANY TABLE`, `SELECT ANY SEQUENCE`, `EXECUTE ANY PROCEDURE`, `EXECUTE ANY TYPE`,
  `SELECT_CATALOG_ROLE`, `ANALYZE ANY`).
- **Sıfır-obje preflight kontrolü:** kaynak şemada görünür obje yoksa artık `FAIL` ile
  uyarı verir (şema adı ve yetkileri kontrol edin), sessizce "TEMIZ" demez.

## [0.3.0] - 2026-06-10

### Eklenenler
- **Debug mode:** çalışırken kontrol edilen her obje canlı olarak
  `ŞEMA.OBJE source=.. target=.. STATUS` biçiminde ekrana (stderr) ve zaman damgalı bir
  log dosyasına (`./logs/dataval_<zaman>.log`) yazılır.
- `validation.yaml` içinden `debug.enabled: true` ile açılır; `--debug/-d` CLI flag'i
  YAML değerini geçersiz kılabilir.
- Observer tabanlı tasarım (`ModuleSummary.add` tek choke-point): mevcut modüllerin
  karşılaştırma mantığına ve stdout raporuna dokunulmadı; debug kapalıyken davranış
  birebir aynıdır.

## [0.2.0] - 2026-06-10

### Eklenenler
- **Source read-only koruması:** kaynak (production) veritabanına hiçbir yazma —
  `DBMS_STATS` dahil — gönderilmez. `read_only` parametresi eklendi (source varsayılanı
  `true`, target `false`).
- **`oracle_client` yapılandırma bloğu:** `mode: thin|thick` ve `lib_dir`; eski
  `source.thick_mode` alanıyla geriye uyumlu.
- `--generate-missing`: target'ta eksik objeler için DDL script üretimi.
- `check_integrity.py`: null-byte ve syntax bütünlük kontrol aracı.
- 11g→19c migration ve troubleshooting dokümanları (`docs/`).

### Düzeltilenler
- **ORA-00932 / ORA-00997 (Oracle 11g LONG):** `ALL_CONSTRAINTS.SEARCH_CONDITION` (LONG)
  alanı scalar subquery / `LISTAGG` / `ORDER BY` ile aynı `SELECT` içinde kullanılamıyordu;
  check koşulları ayrı bir basit sorguya alınarak çözüldü.
- **Oracle 11g thick mode:** 11.2.0.4 thin modda `DPY-3010` verdiğinden Instant Client
  zorunlu; thick mode kalıcı yapılandırmaya taşındı.

## [0.1.0] - 2026-06-09

### Eklenenler
- İlk sürüm — Oracle 11g→19c şema migration doğrulama CLI aracı.
- Doğrulama modülleri: inventory, tables, indexes, constraints, sequences, row counts,
  code objects.
- Akıllı satır sayım stratejileri: `auto` / `exact` / `sample` / `stats` / `skip`.
- `rich` tabanlı terminal raporu ve modül-bazlı özet.

[0.9.0]: https://github.com/murateroglu80/dataval/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/murateroglu80/dataval/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/murateroglu80/dataval/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/murateroglu80/dataval/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/murateroglu80/dataval/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/murateroglu80/dataval/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/murateroglu80/dataval/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/murateroglu80/dataval/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/murateroglu80/dataval/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/murateroglu80/dataval/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/murateroglu80/dataval/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/murateroglu80/dataval/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/murateroglu80/dataval/releases/tag/v0.1.0
