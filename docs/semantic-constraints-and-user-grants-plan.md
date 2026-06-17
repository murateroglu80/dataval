# dataval — Mimari Plan: Semantic Constraint Validation + constraint_types + User/Grant modülü

> Durum: **PLAN** (uygulanmadı). Üç eksen: (1) isim-bağımsız "akıllı" constraint eşleştirme +
> conflict-safe DDL, (2) `constraint_types` filtre parametresi, (3) User & Grant izolasyon modülü
> (dinamik sistem-kullanıcı bypass'ı ile).

---

## Mevcut durum — doğru tespit (kanıtlı)

| Bulgu | Kanıt | Sonuç |
|-------|-------|-------|
| Constraint **detection zaten kolon-bazlı**, isim değil | `constraints.py:118` `_key = (constraint_type, columns, cond)` | "farklı isim, aynı kolon" **zaten SYNC** — premise kısmen yanlış |
| Pseudo-constraint **görünmez** | `constraints.py` yalnız `ALL_CONSTRAINTS` okur; index'lere bakmaz | target'ta UNIQUE INDEX(+NOT NULL) ile sağlanan teklik **yanlışlıkla FAILED** |
| Apply-time **conflict** | `ddl_generator._get_constraint_ddls` kör `ALTER TABLE ADD CONSTRAINT` üretir | target'ta aynı kolonda index/constraint varsa **ORA-02261 / ORA-00955** |
| `SYS_%` constraint'ler **atlanıyor** | `constraints.py:42,55` `NOT LIKE 'SYS_%'` | sistem-isimli PK/UK kör nokta — kolon-bazlı eşleştirmede ad önemsiz olduğundan bu dışlama gevşetilebilir |
| Sistem-user filtresi **statik & yalnız grant** | `grants.py` sabit grantee listesi; user-level yok | 11g/19c default user'ları dinamik ayıklanmıyor; `CREATE USER`/sys-priv/role diff yok |

**Özet:** Gerçek problem "isim bazlı kontrol" değil; (a) target'ın tekliği **constraint yerine
index** ile sağlaması ve (b) üretilen DDL'in **var olan yapıyla çakışması**.

---

## Bölüm 1 — Semantic Constraint Validation (isim-bağımsız + conflict-safe)

### 1.1 Eşleştirme imzası (mevcut + genişletme)
- İmza: `(type, columns, cond)` — **korunur**. FK için imzaya **referans tablo+kolon+`delete_rule`**
  eklenir (şu an FK kolon-only; iki farklı FK aynı kolonda olabilir).
- Kolon **sırası**: PK/UK denkliği için küme yeterli; ama index performansı için sıra önemli →
  hem `frozenset(cols)` (denklik) hem sıralı liste (NOT-SYNC sinyali) tutulur. Küme farkı → FAILED,
  yalnız sıra farkı → NOT-SYNC (`diffs=[("kolon sırası", src, tgt)]`).
- `SYS_%` dışlamasını **gevşet**: kolon-bazlı eşleştirmede ad önemsiz; sistem-isimli PK/UK'ları
  kapsama al (yalnız `BIN$%` geri-dönüşüm kovası dışlanır).

### 1.2 Pseudo-constraint farkındalığı (kritik)
Target tarafında **etkin teklik** kümesi `ALL_CONSTRAINTS`'e ek olarak şunlardan türetilir:
- **pseudo-UK:** `ALL_INDEXES.uniqueness='UNIQUE'` → o kolonlar üzerinde teklik var.
- **pseudo-PK:** unique index **VE** kolonların tümü `ALL_TAB_COLUMNS.nullable='N'` → PK eşdeğeri.
Bu türev küme, target imza kümesine "enforced_by=index" etiketiyle eklenir. Böylece
`source PK (constraint) ↔ target unique-index` eşleşmesi **FAILED değil, NOT-SYNC** olur
(`diffs=[("enforce", "CONSTRAINT", "UNIQUE INDEX")]`).

### 1.3 Statü ayrıştırma (net kural)
| Durum | Statü |
|-------|-------|
| kolon+tip+koşul eşleşiyor, ad da aynı, aynı şekilde enforce | **SYNC** |
| kolon+tip eşleşiyor ama: ad farklı **veya** enforce farklı (constraint↔index) **veya** durum farklı (`DEFERRABLE`/`VALIDATED`/`ENABLE`) | **NOT-SYNC** (granüler diffs) |
| kolon+tip target'ta **hiçbir biçimde** yok (ne constraint ne covering unique index) | **FAILED** |

### 1.4 Conflict-safe DDL üretimi
Generator yalnız **gerçek FAILED** için üretir. Üretmeden önce validation aşaması her FAILED için
`target_state` hesaplar (constraints.py her iki conn'a sahip → bu bilgi generator'a iletilir):
- `none` → düz `ALTER TABLE ADD CONSTRAINT ...;`
- `covering_index:<ix>` → `ALTER TABLE ADD CONSTRAINT pk PRIMARY KEY (cols) USING INDEX <ix>;`
  (ya da `ENABLE`) → **ORA-02261 önlenir**.
- `exists_other_name:<name>` → DDL yerine `-- Zaten mevcut (farklı ad: <name>) — atlandı` yorumu.
- **Mimari:** `missing_constraints` tuple'ı `(table, label, signature, target_state)`'e genişler
  (run.py `_run_generate_scripts` doldurur; bilgi validation'dan gelir — generator target'a
  bağlanmaz, source read-only ilkesi korunur).

---

## Bölüm 2 — `constraint_types` Konfigürasyon Parametresi

### 2.1 YAML
```yaml
modules:
  constraints: true
  constraint_types: ALL          # veya [PK, UK, FK, CHECK]  (alt küme)
```
### 2.2 Parse (`config_loader._parse_modules`)
- `ModulesConfig.constraint_types: set[str]` alanı. Parse: `"ALL"` / boş → `{"PK","UK","FK","CHECK"}`;
  liste → upper-normalize edilmiş alt küme. Geçersiz değer → uyarı + ALL.
### 2.3 Uygulama
- `constraints.py run()`: label→ctype eşlemesiyle (`PK→P,UK→U,FK→R,CHECK→C`) yalnız seçili tipleri
  karşılaştır; `SQL_CONSTRAINTS`'in `constraint_type IN (...)` kısmı seçilenlerden dinamik kurulur.
- **Generation aynı filtreyi paylaşır:** `ddl_generator` CONSTRAINT üretiminde `missing_constraints`
  label'ı `constraint_types`'a göre süzülür (tek kaynak: ModulesConfig).
- CLI (opsiyonel): `--constraint-types PK,UK`.

---

## Bölüm 3 — User & Grant İzolasyon Modülü

### 3.1 Dinamik sistem-kullanıcı filtresi (`_non_system_users(conn)`) — paylaşılan helper
- **12c+/19c:** `SELECT username FROM dba_users WHERE oracle_maintained = 'N'` (en doğru, dinamik).
- **11g (kolon yok → ORA-00904):** curated statik liste ile fallback. Liste (her iki sürümün
  default'ları): `SYS, SYSTEM, OUTLN, XDB, DBSNMP, WMSYS, CTXSYS, MDSYS, ORDSYS, ORDPLUGINS,
  ORDDATA, EXFSYS, APPQOSSYS, ANONYMOUS, SI_INFORMTN_SCHEMA, OLAPSYS, FLOWS_FILES, OWBSYS,
  GSMADMIN_INTERNAL, AUDSYS, LBACSYS, DVSYS, DVF, SYSBACKUP, SYSDG, SYSKM, SYSRAC, GGSYS,
  DBSFWUSER, REMOTE_SCHEDULER_AGENT, SYS$UMF, APEX_PUBLIC_USER, APEX_*, SPATIAL_*_ADMIN, MGMT_VIEW`.
- **Probe:** `SELECT oracle_maintained FROM dba_users WHERE ROWNUM=1` → ORA-00904 ise statik;
  `DBA_USERS` erişimi yoksa (ORA-00942) `ALL_USERS`'a düş (yalnız statik filtre uygulanabilir).
- LIKE desenleri (`APEX\_%`, `SPATIAL\_%`) için ESCAPE'li filtre. Sonuç: yalnız **uygulama** user'ları.

### 3.2 Yeni `validator/modules/users.py`
- **User existence diff:** app-user listesi src↔tgt → src∖tgt **FAILED** (target'ta user yok),
  tgt∖src **extra**. `object_type="USER"`, `object_name=username`. Öznitelik diff (account_status,
  default_tablespace, profile) → **NOT-SYNC**.
- **Per-user yetki diff (common users):**
  - **System priv:** `DBA_SYS_PRIVS` (grantee, privilege, admin_option).
  - **Role grants:** `DBA_ROLE_PRIVS` (grantee, granted_role, admin_option, default_role).
  - **Object grants:** mevcut `grants.fetch_object_grants` (grantee-merkezli süzme).
  - Küme farkı → **FAILED**; `admin_option`/`default_role` farkı → **NOT-SYNC**; birebir → **SYNC**.
    `object_type ∈ {USER, SYS_PRIV, ROLE, OBJ_PRIV}`, `object_name=f"{priv/role} → {grantee}"`.
- **CREATE USER DDL üretimi (opsiyonel, hassas):** FAILED user için `CREATE USER ... IDENTIFIED BY
  VALUES '<hash>'` + default tablespace/profile/quota; ardından sys-priv/role/obj-grant'lar.
  ⚠️ Parola hash'i **11g (`DBA_USERS.PASSWORD`) ↔ 19c (`USER$.SPARE4`)** farklı; taşınabilirlik
  sürüm-bağımlı → **default dry-run + yorumlu**, hash **loglanmaz**.

### 3.3 Mevcut grants.py ile ilişki (decoupling)
- `grants.py` → object-grant / **schema-scoped** kalır (schema-mapping bazlı).
- `users.py` → **instance-wide** user+privilege. İkisi `_non_system_users` ve `fetch_object_grants`
  helper'larını paylaşır (DRY). config: `modules.users: false` (opt-in), router'a `users` dalı.

---

## Modüler izolasyon (önceki `refactor-grants-decoupling-plan.md` Bölüm 3 ile uyumlu)
- Sistem-user/sistem-grantee filtresi **tek helper** (DRY); validation≠generation; tüm opt-in
  flag'ler router'da tüketilir (orphan flag yasağı); bir tipin DDL hatası diğerlerini bloke etmez.

## Sürüm etkisi & sıra
- **Bölüm 1** (semantic + conflict-safe) → minor. Bağımsız değer; önce yapılabilir → **v0.9.0**.
- **Bölüm 2** (constraint_types) → küçük; Bölüm 1 ile birlikte.
- **Bölüm 3** (users modülü) → minor; ayrı tur → **v0.10.0**.

## Doğrulama (DB'siz monkeypatch)
- pseudo-PK (unique index + NOT NULL) → NOT-SYNC; covering index → `USING INDEX` DDL (ORA-02261 yok).
- `exists_other_name` → DDL yerine yorum.
- `constraint_types=[PK,UK]` → yalnız PK/UK karşılaştırılır/üretilir.
- `ORACLE_MAINTAINED` probe → 11g'de ORA-00904 → statik fallback; DBA_USERS yok → ALL_USERS.
- users: existence/sys-priv/role/obj-grant dört vakası (FAILED/NOT-SYNC/SYNC/extra); sistem-user'lar
  hiçbir sorguda görünmüyor.
