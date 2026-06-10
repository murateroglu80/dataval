# dataval — Troubleshooting

Sık karşılaşılan hatalar ve çözümleri. Daha geniş 11g→19c farkları için bkz.
[migration-11g-to-19c.md](migration-11g-to-19c.md).

---

## DPY-3010: connections to this database server version are not supported

**Neden:** python-oracledb **thin mode** Oracle 11.2 source'u desteklemiyor.

**Çözüm:** `connections.yaml` → `oracle_client.mode: thick` yapın ve Oracle Instant
Client kurun.

```yaml
oracle_client:
  mode: thick
  lib_dir: "C:/oracle/instantclient_21_9"   # boş = sistem PATH
```

---

## ORA-00932: inconsistent datatypes: expected CHAR got LONG

**Neden:** 11g veri sözlüğündeki LONG kolon (ör. `ALL_CONSTRAINTS.SEARCH_CONDITION`)
bir scalar subquery / LISTAGG / ORDER BY içeren sorguda seçilmiş.

**Durum:** `tables` modülünde bu hata **giderildi** — CHECK koşulları ayrı, sade bir
sorguyla (`SQL_CHECK_CONDITIONS`) çekiliyor. Yeni bir sorgu eklerken LONG kolonları
asla subquery/aggregate/ORDER BY/fonksiyon ile aynı SELECT'e koymayın; izole edip
Python'da birleştirin. Ayrıntı: [migration-11g-to-19c.md](migration-11g-to-19c.md) §2.

---

## Thick mode başlatılamadı / DPI-1047: cannot locate Oracle Client library

**Neden:** Instant Client kurulu değil ya da PATH'te değil.

**Çözüm:**
- Instant Client'ı indirip açın.
- `oracle_client.lib_dir`'ı tam dizine ayarlayın (ör. `C:/oracle/instantclient_21_9`),
  veya dizini sistem PATH'ine ekleyip `lib_dir`'ı boş bırakın.
- Windows'ta Instant Client mimarisi (64-bit) Python ile aynı olmalı.

---

## PermissionError: Read-only baglantida yazma engellendi

**Neden:** Source bağlantısı read-only (varsayılan) ve bir kod yolu source'a yazmaya
çalıştı (ör. `DBMS_STATS`). Bu **kasıtlı bir korumadır** — source production'a yazılmaz.

**Çözüm:** Normalde hiçbir şey yapmanıza gerek yok; bu, source'u koruyan beklenen
davranıştır. Source'ta gerçekten istatistik yenilemeniz gerekiyorsa (önerilmez)
`connections.yaml`'da source altına `read_only: false` ekleyin.

---

## ORA-00942: table or view does not exist

**Neden:** `valuser` ilgili veri sözlüğü görünümlerine SELECT yetkisine sahip değil.

**Çözüm:** README → "Gerekli Yetkiler" bölümündeki GRANT script'ini çalıştırın
(`ALL_OBJECTS`, `ALL_TABLES`, `ALL_CONSTRAINTS`, `ALL_INDEXES`, `ALL_SEQUENCES`,
`DBMS_METADATA`, `DBMS_STATS`, …).

---

## ORA-01031: insufficient privileges

**Neden:** DDL üretimi için `DBMS_METADATA.GET_DDL` veya başka şema objelerine erişim
yetkisi yetersiz.

**Çözüm:** `valuser`'a `SELECT ANY DICTIONARY` veya ilgili `GRANT EXECUTE ON
DBMS_METADATA` ve hedef şema objeleri üzerinde gerekli yetkileri verin. SYSDBA gerekiyorsa
`connections.yaml`'da `sysdba: true`.

---

## ORA-03136 / sorgu timeout (TIMEOUT statüsü)

**Neden:** Büyük tabloda `exact` sayım `row_count.timeout_sec`'i aştı.

**Çözüm:** O tabloyu `--skip-tables AD` ile atlayın, `--count-mode sample` kullanın,
veya `row_count.overrides` ile tablo bazlı `stats`/`skip` tanımlayın.
