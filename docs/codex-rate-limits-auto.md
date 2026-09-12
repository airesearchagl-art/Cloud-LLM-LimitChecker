# Codex App Server rate limit 自動取得（opt-in、Phase 2: multi-bucket対応）

Codex CLIには公式のCodex App Server（`codex app-server --stdio`、JSON-RPC）があり、`account/rateLimits/read`というread-onlyメソッドでChatGPTプラン上のWork / Codex利用枠を構造化データとして取得できます。この機能は、そのメソッドだけを使って利用枠を自動取得し、`/compact`・通常管理画面・汎用Usage Allowance API（`/api/usage-allowances`）へ反映する仕組みです。

Phase 2では、App Serverが返す複数bucket表現（`rateLimitsByLimitId`）を取り込みます。契約の根拠は、OpenAI公式docs・Codex CLI 0.153.4が生成するApp Server JSON Schema・1回のread-only runtime観測の3層で、それぞれを区別して扱います。runtimeで観測したbucket名やmodel名に近い表示名は、恒久的な契約として固定しません。

## 前提

- 使用するのは`account/rateLimits/read`だけです。`account/rateLimitResetCredit/consume`等のreset credit関連method、task/thread/prompt関連methodは一切呼び出しません。
- `account/rateLimits/updated`通知は購読しません（常駐App Server方式も採りません）。one-shot取得中に届いた通知・別idの応答は読み飛ばし、snapshotの取得元として使いません。時間間隔による定期更新（10分polling）は実装済みです（下記「定期更新」参照）。
- `~/.codex/auth.json`等の認証ファイルやOS credential storeを直接読むことはありません。App Serverが自身の既存認証（ChatGPT認証など）を内部的に使用するだけです。
- ページ表示自体はApp Serverを起動しません。App Serverが起動するのは、(1) 管理画面の「今すぐ更新」ボタンを押したとき、(2) サーバー側の定期更新scheduler（後述）が周期到来したとき、のいずれかだけです。

## 動作（one-shot取得の手順）

1. 「今すぐ更新」ボタンを押す、または定期更新schedulerの周期が到来する
2. `codex app-server --stdio`を一時的に起動し、公式手順どおり`initialize` → `initialized`通知 → `account/rateLimits/read`を1回だけ実行
   - `account/rateLimits/read`はparamsを取らないため、requestは`{"method": "account/rateLimits/read", "id": 2}`で、`params` keyを含めません（公式のrequest例と生成schemaの`params: null`に合わせています）
3. 成功時のみ、legacy表示用の5時間枠・週次枠と、汎用表示用のcanonical bucketsをローカルキャッシュへ保存（下記「応答の2つのview」参照）
4. 失敗時は既存キャッシュを上書きせず、固定文言のエラーメッセージだけを表示
5. App Serverプロセスは毎回確実に終了させます（stdinを閉じる→終了待ち→必要ならterminate→さらに必要ならkill）

## 定期更新（10分間隔、server-side scheduler）

- FastAPIのlifespanに統合されたbackground taskとして実装しています（`app/codex_rate_limits_scheduler.py`）。ブラウザの`setInterval`には依存しないため、ブラウザを閉じていてもサーバー稼働中は更新され続けます。
- 既存のone-shot adapter（`app/codex_rate_limits_adapter.py`）・cache（`app/codex_rate_limits_cache.py`）・排他制御（`app/codex_rate_limits_state.py`の`CodexRateLimitsController`）をそのまま再利用します。schedulerはJSON-RPC解析・cache形式変換・認証処理・stdout/stderr保存・reset credit操作を一切行いません。
- 手動更新（「今すぐ更新」ボタン）と定期更新は同じcontrollerの排他制御を通るため、同時実行しません。手動実行中は定期更新側が黙ってskipし、定期更新実行中の手動POSTは既存の`already_refreshing`（429）として扱われます。skipはエラーとしてcacheへ保存されません。
- 取得失敗（process起動失敗・timeout・authentication unavailable・protocol error・invalid response等）が発生してもbackground task自体は終了せず、次の周期で再試行します。既存cache・手動fallbackはそのまま維持されます。

### 環境変数

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `CLOUD_LLM_CODEX_AUTO_REFRESH_ENABLED` | `true` | `0` / `false` / `no` / `off`（大小文字無視）で無効化。それ以外の値はすべて有効として扱う |
| `CLOUD_LLM_CODEX_AUTO_REFRESH_SECONDS` | `600`（10分） | 更新間隔（秒）。60秒未満の値は60秒へ補正、数値として解釈できない値は既定値600秒へfallback |

設定値・環境変数の内容自体はログへ出力しません。

### 初回更新のタイミング

- サーバー起動直後には即実行しません。
- 既存の自動取得cacheが新鮮（利用可能かつstaleでない）な場合は、次の10分周期まで待ちます。
- cacheが存在しない、またはstaleな場合は、起動から30秒後（60秒以内）に初回取得を行います。

### single-worker前提

- schedulerはprocess-localです。このローカルアプリはsingle-worker運用を前提としています。
- `uvicorn --workers 2`以上で起動した場合、workerごとに独立したschedulerが起動し、それぞれが未協調のままAPI Serverを起動するため、重複取得の可能性があります。分散lockやDB lockは実装していません。single-worker運用を前提とし、複数worker対応は非対応と明記します。

## 保存先

Windows環境では以下に保存されます（手動入力snapshot・Claude Code Usageキャッシュとはいずれも別ファイルです）。

```text
%LOCALAPPDATA%\Cloud-LLM-LimitChecker\codex-rate-limits.json
```

## 応答の2つのview

`account/rateLimits/read`の結果には、同じ利用枠を表す2つのviewがあります。

- `rateLimits`: 公式にbackward-compatibleな単一bucket view（従来のpayloadと同じ形）
- `rateLimitsByLimitId`: 公式のmulti-bucket view。keyはmeteredな`limit_id`で、nullの場合があります

各bucket（`RateLimitSnapshot`）は`primary` / `secondary`の2つのwindow slotを持ちます。slot名は構造上の位置にすぎず、**`primary`が5時間枠とは限りません**（週次枠が`primary`に入り`secondary`がnullの形も観測されています）。`resetsAt`はUnix秒です。

### legacy view（`five_hour` / `weekly`）

`GET /api/codex-rate-limits`と既存Dashboardが表示する値です。`rateLimits`だけから、current mainと同じ規則で作ります。

- nullのwindow、型・範囲が不正なwindow、300分・10080分のどちらでもないdurationのwindowは、エラーにせず捨てます
- `primary` / `secondary`の位置ではなく`windowDurationMins`（300分=5時間枠、10080分=週次枠）で割り当てます
- 2つのwindowが同じ枠に割り当たる場合は`ambiguous_response`として取得全体を失敗にします
- 認識できる有効なwindowが1つもない場合は`invalid_response`として取得全体を失敗にします
- 数値の許容範囲も従来どおりです（`usedPercent` / `resetsAt`はint・floatを受け付け、boolは拒否）

### canonical buckets（汎用表示用）

`/api/usage-allowances`が使う値です。1回の応答から、次のどちらか一方だけを選びます（両方を使うことはありません）。

- `rateLimitsByLimitId`が**空でない**場合: mapの各要素だけがbucketです。`rateLimits`は同じ利用枠のbackward-compatibleな単一bucket viewなので、汎用表示へ**重複して追加しません**
  - 公式schemaの説明は「historical payloadをmirrorする」までです。1回のruntime観測では`rateLimits`がmapの1要素と同じ内容でしたが、これは観測に基づく前提で、常に一致することまでは確認していません（両者の一致は検査しません）
  - bucketのidはmap keyです（`limit_id_origin`は`map_key`）。空文字列のkeyは不正として扱います。要素自身の`limitId`はnullかmap keyと一致する必要があり、食い違う場合は取得全体を失敗にします
  - 生成schemaの型に厳密に合わせて検証します: `usedPercent`はint32（0〜100、boolとfloatは`42.0`でも拒否）、`windowDurationMins`はint64の範囲の整数またはnull、`resetsAt`は整数またはnull（どちらもboolとfloatは拒否）、`limitName` / `planType` / `rateLimitReachedType`は文字列またはnull
  - `resetsAt`の負の値・UTC日時に変換できない値は拒否します（schemaに下限はありませんが、負のUnix秒の扱いがOSによって異なるため、安全側で拒否しています）
  - nullでない不正なwindowや要素が1つでもあれば、そのwindowだけを捨てて正常に見せることはせず、取得全体を`invalid_response`にします。この場合`rateLimits`へfallbackもしません
  - 両windowがnullの要素は、windowを持たないmetadataのみのbucketとして有効です。300分・10080分以外のdurationもそのまま保持します
- `rateLimitsByLimitId`が**null・欠落・空**の場合: `rateLimits`を1つのbucketにします。このpathはlegacy viewと同じ規則で扱い、Phase 2の厳密な検証を持ち込みません
  - windowはlegacy viewが受け付けたものだけです（実際のslot名付き）
  - `limitId`が空でない文字列ならそれをidにし（`limit_id_origin`は`snapshot_field`）、なければidはnullです。idを生成することはありません
  - 文字列でないmetadataはnullとして捨てます（従来はmetadataを読んでいなかったため、これで取得を失敗させません）
- `rateLimitsByLimitId`がobjectでもnullでもない場合は、取得全体を失敗にします

どちらのpathでも、各windowは実際の`source_slot`（`primary` / `secondary`）を保持し、slot名からdurationを推測しません。`remaining_percentage`はsourceに直接のfieldがないため、同じwindowの同じ観測の`100 - usedPercent`としてだけ導出します（別sourceや過去値との合成、clampはしません）。

### 互換性ガード（Phase 2の暫定仕様）

`rateLimitsByLimitId`が空でなく妥当でも、legacy viewの割り当てで`ambiguous_response`になる場合や、`rateLimits`から300分・10080分のwindowが1つも作れない場合は、取得全体を失敗にし、既存のキャッシュを上書きしません。`/api/codex-rate-limits`と既存Dashboardが「成功なのに表示する枠がない」状態にならないようにするためです。

これは恒久的な汎用契約ではありません。将来、表示を汎用Usage Allowance UIへ移した後は、canonical bucketsの成功とlegacy viewの可用性を分けて扱う候補です（下記「将来案」参照）。

## 保存するフィールド

キャッシュの書き込みは`schema_version: 2`です。読み込みは`1`と`2`の両方を受け付けます（v1キャッシュは次に取得が成功した時点でv2に置き換わります）。

- `schema_version` / `source`（固定値 `codex_app_server`）
- `observed_at`
- `five_hour` / `weekly`（legacy view。それぞれ`used_percentage` / `remaining_percentage` / `resets_at` / `window_duration_minutes`）
- `buckets`（v2のみ。canonical buckets。1件以上）
  - `limit_id` / `limit_id_origin`（`map_key` / `snapshot_field` / null）/ `display_name`（`limitName`）/ `plan_type` / `rate_limit_reached_type`
  - `windows`: `source_slot` / `used_percentage` / `remaining_percentage` / `resets_at`（nullあり）/ `window_duration_minutes`（nullあり）

`plan_type`・`rate_limit_reached_type`などは閉じたenumとして固定せず、未知の文字列もそのまま保持します。どちらのviewからbucketを作ったかを示すmarkerは保存しません（`limit_id_origin`で分かるため）。なお`remaining_percentage`は`used_percentage`からの導出値で、mapがないv2キャッシュでは`five_hour` / `weekly`と`buckets`のwindowが同じ値を持ちます（legacy表示用と汎用表示用で役割が異なるためです）。キャッシュを読み込むときも許可リストのfieldだけを取り出すため、手で書き換えたキャッシュに余分なfieldがあっても表示されません。

## 保存しないもの・表示しないもの

- `account/rateLimits/read`のresponse全体（raw payload）
- `accountId`、account情報、email、user id、organization、session id、thread id、token、認証情報、stdout/stderr
- `credits`（`balance`を含む）、`rateLimitResetCredits`（reset creditの詳細を含む）
- `rateLimitUpsell`、`individualLimit`、`spendControlReached`（spend control関連）

これらは読み取りもせず、キャッシュ・APIのどちらにも出しません。また、このドキュメントには実アカウントの利用率や、アカウント固有のbucket値を記載しません。

## fallback（手動snapshotとの関係）

自動取得cacheと手動入力snapshotは1つのファイルへ統合しません。表示側では以下の優先順位で選びます。

1. 自動cacheが利用可能（staleでも）: 自動取得値を表示（バッジ「自動取得」、staleなら「最終自動取得値」）
2. 自動cacheが利用不可・手動snapshotが利用可能: 手動snapshotへfallback表示（バッジ「手動確認値」）
3. どちらも利用不可: 「自動取得または手動入力してください」

自動取得が成功しても、既存の手動入力snapshot（`app/codex_usage_cache.py`、`codex-usage.json`）は削除・変更しません。

## staleの基準

- 自動取得cache: 15分（Claude Code Usageの15分と同じ考え方だが別定数。手動snapshotの24時間とは異なる）
- reset時刻を過ぎている場合も、新しい自動取得がなければstale扱いにし、古いpercentageを現在値のように表示しません

## クールダウン

手動更新ボタンにはprocess-localな30秒クールダウンがあります（成功・失敗どちらの試行も対象）。プロセス再起動でクールダウン状態は消えます（許容している制約です）。

## API

- `GET /api/codex-rate-limits`: read-only。保存済みキャッシュ・直近のrefresh状態・定期更新schedulerの状態を返すだけで、App Serverは起動しません。
  - response shapeは従来どおりです。v1・v2どちらのキャッシュでも、legacy viewの`five_hour` / `weekly`だけを返し、canonical bucketsからlegacy viewを組み立て直すことはありません
  - 定期更新関連の追加フィールド: `auto_refresh_enabled` / `auto_refresh_interval_seconds` / `auto_refresh_running` / `next_auto_refresh_at` / `last_auto_refresh_attempt_at` / `last_auto_refresh_success_at` / `last_auto_refresh_error_type`（いずれもtimezone-aware ISO 8601文字列またはbool/int/null。`last_auto_refresh_error_type`は固定文言のerror_typeのみで、内部例外メッセージは含みません）
  - これらはprocess-localな状態です（複数workerでは共有されません、上記「single-worker前提」参照）
- `POST /api/codex-rate-limits/refresh`: `account/rateLimits/read`を1回だけ実行する唯一の即時実行エンドポイントです（定期更新とは別に、手動で今すぐ実行したい場合に使います）。
- `GET /api/usage-allowances`: 汎用Usage Allowance read model。Codex自動取得分は、v2キャッシュではcanonical bucketごとに1件ずつ出します（legacy viewを重複して追加しません）。各bucketは`limit_id` / `limit_id_origin` / `display_name` / `plan_type` / `rate_limit_reached_type`、実際の`source_slot`付きのwindow、bucket自身のwindowのreset時刻も考慮した`status`（`ok` / `stale`）を持ちます。v1キャッシュでは従来（Phase 1）どおり、metadataをnullとした1件を出します。

## 将来案として検討する内容（対象外）

- `account/rateLimits/updated`通知の継続購読による、利用率変化に近いタイミングでのpush型更新
  - ただし常駐App Server、再接続処理、プロセス監視、認証切れハンドリング、shutdown処理、通知の重複処理が別途必要になります
  - 現状は10分pollingを採用し、運用上それで十分かを確認したうえでpush方式への移行を判断します
- 汎用Usage Allowance UIへの移行後、canonical bucketsの取得成功とlegacy view（`five_hour` / `weekly`）の可用性を分離すること（上記「互換性ガード」の解除候補）
- credits / reset credit関連表示
- 複数worker対応（分散lock等）
- reset時刻ぴったりの更新、使用率変化検知による即時更新
- Windows service化・system tray常駐
