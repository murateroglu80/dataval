# Değişiklik Günlüğü

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenir.

Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) temel alınarak tutulur
ve proje [Semantic Versioning](https://semver.org/lang/tr/) kurallarını izler.

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

[0.3.2]: https://github.com/murateroglu80/dataval/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/murateroglu80/dataval/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/murateroglu80/dataval/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/murateroglu80/dataval/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/murateroglu80/dataval/releases/tag/v0.1.0
