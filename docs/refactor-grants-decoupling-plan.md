# dataval — Hata Ayıklama & Refactor Planı: Grants + Modüler İzolasyon

> Durum: **PLAN** (uygulanmadı). Kapsam: (1) Sequence DDL ↔ Grants algılanan bağımlılığı,
> (2) Grant verisini eksik çekme, (3) modüller-arası izolasyon kuralları.

---

## Özet teşhis (kanıtlarla)

| # | Bulgu | Kanıt | Tür |
|---|-------|-------|-----|
| A | `modules.grants` **orphan flag** — hiçbir yerde tüketilmiyor | `config_loader.py:81,:220` parse ediliyor; `validator/modules/` altında **`grants.py` YOK**; `run.py:156-165` router `mc.grants`'ı okumuyor | Phantom coupling kaynağı |
| B | Tip varsayılanları **iki yerde** kopyalı → drift | `config_loader.py:115` (dataclass) **ve** eski `:316` (`_parse_generate_scripts`) | Mimari (bu turda düzeltildi) |
| C | `ALL_TAB_PRIVS` görünürlük sınırı | `ddl_generator._get_grant_statements` | Grant fetch eksikliği |
| D | Grant tarafında **karşılaştırma yok** (yalnız source'tan generation) | `_get_grant_statements` source okur, SYNC/NOT-SYNC üretmez | Eksik validation modülü |

**Sonuç:** Problem #1'de kodda gerçek bir Sequence→Grant bağımlılığı **yok**. Sequence DDL üretimi
üç bağımsız koşula bağlı: `modules.sequences=true` (FAILED sequence üretilsin) → `generate_scripts.enabled`
→ `generate_scripts.types.SEQUENCE=true`. `modules.grants` bunlardan hiçbirine dokunmaz; ama **işlevsiz
bir bayrak** olduğu için kullanıcıyı "grants kapatınca bozuldu" yanlış ilişkilendirmesine itiyor.

---

## Bölüm 1 — Bağımlılığın İzolasyonu (Decoupling)

### 1.1 Analiz adımları (kalıcı doğrulama)
1. **Orphan kanıtı:** `grep -rn "mc\.grants\|modules\.grants\|\.grants\b" validator/ run.py` → tüketim
   noktası beklenmiyor. Çıktı boşsa flag ölüdür.
2. **İzolasyon testi (DB'siz, monkeypatch):** yalnız bir FAILED sequence içeren sahte summary ile
   `_run_generate_scripts` çağır; `types={SEQUENCE:true, GRANT:false}`. Beklenen: `<tgt>_SEQUENCE.sql`
   üretilir. Bu test coupling'i kalıcı olarak çürütür (regresyon koruması).
3. **Erken-return haritası:** `generate_scripts` ve `_run_generate_scripts` içindeki tüm
   `continue`/erken-`return`/`if cfg.types.get(...)` noktalarını çıkar; her blok yalnız **kendi**
   tipinin bayrağına bakıyor mu doğrula (özellikle global early-return koşulu).

### 1.2 Mimari değişiklik
- **`modules.grants`'a işlev ver _veya_ kaldır.** Öneri: Bölüm 2'deki **grants validation modülünü**
  ekle ve `mc.grants` onu yönlendirsin. Böylece flag gerçek bir router dalına bağlanır.
- **Validation ≠ Generation ayrımını koru:** `modules.grants` (doğrulama) ile
  `generate_scripts.types.GRANT` (üretim) iki ayrı eksen kalır; biri diğerini gate'lemez.
- **Tek-kaynak tip varsayılanı** (bu turda yapıldı): `_parse_generate_scripts` artık
  `GenerateScriptsConfig().types`'ı deep-merge ediyor; kopya yok, case-insensitive, bilinmeyen
  anahtar düşmüyor.

---

## Bölüm 2 — Grants Sorgu (Fetch) Mantığının Revizyonu

### 2.1 Teşhis
`_get_grant_statements` (`ddl_generator.py`) `ALL_TAB_PRIVS WHERE TABLE_SCHEMA=:schema` kullanıyor:
- **Görünürlük:** `ALL_TAB_PRIVS` yalnız bağlı kullanıcının grantor/grantee/owner olduğu satırları
  döner. Validation user şema sahibi/DBA değilse, başka kullanıcıların verdiği grant'lar **görünmez**
  → "eksik çekiyor" semptomu.
- **Kapsam:** yalnız **object** privileges. System priv (`DBA_SYS_PRIVS`) ve role grant
  (`DBA_ROLE_PRIVS`) kapsanmıyor.
- **Karşılaştırma yok:** yalnız source okunuyor; bu bir generation fonksiyonu, doğrulama değil.

### 2.2 Revizyon planı
1. **Kaynak view seçimi (runtime):** `_grants_source_view(conn)` — `DBA_TAB_PRIVS` erişimi varsa
   (DBA / `SELECT_CATALOG_ROLE`) onu, yoksa `ALL_TAB_PRIVS`'i kullan. Tespit: `SELECT 1 FROM
   dba_tab_privs WHERE ROWNUM=1` dene/except düş. (Source **read-only**; yalnız SELECT.)
2. **Tam grant anahtarı:** `(grantee, owner, table_name, privilege, grantable, hierarchy)` +
   mümkünse `grantor`. DBA view bu kolonların tamamını verir.
3. **Yeni `validator/modules/grants.py` (validation modülü):**
   - src & tgt grant kümelerini aynı anahtarla indeksle (mevcut tablolarla sınırlı — `constraints.py`
     `common_tables` paterni).
   - `src ∖ tgt` → **FAILED** ("target'ta eksik yetki")
   - `tgt ∖ src` → **extra** (`output.extra_as` → NOT-SYNC/SYNC)
   - kesişim ama `grantable`/`hierarchy` farkı → **NOT-SYNC**
   - tam eşleşme → **SYNC**
   - `ModuleSummary(module="grants")`,
     `ValidationResult(object_type="GRANT", object_name=f"{table_name}:{privilege}→{grantee}")`.
4. **Router:** `run.py`'ye `if "grants" in active_modules:` dalı; `mc.grants` ile beslenir (orphan
   flag'e işlev). `--modules grants` da çalışır.
5. **DRY paylaşımı:** generation (`_get_grant_statements`) aynı fetch katmanını kullansın. Opsiyonel:
   `_run_generate_scripts`, GRANT FAILED kayıtlarından grant DDL üretsin (sequence/constraint
   remediation paterniyle) — böylece "eksik grant" hem raporlanır hem script'lenir.

### 2.3 Doğrulama (DB'siz)
- Küme-farkı testleri: FAILED / NOT-SYNC / SYNC / extra dört vakası monkeypatch ile.
- `DBA→ALL` fallback testi: `dba_tab_privs` sorgusu ORA-00942 fırlatınca `all_tab_privs`'e düşüyor mu.

---

## Bölüm 3 — Modüler Bağımsızlık Çekirdek Kuralları

1. **Tek-kaynak varsayılan:** her config varsayılanı tek yerde tanımlanır; parser kopya tutmaz,
   deep-merge eder. (Uygulandı: `_parse_generate_scripts`.)
2. **Validation ≠ Generation:** `modules.*` neyin **doğrulanacağını**, `generate_scripts.types.*`
   neyin **üretileceğini** kontrol eder. Biri diğerinin bayrağına bakmaz.
3. **Cross-flag yasağı:** bir tipin fetch/DDL yolu **yalnız kendi** bayrağına (`types.get(KENDİ_TİP)`
   / `modules.KENDİ_MODÜL`) bağlı olabilir; başka tipin bayrağı asla gate olamaz.
4. **İzole hata sınırı:** bir tipin fetch/DDL hatası diğerlerini bloke etmez — her obje kendi
   bloğunda; hata → `-- !! DDL alınamadı` + devam (mevcut patern korunur).
5. **Orphan-flag yok:** tanımlı her config alanı en az bir router/branch tarafından tüketilir, yoksa
   kaldırılır. Test: "her `ModulesConfig`/`types` alanı bir yerde okunuyor" assertion'ı.
6. **Erken-return audit:** `generate_scripts`'in global erken-çıkış koşulu yeni tip eklendikçe o tipin
   listesini de içermeli (bugün: `missing` + `not_sync_sequences` + `missing_constraints`). Yeni tip
   ekleyen, bu koşula kendi koleksiyonunu eklemekle yükümlüdür — aksi halde "tek tip varken üretim
   atlanıyor" coupling'i doğar.

---

## Sürüm & risk
- Bölüm 1 orphan-flag temizliği → **patch**.
- Bölüm 2 grants validation modülü → **minor** (yeni özellik).
- Bölüm 3 kurallar → `docs/` + (varsa) CONTRIBUTING.
- Güvenlik: source **asla yazılmaz** (tüm grant sorguları SELECT); generation script'leri yalnız
  **target**'ta uygulanır; token'lar env-only/maskeli (mevcut kurallar aynen).
