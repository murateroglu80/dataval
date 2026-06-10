# Oracle 11g → 19c Migration — Bilinen Sorunlar ve dataval Davranışı

Bu belge, 11g'den 19c'ye şema taşımalarında sık karşılaşılan farkları ve `dataval`'in
her birini nasıl ele aldığını listeler. Amaç: hangi farkların gerçek bir sorun (FAIL),
hangilerinin beklenen/zararsız (WARNING) olduğunu net göstermek.

---

## 1. Thin mode 11g'yi desteklemez (DPY-3010)

python-oracledb **thin mode** yalnızca Oracle 12.1+ ile çalışır. Oracle 11.2.0.4 source'a
thin mode'da bağlanmak `DPY-3010: connections to this database server version are not
supported` hatası verir.

**Çözüm:** `connections.yaml` → top-level `oracle_client.mode: thick` + Oracle Instant
Client. Thick mode process-global olduğundan her iki bağlantı da (11g source ve 19c
target) thick üzerinden çalışır; 19c thick'i tam destekler.

```yaml
oracle_client:
  mode: thick
  lib_dir: "C:/oracle/instantclient_21_9"   # boş = sistem PATH
```

---

## 2. Data dictionary LONG kolonları → ORA-00932

11g'de bazı veri sözlüğü kolonları **LONG** tipindedir:

| View | Kolon | Not |
|------|-------|-----|
| `ALL_CONSTRAINTS` | `SEARCH_CONDITION` | CHECK koşulu. 12c+'da `SEARCH_CONDITION_VC` (VARCHAR2) eklendi; 11g'de yalnızca LONG var. |
| `ALL_TAB_COLUMNS` | `DATA_DEFAULT` | Kolon default ifadesi. |
| `ALL_TRIGGERS` | `TRIGGER_BODY` | Trigger gövdesi. |
| `ALL_VIEWS` | `TEXT` | View tanımı. |

**Tuzak:** Bir LONG kolon, aynı SELECT içinde **scalar subquery / LISTAGG / aggregate /
ORDER BY** ile birlikte seçilemez; `TO_CHAR`, `SUBSTR`, `INSTR` gibi fonksiyonlara da
sokulamaz → `ORA-00932: inconsistent datatypes: expected CHAR got LONG`.

**dataval deseni:** LONG kolonu **sade, izole bir sorguda** çek — başka kolon dönüşümü,
subquery, aggregate veya ORDER BY olmadan — ve sonucu Python tarafında ana sonuçla
`constraint_name` üzerinden eşle. `validator/modules/tables.py` içindeki
`SQL_CHECK_CONDITIONS` bu deseni uygular.

> `ALL_TAB_COLUMNS.DATA_DEFAULT` (`SQL_COLUMNS`) zaten tek tablodan, subquery/sort
> olmadan çekildiği için sorun çıkarmaz; dokunulmadı.

---

## 3. CHECK koşulu metni sürümler arasında biçim değiştirir

Aynı CHECK koşulu 11g ve 19c'de farklı saklanabilir: identifier tırnakları (`"AGE">0`
vs `AGE>0`), boşluk, harf büyüklüğü. Ham karşılaştırma **yanlış FAIL** üretir.

**dataval:** `_normalize_condition()` ile tırnakları kaldırır, boşlukları tekiller ve
büyük harfe çevirir → yalnızca anlamlı farklar raporlanır.

---

## 4. NOT NULL kısıtları SYS_C check olarak saklanır

11g'de `NOT NULL`, sistem-isimli (`SYS_C00xxx`) bir CHECK constraint olarak tutulur.
İsimler iki tarafta farklı üretildiği için bunlar gürültüdür.

**dataval:** Constraint sorguları `constraint_name NOT LIKE 'SYS_%'` ile bunları eler.
Kolon NULL'luğu ayrıca `tables` modülünde `nullable` karşılaştırmasıyla kontrol edilir.

---

## 5. BASICFILE → SECUREFILE LOB storage

19c'de LOB'lar genelde SECUREFILE olur; 11g BASICFILE olabilir. Bu bir storage farkıdır,
veri/şema uyumsuzluğu değildir.

**dataval:** `ignore_differences.lob_storage: true` (varsayılan) ile CLOB/BLOB/NCLOB tip
farkları WARNING'e indirilir, FAIL üretmez.

---

## 6. SEGMENT CREATION DEFERRED → boş tablo / NULL num_rows

11g `deferred segment creation` ile hiç satır eklenmemiş tabloların segmenti olmaz;
`ALL_TABLES.NUM_ROWS` NULL ya da bayat olabilir.

**dataval etkisi:** `row_count` `stats`/`auto` modunda NUM_ROWS'a dayanır; segment
oluşmamış/bayat istatistikli tablolarda sayım yanıltıcı olabilir. Bu tablolar için
`exact` mod veya target'ta istatistik yenileme önerilir (bkz. madde 9).

---

## 7. Case-sensitive parolalar (sec_case_sensitive_logon)

11g ile 19c arasında `SEC_CASE_SENSITIVE_LOGON` davranışı farklı olabilir; 11g'de
büyük/küçük harf duyarsız çalışan bir parola 19c'de duyarlı hale gelir. Bağlantı
hatalarında parolanın tam olarak doğru harf büyüklüğünde verildiğini doğrulayın.

---

## 8. DBMS_METADATA storage/tablespace farkları → TABLE/INDEX üretilmez

`DBMS_METADATA.GET_DDL` çıktısı TABLE ve INDEX için TABLESPACE, STORAGE, segment
özelliklerini içerir; bunlar 11g→19c arasında neredeyse her zaman farklıdır ve manuel
karar gerektirir.

**dataval:** `--generate-missing` TABLE ve INDEX'i **kasıtlı olarak üretmez**. Yalnızca
SEQUENCE, FUNCTION, PROCEDURE, PACKAGE(+BODY), TRIGGER, TYPE(+BODY), SYNONYM, GRANT üretir.

---

## 9. Bayat optimizer istatistikleri → stats-mode sayım sapması

`stats` modu `NUM_ROWS`'a güvenir; istatistik bayatsa sayı gerçeği yansıtmaz.
`--refresh-stats` ile yenilenebilir — **ancak source production'dır.**

**dataval:** Source **read-only** korumalıdır (varsayılan). `--refresh-stats` verilse
bile source'a `DBMS_STATS` **gönderilmez**; yalnızca target yenilenir ve sonuca
"source read-only — istatistik yenilenmedi" WARNING'i eklenir. Source'da taze sayı
gerekiyorsa `exact` mod kullanın (salt-okuma `SELECT COUNT(*)`).

---

## 10. NLS / karakter seti farkları

Source ve target farklı karakter setlerindeyse (ör. WE8ISO8859P9 → AL32UTF8) bazı
VARCHAR2 kolonlarının byte uzunlukları değişebilir (semantics: BYTE vs CHAR). Uzunluk
farkları `tables` modülünde tip farkı olarak görünebilir; gerçek bir uyumsuzluk mu yoksa
charset kaynaklı mı olduğunu değerlendirin.

---

## 11. Migration sonrası INVALID objeler

Taşıma sonrası bağımlılıklar nedeniyle paketler/trigger'lar INVALID kalabilir. 19c'de
`UTL_RECOMP.RECOMP_PARALLEL` veya `@?/rdbms/admin/utlrp.sql` ile recompile gerekir.

**dataval:** `code_objects` modülü DDL hash'i karşılaştırır; INVALID objeler ayrıca
`--generate-missing` sırasında `include_invalid: false` (varsayılan) ile atlanır + uyarı.

---

## 12. Sequence LAST_NUMBER / cache boşlukları

Sequence'ler taşındığında `LAST_NUMBER` cache nedeniyle ileri atlamış olabilir; küçük
ileri sapmalar normaldir.

**dataval:** `sequences` modülü INCREMENT_BY/MIN/MAX/CACHE/CYCLE'ı karşılaştırır ve
LAST_NUMBER için tolerans uygular. `--generate-missing` SEQUENCE'leri mevcut
LAST_NUMBER'ı koruyacak şekilde `START WITH <değer>` ile üretir.
