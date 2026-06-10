# Değişiklik Günlüğü

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenir.

Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) temel alınarak tutulur
ve proje [Semantic Versioning](https://semver.org/lang/tr/) kurallarını izler.

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

[0.5.0]: https://github.com/murateroglu80/dataval/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/murateroglu80/dataval/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/murateroglu80/dataval/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/murateroglu80/dataval/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/murateroglu80/dataval/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/murateroglu80/dataval/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/murateroglu80/dataval/releases/tag/v0.1.0
