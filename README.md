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

Gemini / Claude のCollector APIは現時点では外部APIを呼ばない雛形です。OpenAI Collectorを含む実Collector実装時は、usage / costs / billing / quota / rate limit 系の管理情報取得APIに限定し、モデル推論APIは呼ばないでください。

### OpenAI Collector

OpenAI Collectorはusage/costなどの管理情報取得専用です。モデル推論API、画像生成、音声、embedding、moderationなどの生成・推論系APIは呼びません。実行には以下が必要です。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_OPENAI_COLLECTOR=true
OPENAI_API_KEY=...
ALLOW_PAID_MODEL_CALLS=false
```

`ALLOW_PAID_MODEL_CALLS=false` のまま使います。まずは `POST /api/collect/openai?dry_run=true` を推奨します。初回実装では取得結果の正規化と `collector_runs` への記録までを行い、`usage_records` への保存はまだ行いません。そのため `records_saved=0` です。

OpenAI側でもspend cap / budget limitを設定してください。ChatGPT Web版の残メッセージ数やPlus/ProのWeb利用枠はOpenAI APIのusage/cost Collectorでは取得対象外です。

### OpenAI Collector dry-run and permissions

`dry_run=true` は `usage_records` に保存しない確認モードです。ただし、OpenAI の usage/costs 管理APIへの `GET` 通信は発生します。モデル推論APIやトークン課金対象の生成APIは呼びません。外部通信自体を避けたい場合は、Collectorを実行しないでください。

実行前にOpenAI側でspend cap / budget limitを設定してください。usage / costs 系APIは通常のAPIキーでは取得できない場合があり、organization / project の権限が必要になることがあります。OpenAI管理APIから `401` / `403` が返った場合、このアプリではOpenAI管理APIエラーとして扱い、`collector_runs.error_message` に権限確認が必要な旨を記録します。

### Gemini Collector

Gemini Collectorはusage / billing / quota / rate limit / project usage などの管理情報取得専用です。生成API、画像生成、動画生成、音声生成、grounding/search/tool系の生成処理は呼びません。

実行には以下のいずれかの認証情報が必要です。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_GEMINI_COLLECTOR=true
GEMINI_API_KEY=...
# または
GOOGLE_CLOUD_ACCESS_TOKEN=...
GOOGLE_CLOUD_PROJECT=...
ALLOW_PAID_MODEL_CALLS=false
```

まずは `POST /api/collect/gemini?dry_run=true` を推奨します。`dry_run=true` は `usage_records` に保存しない確認モードですが、管理情報取得APIへの外部GET通信は発生します。外部通信自体を避けたい場合はCollectorを実行しないでください。

初回実装では取得結果の正規化と `collector_runs` への記録までを行い、`usage_records` への保存はまだ行いません。そのため `records_saved=0` です。Google Cloud側でもbudget / quota / alertを設定してください。Gemini Web版の残り利用枠は取得対象外です。

### Claude Collector

Claude Collectorはusage / billing / costs / limits / organization usage / workspace usage などの管理情報取得専用です。Messages API、streaming Messages、tool use付きMessages、prompt cachingを含むモデル推論は呼びません。

実行には以下の認証情報が必要です。

```text
ENABLE_VENDOR_COLLECTORS=true
ENABLE_CLAUDE_COLLECTOR=true
ANTHROPIC_API_KEY=...
ALLOW_PAID_MODEL_CALLS=false
```

必要に応じて `ANTHROPIC_ORGANIZATION_ID` / `ANTHROPIC_WORKSPACE_ID` も設定できます。まずは `POST /api/collect/claude?dry_run=true` を推奨します。`dry_run=true` は `usage_records` に保存しない確認モードですが、管理情報取得APIへの外部GET通信は発生します。外部通信自体を避けたい場合はCollectorを実行しないでください。

初回実装では取得結果の正規化と `collector_runs` への記録までを行い、`usage_records` への保存はまだ行いません。そのため `records_saved=0` です。Anthropic側でもspend limit / usage limitを設定してください。Claude Web版の残り利用枠は取得対象外です。

### Collector normalized records and save policy

OpenAI / Gemini / Claude Collectorの戻り値は共通の `CollectorNormalizedRecord` 形式に揃えます。

```text
vendor
service_provider
model_name
limit_type
used_value
unit
recorded_at
source_type
project_id
organization_id
workspace_id
raw_label
metadata
```

`vendor` は `openai` / `gemini` / `claude`、`source_type` は `api_openai_management` / `api_gemini_management` / `api_claude_management` を使います。`used_value` はfloat、`recorded_at` はISO形式文字列です。

次工程の保存方針は以下です。

- `dry_run=true` は `usage_records` に保存しない。
- `dry_run=false` のみ保存対象にする。
- 保存時は `vendor + project_id/workspace_id + model_name + limit_type + unit` の組み合わせで既存の `limit` を探す。
- 該当する `service` / `limit` がなければ、`account_type=api` のservice / limitを自動作成するか、手入力待ちとして扱う。
- 同じ `vendor / model_name / limit_type / recorded_at` の重複保存を防ぐ。
- `records_saved` には実際に保存した件数を入れる。
- 取得元は `source_type` で区別する。

この段階では保存仕様の固定とバリデーションまでで、`usage_records` への保存実装はまだ行いません。

### Collector import behavior

`dry_run=false` のCollector実行では、正規化済みレコードを `usage_records` に保存します。`dry_run=true` は確認モードのため保存しません。

保存時にAPI用の `service` が存在しない場合は自動作成します。

```text
OpenAI  -> OpenAI API / provider=OpenAI / account_type=api
Gemini  -> Gemini API / provider=Google / account_type=api
Claude  -> Claude API / provider=Anthropic / account_type=api
```

対応する `limit` が存在しない場合も、`model_name + limit_type + unit + source_type` をもとにAPI用limitを自動作成します。作成されるlimitは `max_value=null`、`reset_interval_type=days`、`reset_interval_value=1` です。

重複保存は `collector_imports.import_key` で防ぎます。import_keyは `vendor / source_type / project_id / organization_id / workspace_id / model_name / limit_type / unit / recorded_at` から作成します。`records_saved` は重複を除いた新規保存件数です。

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

## Remaining Work

- OpenAI / Gemini / Claude の管理情報Collector実装。
- Collector実行回数の永続的な日次制限。
- Basic認証より強いログイン・権限管理。
- `set` モードの実装。
