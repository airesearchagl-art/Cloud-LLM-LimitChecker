# Vendor Collector Production Readiness

- 調査日: 2026-08-07
- 対象: OpenAI / Gemini(Google Cloud) / Claude(Anthropic) の管理API Collector（`app/collectors/`）
- 実施内容: 公式ドキュメント調査、現行実装とのgap分析、Security・データ契約・重複防止・dry_run安全性の実装
- **実施していないこと**: 実Credentialを使った実接続、vendor APIへの実network request、DB migration、実データbackfill

このドキュメントは、実装（`app/collectors/*.py`、`app/schemas.py`、`app/main.py`）が依拠している公式仕様のEvidenceと、その結果としての設計判断をまとめたものです。実装と矛盾がある場合はコード＋テストを正とし、このドキュメントを更新してください。

## 1. 公式source一覧

### OpenAI

- Usage/Costs API: https://developers.openai.com/cookbook/examples/completions_usage_api
- Project rate limits API: https://developers.openai.com/api/reference/go/resources/admin/subresources/organization/subresources/projects/subresources/rate_limits/methods/list_rate_limits
- Spend limits (書き込み専用): https://developers.openai.com/api/docs/guides/spend-limits

`platform.openai.com/docs/api-reference/*` は本調査時点でWebFetchが403を返したため、上記の `developers.openai.com` 系ページ（ミラー/後継ドキュメント）を一次情報として使用した。

### Google Cloud / Gemini

- Gemini APIキー利用: https://ai.google.dev/gemini-api/docs/api-key
- Gemini API reference: https://ai.google.dev/api
- Live API (WebSocket) reference: https://ai.google.dev/api/live
- Cloud Monitoring API authentication: https://docs.cloud.google.com/monitoring/api/authentication
- APIキーの利用範囲: https://docs.cloud.google.com/docs/authentication/api-keys-use
- Quotaの参照・管理: https://docs.cloud.google.com/docs/quotas/view-manage
- Cloud Billing Budget API概要: https://docs.cloud.google.com/billing/docs/how-to/budget-api-overview
- Method: billingAccounts.budgets.get: https://docs.cloud.google.com/billing/docs/reference/budget/rest/v1/billingAccounts.budgets/get

### Anthropic / Claude

- Usage and Cost API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Spend Limits API (Enterprise専用): https://platform.claude.com/docs/en/manage-claude/spend-limits-api
- Rate Limits API: https://platform.claude.com/docs/en/manage-claude/rate-limits-api
- Claude Code statusline: https://code.claude.com/docs/en/statusline

## 2. Vendor仕様マトリクス

### OpenAI

| API / endpoint | 目的 | API status | 認証方式 | 必要なkey種別 | Organization/Project scope | pagination | bucket粒度 | unit | currency | 公式source | 現行コードとのgap | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `GET /v1/organization/usage/completions` | usage | 明示的なGA/Betaラベルなし（deprecation notice無し） | Bearer (Admin API key) | Organization Admin key（通常keyでは不可） | project_ids / user_ids / api_key_ids / modelsでfilter可 | `page` request param / `next_page` response field | `1m`/`1h`/`1d`（既定`1d`） | input_tokens / output_tokens / input_cached_tokens / num_model_requests（別フィールド） | — | cookbook | 実装済みだがpaginationが未実装だった → 本PRで追加 | **Supported** |
| `GET /v1/organization/costs` | cost | 同上 | 同上 | 同上 | project_ids、group_by: line_item / project_id | 同上（`bucket_width`は`1d`のみ） | `1d`のみ | `amount: {value, currency}`（decimal float） | usd | cookbook | 実装済み・値の形は一致 | **Supported** |
| `GET /v1/organization/projects/{id}/rate_limits` | quota（設定値、残量ではない） | 同上 | Bearer (Admin key) | Admin key | project単位、cursor pagination(`first_id`/`last_id`/`has_more`) | あり | — | max_requests_per_1_minute等 | — | API reference | **未実装**（実装対象外、本PRのscope外） | **Partial**（公式仕様Supported・実装Unsupported） |
| `POST /v1/organization/spend_limit` | budget（書き込みのみ） | 同上 | Bearer (Admin key) | Admin key | organization単位 | — | — | `{threshold_amount: cents, currency: USD, interval: month}` | usd | spend-limits guide | 読み取り専用endpointが見つからない → 未実装（実装できない） | **Unsupported**（読み取りAPI自体がNot Found） |

### Google Cloud / Gemini

| API | 目的 | API status | 認証方式 | APIキー対応 | project scope | pagination | 公式source | 現行コードとのgap | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| `generativelanguage.googleapis.com`（生成API、REST） | 生成（このアプリでは不使用） | GA | `x-goog-api-key` header | Yes（header。`?key=`はWebSocket Live API限定） | — | — | ai.google.dev/api | このアプリはこのAPIを一切呼ばない | 対象外 |
| Cloud Monitoring API (`monitoring.googleapis.com`) | usage（`aiplatform.googleapis.com`系metric） | GA | OAuth2/ADC | **No** | project | `pageToken`（未再検証） | docs.cloud.google.com/monitoring/api/authentication | 旧実装はAPIキーをquery paramへ入れる分岐を保持（到達不能コードだったが削除） | **Supported**（OAuth2のみ） |
| Service Usage API / Consumer Quota (`serviceusage.googleapis.com`) | quota | GA | OAuth2/ADC、`roles/servicemanagement.quotaViewer` | **No** | project | — | docs.cloud.google.com/docs/quotas/view-manage | 同上 | **Supported**（OAuth2のみ） |
| Cloud Billing Budget API (`billingbudgets.googleapis.com`) | budget | GA | OAuth2/ADC、scope `cloud-billing` | **No** | billing account | — | budget-api-overview / budgets.get | **未実装** | **Partial**（公式仕様Supported・実装Unsupported） |

**結論（最重要）**: Google AI Studioが発行するGemini APIキーは、Cloud Monitoring・Service Usage・Cloud Billing Budgetのいずれの公式管理APIも認証できない。これらはすべてOAuth2/ADCのみを受け付ける。生成API自体もWebSocket以外はquery paramではなくheader (`x-goog-api-key`) が公式方式であり、いずれにせよ管理APIとは無関係。

### Anthropic / Claude

| API / endpoint | 目的 | API status | 認証方式 | 必要なkey種別 | scope | pagination | bucket粒度 | unit | currency | 公式source | 現行コードとのgap | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `GET /v1/organizations/usage_report/messages` | usage | GA | `x-api-key` + `anthropic-version` | **Admin API key**（prefix `sk-ant-admin01-`、通常keyでは不可） | model / workspace_id / api_key_id / service_tier等でgroup_by | `has_more` + `next_page` cursor（`page`で送り返す） | `1m`(最大1440バケット) / `1h`(168) / `1d`(31) | uncached_input_tokens / cache_creation_input_tokens / cache_read_input_tokens / output_tokens（別フィールド） | — | usage-cost-api | 旧実装はフィールド名不一致（`input_tokens`等）・pagination未実装 → 本PRで修正 | **Supported** |
| `GET /v1/organizations/cost_report` | cost | GA | 同上 | 同上 | workspace_id / description でgroup_by | 同上 | `1d`のみ | 最小通貨単位（セント）のdecimal string | usd | 同上 | 旧実装は`{value, currency}`dict想定 → 実際は decimal string(cents) → 本PRで修正 | **Supported** |
| `GET /v1/organizations/rate_limits` | quota（Messages APIのRPM/TPM） | GA | Admin key | Admin key | model_group / group_type | — | — | requests_per_minute等 | — | rate-limits-api | **未実装** | **Partial**（公式仕様Supported・実装Unsupported） |
| `GET /v1/organizations/spend_limits/effective` 等 | budget | GA、**Enterprise専用** | Enterprise Admin key（`sk-ant-api01-`＋`read:spend_limits` scope、Console用Admin keyとは別種別） | Enterprise向けkey | organization | — | — | 最小通貨単位のdecimal string | usd | spend-limits-api | Console/Platform組織では利用不可と明記 → 未実装（対象外） | **Unsupported**（Console組織では非対応と公式に明記） |
| Claude Code statusLine `rate_limits.five_hour`/`.seven_day` | Claude.aiサブスクリプションのセッション制限（このアプリの`app/claude_code_usage_bridge.py`が別途利用） | GA | ローカルCLIのみ（stdin JSON） | — | — | — | — | — | — | code.claude.com/docs/en/statusline | Usage & Cost Admin APIとは別系統と公式に確認済み。混同しない | 対象外（別機能） |

## 3. 現行実装との主なgap（Finding分類）

- **Confirmed defect**: `build_import_key`（重複防止）が、既存identityと衝突した場合に常に`continue`（無視）していた。値が改訂された場合でも古い値のまま放置され、更新されることがなかった。→ 本PRで revision-safe upsert を実装。
- **Confirmed defect**: Claudeの`_normalize_usage`/`_normalize_costs`が、公式ドキュメントで確認できる実フィールド名（`uncached_input_tokens`等、`amount`はdecimal string）と異なるフィールド名を読んでいた。実API相手では値が全く抽出できない可能性が高い。→ 本PRでフィールド名を修正。
- **Confirmed defect**: `GeminiUsageCostCollector._get_json`に、APIキーをURL query paramへ追加する分岐が存在した。現在の`collect()`の制御フロー上は到達不能（`access_token`がある場合のみ`_get_json`が呼ばれ、その場合`bearer_token`相当が常に真になるため）だったが、将来の変更で再度到達可能になるリスクがあった。→ 本PRで該当コードパス自体を削除。
- **Confirmed defect**: dry_run実行が`import_normalized_records`を一切呼ばず、`records_found=len(rows)`だけを返していた。normalized record validationもimport判定も行われていなかった（要件: 「normalized record validationは実行する、import decisionは計算する」）。→ 本PRで`plan_normalized_records`（read-only）を追加。
- **Spec gap**: `CollectorNormalizedRecord`にmetric_kind相当のフィールドが無く、usage/cost/quotaが同じ`UsageRecord`として保存されうる設計だった（実際にGeminiの`_normalize_quota`はusageと同じ経路で保存されていた）。→ 本PRで`metric_kind`必須化＋persistence policyを追加。
- **Spec gap**: `unit`が自由文字列で、currency（`usd`）とtoken/request単位が型として区別されていなかった。→ 本PRでcanonical unit語彙＋metric_kindとの整合バリデーションを追加。
- **Enforcement gap**: `app/safety.py`の`assert_paid_model_calls_allowed`が定義されているが、`tests/test_safety.py`以外のどこからも呼ばれていない（本番コードパスで未使用）。実際の防御は「Collectorが最初から推論APIを呼ばない実装になっていること」と`tests/test_no_paid_model_calls.py`の静的文字列検査に依存している。本PRでは配線の追加は行わず、事実として記録する（Collectorが推論APIを呼ぶコードは依然として存在しない）。
- **Convention gap**: READMEの一部が「`usage_records`への保存はまだ行いません」と記載していたが、実装は既に`dry_run=false`で保存を行っていた（`finish_collector_import` → `import_normalized_records`）。本PRでREADMEの記述を実装に合わせて修正。
- **False positive（否定できた候補）**: 「Gemini APIキーがquery paramへ入る」という設計自体は事実だったが、実際に外部へ送信される経路（`collect()`から到達可能な経路）は存在しなかった。Confirmed defectとして扱いコードは削除したが、「稼働中に実際にキーが漏洩していた」という意味でのIncidentではない。

## 4. 実装したHardening

### metric_kind / canonical unit（`app/collectors/types.py`）

- `CollectorNormalizedRecord`に`metric_kind: Literal["usage","cost","quota","budget"]`を必須化。
- `unit`を`Literal["requests","input_tokens","output_tokens","cache_read_tokens","cache_creation_tokens","total_tokens","usd","quota_count"]`に制限（自由文字列を廃止）。
- `unit`と`metric_kind`の組み合わせをバリデーション（例: `usd`は`cost`/`budget`専用、token/request系unitは`usage`専用、`quota_count`は`quota`専用）。
- `period_start`/`period_end`（timezone-aware datetime、`start < end`必須）を必須化。`bucket_width`（例: `"1d"`）・`source_record_id`（vendor提供の安定ID、現時点ではどのvendorも提供していないためNone）を追加。

### Persistence policy（`app/collectors/types.py::PERSISTABLE_METRIC_KINDS`、`app/collectors/importer.py`）

| metric_kind | 保存方針 |
|---|---|
| `usage` | identityとperiodが確定できる場合に保存対象 |
| `cost` | currency(usd固定)・periodが確定できる場合に保存対象。精度はFloat（後述のMissing requirements参照） |
| `quota` | `usage_records`へは**保存しない**。既存`Limit`への反映方法は未設計（後述） |
| `budget` | `usage_records`へは**保存しない**。専用modelは未設計（後述） |

silent skipはしない。dry_run・実保存いずれも、レコード単位の理由（`imported`/`updated`/`duplicate`/`unsupported_metric_kind`/`dry_run`/`invalid_record`）を`ImportOutcome`として返す（`POST /api/collect/{vendor}`レスポンスの`outcomes`フィールド。DBには永続化しない — `CollectorRun`のschema変更＝migrationになるため）。

### Import identity / revision-safe upsert（`app/collectors/importer.py::build_import_key`, `_plan_and_apply`）

- identity: `vendor / source_type / project_id / organization_id / workspace_id / model_name / limit_type / metric_kind / unit / period_start / period_end`のSHA256ハッシュ。`used_value`は含めない（既存方針を維持）。
- 同一identityで既存recordが見つかった場合:
  - `used_value`が同じ → `duplicate`（no-op）
  - `used_value`が異なる → 既存の`UsageRecord`をatomicに更新（`updated`。新規行は作らない）
- 異なるperiod / scope / metric_kindは別record。
- pagination再取得で同一値の重複行が生じないことを、同一batch内での重複入力に対するテストで確認（SQLAlchemyのautoflushにより、同一batch内でも既存importが検出される）。
- 不正なrecord（Pydantic validation失敗）は、dry_run（read-only）では`invalid_record`として個別スキップし処理継続、実書き込みでは既存どおりbatch全体をrollbackする（既存テストの契約を維持）。
- migrationは実行していない。既存テーブル（`CollectorImport.usage_record_id` FK）だけで実現可能な範囲に留めた。

### Gemini Security（`app/collectors/gemini_collector.py`, `app/collectors/preflight.py`, `app/main.py`）

- 公式ドキュメントで、Cloud Monitoring / Service Usage / Cloud Billing BudgetのいずれもAPIキー認証に対応していないことを確認（本ドキュメント2節）。
- `GeminiUsageCostCollector`から`api_key`フィールドを完全に削除（無効化ではなく削除。将来の変更で誤って再度有効化できないようにするため）。
- URLクエリ文字列・ヘッダーのいずれにも、いかなる状況でもAPIキーを含めない。
- `access_token`（OAuth2/ADC）が無い場合は`GeminiCollectorConfigError`で即座に停止する（空配列を返して「取得成功・0件」に見せることはしない）。
- HTTPエラーメッセージは固定文言のみを返し、レスポンスボディやURLを一切含めない（Gemini以外の2vendorは詳細メッセージを240文字に切り詰めて含めるが、Geminiだけはそれも行わない — より厳格）。

### Config preflight（`app/collectors/preflight.py`, `GET /api/collector-preflight`）

- ネットワーク通信なしで、vendorごとの`configured` / `auth_mode` / `production_ready` / `missing_requirements` / `notes`を返す。
- key値・token値・Credentialのpath・account/organization名・Secretの長さ・prefix/suffix・hashは一切返さない（Claudeのkey prefix判定はbool判定の結果のみを`notes`の固定文言として返し、実際のprefix文字列そのものは出力しない）。

## 5. Production Ready / Partial / Unsupported / Inconclusive 分類（実装状態ベース）

- **Production Ready（実装済み・公式仕様に一致）**: OpenAI usage/costs、Claude usage/cost report
- **Partial（公式APIは存在するが未実装）**: OpenAI project rate limits（quota）、Claude organization rate limits（quota）、Gemini Cloud Billing Budget API
- **Unsupported（公式に読み取り経路が存在しない、または対象外と明記）**: OpenAI budget読み取り（POSTのみ確認）、Claude spend limits（Console組織では利用不可と公式に明記）
- **Inconclusive**: 本PR内では無し（Gemini APIキーのauth可否は、当初Inconclusive-but-fail-closedとして実装したが、その後の追加調査でConfirmed（OAuth2必須・APIキー不可）に格上げ済み）

## 6. 実Credential接続のHuman Gate（次のステップ）

以下は本PRでは実施しておらず、ユーザーの明示的な許可のもとで別セッション・別PRとして進める。

- **OpenAI**: Organization Admin API keyでの実接続確認（通常keyでは401/403になることの実地確認を含む）
- **Google Cloud / Gemini**: OAuth2アクセストークン（またはADC）+ プロジェクトIDでの実接続確認。必要IAM: `roles/monitoring.viewer`系（Monitoring Viewer）、`roles/servicemanagement.quotaViewer`（Quota Viewer）
- **Anthropic / Claude**: organization Admin API key（`sk-ant-admin01-`）での実接続確認

値（実際のkey/token文字列）はこのドキュメントにもVaultにも記録しない。

## 7. 未対応事項

- OpenAI project rate limits（quota）Collector未実装
- Claude organization rate limits（quota）Collector未実装
- Gemini Cloud Billing Budget Collector未実装
- quota/budgetを`Limit`（既存の残量管理テーブル）へ反映する設計は未確定（`usage_records`への保存を避けているだけで、代替の保存先は未設計）
- cost金額の精度: `UsageRecord.used_value`は`Float`カラムのため、現状は`float`として保存している。より高精度なDecimal/固定小数点が必要な場合はDB migrationが必要（本PRでは実施せず、設計課題として記録するのみ）
- `ImportOutcome`（per-record理由）はAPIレスポンスのみに存在し、`CollectorRun`へは永続化されない（永続化するには新規テーブル/カラムが必要でmigrationになるため）
- Claude API cost_reportのgroup_by/フィールド構造は、公式ドキュメントの記述から実装したものであり、実レスポンスでの確認はHuman Gate後に行う

## 8. 採用しなかった方式

- Gemini APIキーをquery paramへ入れる方式（公式にAPIキー認証自体が存在しないため）
- 不明なvendor unit文字列をそのまま自由入力として保存する方式（canonical unit語彙へ制限）
- quota/budgetをusageと同じ`UsageRecord`として保存する方式（metric_kindで分離し、persistence policyで除外）
- 重複importを常にskipする方式（値が改訂された場合に更新できないため、revision-safe upsertへ変更）
- `CollectorRun`テーブルへper-record理由を永続化する設計（migrationが必要になるため、このPRのスコープでは見送り、APIレスポンスのみで返す設計とした）
