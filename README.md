# Cloud LLM Limit Checker

ChatGPT、Gemini、ClaudeのWeb版サブスクリプション利用制限とAPI利用状況を、ローカルPCまたは小規模サーバーで管理するMVPです。

## Safety Policy

このツールは管理情報取得専用です。OpenAI / Gemini / Claude のモデル推論APIは呼びません。

許可する対象は、usage、costs、billing、quota、rate limit などの管理情報取得APIです。従量課金が発生するモデル推論、画像生成、音声、リアルタイム処理は実装・テスト・確認の対象外です。

Collectorは初期状態では無効です。dry-runを標準にし、明示的に有効化しない限りベンダーAPI取得は動きません。

```text
ENABLE_VENDOR_COLLECTORS=false
ENABLE_OPENAI_COLLECTOR=false
ENABLE_GEMINI_COLLECTOR=false
ENABLE_CLAUDE_COLLECTOR=false
COLLECTOR_DRY_RUN_DEFAULT=true
ALLOW_PAID_MODEL_CALLS=false
MAX_COLLECTOR_CALLS_PER_DAY=24
```

`ALLOW_PAID_MODEL_CALLS=false` を維持してください。APIキーは `.env` で管理し、ソースコードやSQLiteには保存しません。ベンダー側でもspend cap、budget limit、quota制限を必ず設定してください。

## Basic Auth

LANやNASで共有する場合はBasic認証を有効化できます。

```text
ENABLE_BASIC_AUTH=true
BASIC_AUTH_USERNAME=admin
BASIC_AUTH_PASSWORD=change-this-password
```

`GET /api/health` はBasic認証が有効な場合でも認証不要です。死活監視専用で、機密情報を返しません。それ以外のUI/APIはBasic認証の対象です。

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## Run

```powershell
uvicorn app.main:app --reload
```

```text
http://127.0.0.1:8000
```

## Windows Standard Commands

`.venv` が未作成の場合は、お使いのPython 3.xで作成してください（`python -m venv .venv`）。作成後は、以下のコマンドを標準の実行方法とします。

```text
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8001
```

`.venv` はGit管理対象外です（`.gitignore`で除外済み）。依存パッケージのバージョン更新は、この手順の一部としてではなく、`requirements.txt`の変更内容とpytest実行結果を明示的に確認する別PRで行ってください。

## Windows: 一番簡単な起動

日常利用向けに、コマンドを覚えなくても起動できるBATファイルを用意しています。

- リポジトリ直下の `start_dashboard.bat` をダブルクリックする
- 自動的にブラウザで `http://127.0.0.1:8001` が開く
- 停止する場合は、開いたコンソールウィンドウで `Ctrl+C` を押す
- すでにserverが起動中の場合は、二重起動せず既存dashboardを開く

### 初回セットアップ

`start_dashboard.bat` は `.venv` を自動作成しません。初回のみ、上記の Setup または Windows Standard Commands の手順で `.venv` を作成し、依存パッケージをインストールしてください。`.venv\Scripts\python.exe` が見つからない場合、BATはサーバーを起動せずセットアップ手順を案内して終了します。

### 開発時

コード変更を即座に反映したい開発時は、`start_dashboard.bat`ではなく、上記「Windows Standard Commands」の `--reload` 付きコマンド（`.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8001`）を使用してください。`start_dashboard.bat`は日常利用向けのため`--reload`を付けていません。

### Windows再起動後

Windows再起動やログオフでサーバープロセスも終了します。再度使う場合は`start_dashboard.bat`をもう一度実行してください。Windows起動時の自動起動（スタートアップ登録・タスクスケジューラ・サービス化）は現時点では未対応です。

### ネットワーク

`start_dashboard.bat`はサーバーを`127.0.0.1`（このPC自身）にのみbindします。他の端末やLAN上の別PCからはアクセスできません。日常利用（このPCだけでの利用）を前提とした起動方法です。

## Usage Records

使用量入力には「通常加算」と「補正」があります。

- 通常加算は、現在値の上書きではありません。
- 補正は、履歴を削除せず、差分レコードを追加して調整します。
- 補正レコードは `source_type=manual_adjustment` として表示されます。
- `set` モードは未実装です。

## Export

```text
GET /api/export/json
GET /api/export/limits.csv
GET /api/export/usage-records.csv
```

CSVはUTF-8 BOM付きです。usage records CSVには通常加算と補正レコードの両方が含まれます。

## Seed API

`POST /api/seed` はMVPのローカル初期化・保守用APIです。

- `ENABLE_SEED_API=true` の場合のみ実行できます。
- デフォルトは `false` です。
- Basic認証を有効化していても、通常運用では `ENABLE_SEED_API=false` を推奨します。

## Main APIs

```text
GET  /api/health
GET  /api/dashboard
GET  /api/services
POST /api/services
GET  /api/limits
POST /api/limits
POST /api/limits/{limit_id}/usage
GET  /api/usage-records
GET  /api/export/json
GET  /api/export/limits.csv
GET  /api/export/usage-records.csv
POST /api/collect/{vendor}
GET  /api/collector-runs
```

## Collector Runs

Collectorは初期状態では無効です。利用する場合は、グローバル設定とvendor別設定の両方を有効にします。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_OPENAI_COLLECTOR=true
ENABLE_GEMINI_COLLECTOR=true
ENABLE_CLAUDE_COLLECTOR=true
COLLECTOR_DRY_RUN_DEFAULT=true
MAX_COLLECTOR_CALLS_PER_DAY=24
```

```text
POST /api/collect/{vendor}?dry_run=true
GET  /api/collector-runs
```

`vendor` は `openai` / `gemini` / `claude` のみです。Collector実行は `collector_runs` に記録され、dry-runも日次実行回数に含まれます。日次上限は `MAX_COLLECTOR_CALLS_PER_DAY` で制御します。

OpenAI / Gemini / Claude Collectorはいずれも実際に管理APIへ`GET`通信を行う実装です(生成・推論APIは呼びません)。各vendorの公式仕様・現行実装とのgap・認証方式の判断根拠は [docs/vendor-collector-production-readiness.md](docs/vendor-collector-production-readiness.md) を参照してください。

### Collector config preflight

`GET /api/collector-preflight` で、各vendorの設定状態をネットワーク通信なしで確認できます。`configured` / `auth_mode` / `production_ready` / `missing_requirements` / `notes` を返し、APIキーやtoken、organization名などの値そのものは一切含みません。

### OpenAI Collector

OpenAI Collectorはusage/costなどの管理情報取得専用です。モデル推論API、画像生成、音声、embedding、moderationなどの生成・推論系APIは呼びません。実行には以下が必要です。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_OPENAI_COLLECTOR=true
OPENAI_API_KEY=...
ALLOW_PAID_MODEL_CALLS=false
```

`OPENAI_API_KEY` は**Organization Admin API key**(Organization → Admin keysで作成)である必要があります。通常の(project-scoped)APIキーではusage/costs管理APIにアクセスできません。`ALLOW_PAID_MODEL_CALLS=false` のまま使います。まずは `POST /api/collect/openai?dry_run=true` を推奨します。

OpenAI側でもspend cap / budget limitを設定してください。ただし公式APIにはbudget/spend capの**読み取り**専用エンドポイントが見つかっておらず(設定用のPOSTのみ確認)、このCollectorはbudget値を取得しません。ChatGPT Web版の残メッセージ数やPlus/ProのWeb利用枠はOpenAI APIのusage/cost Collectorでは取得対象外です。

### OpenAI Collector dry-run and permissions

`dry_run=true` は `usage_records` に保存しない確認モードです。ただし、OpenAI の usage/costs 管理APIへの `GET` 通信は発生します(正規化・import判定は行いますが、書き込みは一切行いません)。モデル推論APIやトークン課金対象の生成APIは呼びません。外部通信自体を避けたい場合は、Collectorを実行しないでください。

実行前にOpenAI側でspend cap / budget limitを設定してください。usage / costs 系APIはOrganization Admin API keyが必要です。OpenAI管理APIから `401` / `403` が返った場合、このアプリではOpenAI管理APIエラーとして扱い、`collector_runs.error_message` に権限確認が必要な旨を記録します。

### Gemini Collector

Gemini Collectorはusage(Cloud Monitoring)・quota(Service Usage/Consumer Quota)の管理情報取得専用です。生成API、画像生成、動画生成、音声生成、grounding/search/tool系の生成処理は呼びません。

実行には**OAuth2アクセストークン**とGoogle CloudプロジェクトIDが必要です。`GOOGLE_CLOUD_ACCESS_TOKEN`に設定したトークンをそのままBearerトークンとして使います。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_GEMINI_COLLECTOR=true
GOOGLE_CLOUD_ACCESS_TOKEN=...
GOOGLE_CLOUD_PROJECT=...
ALLOW_PAID_MODEL_CALLS=false
```

**Application Default Credentials(ADC)の自動探索・トークン更新は実装していません。** このCollectorが受け付けるのは静的なOAuth2アクセストークン(`GOOGLE_CLOUD_ACCESS_TOKEN`)のみです。トークンの取得・有効期限管理・更新は利用者側の責任です(例: `gcloud auth print-access-token` を手動または外部スクリプトで実行し、都度`.env`を更新するなど)。ADCの正式対応(google-authライブラリ等、新規dependencyの追加を伴う)は今回のPRでは行わず、将来候補として記録します。

**`GEMINI_API_KEY`(Google AI Studioで発行するAPIキー)だけでは実行できません。** Cloud Monitoring APIおよびService Usage/Consumer Quota APIは公式ドキュメント上、OAuth2/ADCのみを認証方式として受け付け、APIキー認証には対応していません。この制約はGoogle公式ドキュメントで確認済みです(詳細は [docs/vendor-collector-production-readiness.md](docs/vendor-collector-production-readiness.md))。`GEMINI_API_KEY`のみが設定されている場合、Collectorは「未設定」として扱い、400エラーを返します(空の結果を返して黙って成功したように見せることはしません)。

まずは `POST /api/collect/gemini?dry_run=true` を推奨します。`dry_run=true` は `usage_records` に保存しない確認モードですが、管理情報取得APIへの外部GET通信は発生します。外部通信自体を避けたい場合はCollectorを実行しないでください。

Google Cloud側でもbudget / quota / alertを設定してください。Cloud Billing Budget APIによるbudget読み取りは公式に存在しますが(OAuth2/ADCのみ、APIキー不可)、このCollectorはまだ実装していません。Gemini Web版の残り利用枠は取得対象外です。

### Claude Collector

Claude Collectorはusage / cost(Anthropic Organization Usage & Cost Admin API)の管理情報取得専用です。Messages API、streaming Messages、tool use付きMessages、prompt cachingを含むモデル推論は呼びません。

実行には以下の認証情報が必要です。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_CLAUDE_COLLECTOR=true
ANTHROPIC_API_KEY=...
ALLOW_PAID_MODEL_CALLS=false
```

`ANTHROPIC_API_KEY` は**organization Admin API key**(プレフィックス `sk-ant-admin01-`)である必要があります。通常のAPIキーではusage/cost管理APIにアクセスできません。必要に応じて `ANTHROPIC_ORGANIZATION_ID` / `ANTHROPIC_WORKSPACE_ID` も設定できます(取得結果のグルーピング用で、Admin keyが読み取れる範囲自体は変わりません)。まずは `POST /api/collect/claude?dry_run=true` を推奨します。

Anthropic側でもspend limit / usage limitを設定してください。ただしspend limit(budget)の読み取りAPIはClaude Enterprise専用と公式に明記されており、Claude Console/Platform組織では利用できないため、このCollectorはbudget値を取得しません。Claude Web版の残り利用枠は取得対象外です。

Claude Codeの5時間枠・7日枠rate limit(CLI自動取得およびClaude Desktop Cloud手動fallback)は、Anthropic公式ドキュメント上もこのUsage & Cost Admin APIとは別系統(Claude.aiサブスクリプションのセッション制限)であることを確認済みで、混同していません。詳細は [docs/claude-code-usage-bridge.md](docs/claude-code-usage-bridge.md) を参照してください。

### Collector normalized records and save policy

OpenAI / Gemini / Claude Collectorの戻り値は共通の `CollectorNormalizedRecord` 形式に揃えます。

```text
vendor
service_provider
model_name
limit_type
metric_kind
used_value
unit
recorded_at
period_start
period_end
bucket_width
source_record_id
source_type
project_id
organization_id
workspace_id
raw_label
metadata
```

`vendor` は `openai` / `gemini` / `claude`、`source_type` は `api_openai_management` / `api_gemini_management` / `api_claude_management` を使います。`metric_kind` は `usage` / `cost` / `quota` / `budget` のいずれかで、`unit` は公式レスポンスで確認できた値だけに制限された固定語彙です(`requests` / `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens` / `total_tokens` / `usd` / `quota_count`)。`unit`は対応する`metric_kind`以外では使えません(例: `usd`は`cost`/`budget`専用)。`period_start` / `period_end` はtimezone-aware datetimeで、`period_start < period_end` を必須とします。`used_value` はfloat、`recorded_at` は表示用のISO形式文字列です。

次工程の保存方針は以下です。

- `dry_run=true` は `usage_records` に保存しない(ただしvalidationとimport判定自体は実行し、結果を返す)。
- `dry_run=false` のみ実際に保存する。
- `metric_kind` が `usage` / `cost` の場合のみ保存対象。`quota` / `budget` は(将来Limitへの反映方法を別途設計するまで)`usage_records` へ保存しない。
- 保存時は `vendor + source_type + project_id/organization_id/workspace_id + model_name + limit_type + unit` の組み合わせで既存の `limit` を探す。
- 該当する `service` / `limit` がなければ、`account_type=api` のservice / limitを自動作成する。
- 同一identity(後述)で値が変わっていた場合は、新規行を追加せず既存の`usage_records`行を更新する。
- `records_saved` には実際に保存(新規作成+更新)した件数を入れる。
- 取得元は `source_type` で区別する。

### Collector import behavior

`dry_run=false` のCollector実行では、正規化済みレコードを `usage_records` に保存します。`dry_run=true` は確認モードのため書き込みませんが、同じvalidation・import判定ロジックを通し、`POST /api/collect/{vendor}` のレスポンス `outcomes` へ理由付きで結果を返します(`imported` / `updated` / `duplicate` / `unsupported_metric_kind` / `dry_run` / `invalid_record`)。`outcomes` はDBへは保存されません(集計値の`records_found`/`records_saved`のみ`collector_runs`へ保存)。

保存時にAPI用の `service` が存在しない場合は自動作成します。

```text
OpenAI  -> OpenAI API / provider=OpenAI / account_type=api
Gemini  -> Gemini API / provider=Google / account_type=api
Claude  -> Claude API / provider=Anthropic / account_type=api
```

対応する `limit` が存在しない場合も、`model_name + limit_type + unit + source_type` をもとにAPI用limitを自動作成します。作成されるlimitは `max_value=null`、`reset_interval_type=days`、`reset_interval_value=1` です。

重複防止・更新判定は `collector_imports.import_key` で行います。import_keyは `vendor / source_type / project_id / organization_id / workspace_id / model_name / limit_type / metric_kind / unit / period_start / period_end` から作成します(`used_value`は含みません)。同一import_keyの既存レコードが見つかった場合、値が同じなら何もせず(`duplicate`)、値が異なれば既存の`usage_records`行を更新します(`updated`。新規行の追加はしません)。`records_saved` は新規作成+更新を合わせた件数です。

これはChatGPT / Gemini / Claude Web版の残量保存ではなく、OpenAI / Gemini / Claude APIの利用履歴保存です。

### Usage history source labels

使用履歴には、手入力、補正、OpenAI API、Gemini API、Claude API由来のデータが混在します。データの出所は `usage_records.source_type` で判別します。

```text
manual -> 手入力
manual_adjustment -> 補正
api_openai_management -> OpenAI API
api_gemini_management -> Gemini API
api_claude_management -> Claude API
```

UIでは `source_type` をそのまま表示せず、上記の表示ラベルに変換して表示します。

### Collector UI

Dashboard画面からOpenAI / Gemini / Claude Collectorを実行できます。vendorと `dry_run` を選び、実行ボタンを押した場合のみ `POST /api/collect/{vendor}` を呼びます。ページ読み込み時にCollectorは自動実行しません。

`dry_run=true` は確認モードですが、各ベンダーの管理APIへの外部GET通信は発生します。モデル推論APIは呼びません。`dry_run=false` では取得結果を `usage_records` に保存します。`dry_run=false` を選んだ場合、UIは保存前に確認ダイアログを表示します。

Collector実行結果と最新履歴は画面に表示され、実行履歴は `collector_runs` に残ります。APIキーや認証情報は画面に表示しません。

### Browser check checklist

- 画面表示が文字化けしていない。
- Collector実行パネルが表示される。
- `dry_run=true` でCollectorを実行できる。
- `dry_run=false` で確認ダイアログが出る。
- Collector実行履歴が表示される。
- 使用履歴で手入力 / 補正 / API由来をフィルターできる。
- ダッシュボードカードに取得元が表示される。

## GitHub Actions Monthly Billing Monitor

GitHub personal account（自分のアカウント）のGitHub Actions **月間** 利用枠（分）を、通常のダッシュボードと`/compact`の両方で確認できます。既存の「GitHub API Rate Limit」カード（APIリクエスト枠、1時間ごとにリセット）とは別物です。

- **Monthly allowance**: GitHub Free — 2,000分/月、GitHub Pro — 3,000分/月（`GET /user`の`plan.name`から判定。`free`/`pro`以外・未取得の場合はPlan不明として扱い、数値を推測しません）。この値はplan名から確定できる事実です
- **消費しない利用**: publicリポジトリでのstandard GitHub-hosted runnerの利用、self-hosted runnerの利用はいずれも月間枠（private repository向けのincluded minutes）を消費しません
- **larger runner**: 常に別課金対象で、月間枠からは一切引かれません（`paid_non_included_minutes`として別表示。これは公式docsで明確に保証されています）
- **重要: Exact used / Exact remainingは表示しません** — GitHub公式Billing usage summary API（Public Preview）が返す`discountQuantity`は、「account included usageによるdiscount」だけでなく「publicリポジトリのstandard runner利用」「self-hosted runner利用」のdiscountも混在するとGitHub公式Billing reportsドキュメントに明記されており、このAPIだけでは月間枠の正確な消費量・残量を算出できません（詳細は[docs/github-actions-billing-monitor.md](docs/github-actions-billing-monitor.md)）。数値は「—」表示とし、0や推測値へ偽装しません
- 代わりに、意味を限定した参考値（`discounted_standard_minutes`＝discountされたstandard runner分、`billable_standard_minutes`＝課金対象になったstandard runner分、`paid_non_included_minutes`＝larger runner等の別課金分）を表示します
- **API**: `GET /users/{username}/settings/billing/usage/summary`（GitHub側で現在Public Preview）。個人アカウントのbilling usage取得には、credentialに"Plan: read"権限（fine-grained PATの"Plan"パーミッション、または`user` scope）が必要です。`X-GitHub-Api-Version`ヘッダーで現在の公式APIバージョンを明示的にpinしています
- 現在のcredentialにこの権限がない場合、`permission_required`状態として安全に表示されます。トークンをアプリへ保存することはありません（既存のGitHub CLI認証を再利用するのみ）
- ページ表示だけでは取得しません。「更新」ボタンを押したときだけGitHub CLI経由で取得し、以後15分間はcooldownとして再取得しません（billing情報は秒単位の更新を必要としないため）。更新失敗時は固定のsafe messageのみ表示し、backendのresponse本文を画面へ出しません

設計判断・公式ソースの詳細、および「なぜexact remainingを表示しないか」の根拠は[docs/github-actions-billing-monitor.md](docs/github-actions-billing-monitor.md)を参照してください。

## Remaining Work

- OpenAI / Gemini / Claude の管理情報Collector実装。
- Collector実行回数の永続的な日次制限。
- Basic認証より強いログイン・権限管理。
- `set` モードの実装。
