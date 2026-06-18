# dataval — Hata Ayıklama & İzolasyon Planı: `grants:false` bypass + filtresiz GRANT dökümü

> Durum: **PLAN** (uygulanmadı). Bug: `modules.grants: false` olmasına rağmen `--generate-missing`
> akışı `<TGT>_GRANT.sql` (6463 grant) üretiyor, SYNC grant'ları da döküyor ve ekrana basıyor.

---

## Kök neden (kodla doğrulandı)

| # | Bulgu | Kanıt |
|---|-------|-------|
| A | `grants:false` **doğru parse ediliyor** (bool) ve **validation modülü doğru atlanıyor** | `config_loader._parse_modules` `grants=raw.get("grants", False)`; `run.py` `if mc.grants: active_modules.add("grants")` |
| B | **GRANT üretimi farklı bir bayrağa bakıyor** — `generate_scripts.types.GRANT` (varsayılan **True**) | `ddl_generator.generate_scripts` ~`642`: `if t == "GRANT": if cfg.types.get("GRANT", True)` (cfg = GenerateScriptsConfig) |
| C | **Generator modül bayraklarından habersiz** — yalnız `gs_cfg` alır, `cfg.modules` görmez | `run.py _run_generate_scripts` → `generate_scripts(..., cfg=gs_cfg)` |
| D | **`_write_grant_file` TÜM source grant'larını döküyor** — target diff'i / FAILED / `only_missing` yok | `_get_grant_statements` `fetch_object_grants(source)` → hepsi; 6463 + SYNC dahil |
| E | Ekran kirliliği | `_write_grant_file` koşulsuz `console.print("✅ …_GRANT.sql …")` |

**Özet:** "Config ihlali" bir parse hatası değil; kullanıcı **validation** bayrağını (`modules.grants`)
kapattı, ama üretim **ayrı** bir bayrağı (`generate_scripts.types.GRANT`, default True) okuyor ve
GRANT yolu — diğer tiplerden farklı olarak — **validation sonuçlarını hiç kullanmadan** kaynaktan
toptan döküm yapıyor. İki ayrı çöküş: (1) modül-üretim bağ kopukluğu, (2) FAILED-only mantığının
GRANT'ta hiç uygulanmaması.

---

## Bölüm 1 — Config State & Boolean Parse Analizi (doğrulama adımları)

1. **Parse teyidi:** `_parse_modules`'ta `grants` değerinin tipini logla/incele → `False` (bool).
   YAML `false`/`"false"`/`no` varyasyonları için `bool(raw.get(...))` davranışını doğrula
   (YAML zaten `false`→bool çevirir; tırnaklı `"false"` riskli → normalize edilebilir).
2. **Tetikleyici izi:** `grep -n "types.get(\"GRANT\"" ` ve `grep -n "mc.grants\|\"grants\" in active_modules"`
   → GRANT üretiminin `modules.grants`'ı **hiç** okumadığını göster (B/C kanıtı).
3. **Sonuç:** "ezilme" parse'ta değil; **yanlış kaynaktan okuma**. `modules.grants` ve
   `generate_scripts.types.GRANT` iki ayrı eksen; ikincisi default-on olduğundan bayrak görünüşte
   yok sayılıyor.

## Bölüm 2 — DDL Motoru İzolasyonu (yapısal düzeltme)

Hedef: GRANT üretimi **diğer tiplerle aynı sözleşmeye** uysun — (a) modül açıksa, (b) yalnız
**FAILED/eksik** kayıtlar için, (c) `only_missing` semantiğine saygılı.

1. **Modül-farkında üretim:** `generate_scripts`'e modül bayrakları (ör. `cfg.modules`'tan türeyen
   bir `allowed_modules`/`module_enabled` kümesi) geçir. GRANT bloğu artık
   `types.GRANT AND modules.grants` ister (cross-gate). `modules.grants=false` → GRANT üretimi yok.
2. **FAILED-only GRANT üretimi:** `_write_grant_file`'ın koşulsuz tam-döküm yolunu kaldır.
   GRANT'ı diğer tipler gibi **validation FAILED sonuçlarından** üret:
   `run.py _run_generate_scripts`, grants modülünün ürettiği `FAILED` grant kayıtlarını
   `missing_grants` olarak toplar (sequence/constraint paterni) ve generator yalnız bunlar için
   `GRANT …` ifadesi yazar. SYNC/extra grant'lar **asla** script'e girmez → 6463 → yalnız gerçekten
   eksik olanlar.
3. **`only_missing` saygısı:** `GenerateScriptsConfig.only_missing` (default True) GRANT dahil tüm
   tiplerde uygulanır; `false` ise (bilinçli "hepsini üret") davranışı ayrı ve **açık** olur.
4. **Tutarlılık:** Böylece `modules.grants=false` → grants ne fetch edilir, ne diff'lenir, ne
   yazdırılır, ne de DDL üretilir (E kirliliği de biter, çünkü blok hiç çalışmaz).

## Bölüm 3 — Hard Bypass (Execution Guard) Standardizasyonu

Tek merkezi kural: **`modules.X = false` ⇒ X için fetch + diff + stdout + DDL %100 engellenir.**

1. **Tek doğruluk kaynağı:** `module_enabled(cfg, name) -> bool` yardımcı fonksiyonu
   (config_loader veya run.py). Hem router (`active_modules`) hem generator bunu kullanır;
   ayrıca `--modules` override'ı da aynı kapıdan geçer.
2. **Tip→modül haritası:** üretim tipleri sahibi modüle bağlanır
   (`GRANT→grants`, `CONSTRAINT→constraints`, `INDEX→indexes`, `SEQUENCE→sequences`,
   PL/SQL→`code`). Bir tip yalnızca **(modülü açık) VE (FAILED sonucu var)** ise üretilir.
   `generate_scripts.types.*` artık yalnızca "bu açık modül içinde bu tipi üret/üretme" ince ayarı;
   modül kapalıysa hiçbir şeyi geçersiz kılamaz.
3. **Generation girişinde guard:** `_run_generate_scripts` FAILED toplarken, kaynağı kapalı bir
   modül olan sonuçları **atla** (zaten üretilmezler ama guard açık ve merkezi olmalı).
4. **Stdout guard:** kapalı modüller için ne panel ne "✅ … üretildi" satırı basılır.
5. **Regresyon kuralı:** "tanımlı her modül bayrağı; fetch/diff/print/DDL dört yüzeyinde de
   tüketilir" testi (önceki `docs/refactor-grants-decoupling-plan.md` Bölüm 3 ile uyumlu —
   orphan/half-wired flag yasağının genişletilmiş hali).

---

## Doğrulama (DB'siz monkeypatch + niyet testleri)

- `modules.grants=false` + `generate_scripts.enabled=true` → **hiç** `_GRANT.sql` üretilmez,
  grant fetch çağrılmaz, ekrana grant satırı basılmaz.
- `modules.grants=true` + bir source grant target'ta eksik → yalnız **o** grant `_GRANT.sql`'e
  girer (SYNC olanlar girmez); sayı = FAILED grant sayısı (6463 değil).
- Aynı guard genelleştirme: `modules.constraints=false` → constraint fetch/diff/CONSTRAINT.sql yok;
  `sequences=false` → sequence yok. (Execution Guard tek kapıdan.)
- `py_compile` + `check_integrity` + mevcut testlerin regresyonsuz geçmesi.

## Sürüm etkisi
Davranış düzeltmesi + küçük yapısal refactor → **patch/minor (v0.9.1 veya v0.10.0)**. Güvenlik
kuralları aynen: source read-only, token env-only/maskeli.
