# Desktop UX Phase 2: Diagnostics / History / Analyze

既存のDashboardを、Desktop applicationとして読みやすい情報構造へ整理したものです。backend API・DB・provider連携・diagnostics engineは一切変更していません。追加した挙動はすべて、すでに取得済みのレスポンスに対するfrontend側の射影です。

対象ファイルは `static/index.html` / `static/app.js` / `static/styles.css` の3つだけです。

## 1. Diagnostics

`data-view="diagnostics"` のセクションを、次の順に並べ替えました。

```text
診断
├─ 現在の状態(Current Status Summary)
├─ GitHub API Rate Limit
├─ GitHub Actions（月間利用枠）
├─ GraphQL消費診断
│   ├─ 計測中のActivity
│   ├─ Recent Activity Sessions
│   └─ Sample Timeline
└─ Collector実行 / Collector実行履歴
```

### 現在の状態(summary strip)

`#diagnosticsSummary` は、各パネルが既に受け取ったレスポンスを並べ替えて表示するだけの射影です。新しい取得は行わず、元データに無い判定も作りません。

| 行 | 出典 | 表示 |
|---|---|---|
| GitHub API Rate Limit | `overall.status` / `overall.reason` | statusをそのまま。`fetched=false` かつ `last_known` がある場合は「最終取得値」バッジを添える |
| GitHub Actions（月間利用枠） | `status` | `plan_unknown` → 「Plan不明」、`usage_breakdown_inconclusive` → 「内訳判定不可」 |
| GraphQL消費診断 | `enabled` / `sampler_running` / `active_sessions.length` | 「無効」「計測中」「待機中」＋計測中Activity件数 |
| Codex 自動取得 | `auto_refresh_enabled` / `last_auto_refresh_error_type` | 「自動更新 有効/無効」「直近の自動更新に失敗」 |

意図的に守っている制約が3つあります。

- **searchをOverallの根拠へ足さない。** `determine_overall`（`app/github_rate_limit.py`）はcoreとgraphqlだけでOverallを決めています。summaryはその値をそのまま出し、searchを混ぜません。パネル内には根拠を明記した注記を常置しています。
- **GitHub Actionsに「正常」を作らない。** `BillingStatus` は `usage_breakdown_inconclusive` と `plan_unknown` の2値しかなく、「正常」に相当する状態が公式データ側に存在しません。UI側で合成しません。
- **Codexの `last_auto_refresh_error_type` の生トークンは画面へ出しません。** 失敗したという事実と、既存キャッシュが保持されている旨だけを示します。

### 相関の語彙

「このAgentが消費した」「このcommandが原因」といった断定は行わず、按分（proportional allocation）も行いません。

- `attribution_status` の6値（`UNATTRIBUTED` / `SINGLE_ACTIVITY_CORRELATION` / `OVERLAPPING_ACTIVITIES` / `RESET_BOUNDARY` / `COUNTER_REGRESSION` / `FETCH_FAILED`）はすべてラベルへ写します。未知値は生のenumを露出させず「不明」へ落とします。
- `fetch_status` も同様に5値（`ok` / `no_previous` / `reset_boundary` / `counter_regression` / `fetch_failed`）を写します。`fetch_failed` は `DeltaOutcome` 型エイリアスには載っていませんが、fetch失敗時にcontrollerが直接書き込むためDBには実在します。Sample Timelineの各行に「取得状態」として表示します。
- Recent Activity Sessions には `session.attribution_status` を**比較指標として出しません**。この列はbaseline sample時点で一度だけ設定され以後更新されないため、相関状態の権威あるsourceはSample Timeline側のsample単位の値です。

## 2. History

`data-view="history"` に、取得元filterに加えて期間filterと段階表示を追加しました。

- 期間: すべての期間 / 今日 / 7日 / 30日。**取得済みの配列に対するclient側の絞り込みだけ**で、期間を変えてもサーバーへの追加リクエストは発生しません。
- 段階表示: 既定30件。「もっと見る」で30件ずつ追加します。filterを変更すると先頭ページへ戻ります。
- 日時として読めない行は、期間を指定している間は範囲内だと断定できないため除外します。

### 手入力とAPI由来の意味の違い

`usage_records` は入力経路によって意味が異なります。

- 手入力・補正は常に新しい行が追加されます（`app/crud.py::add_usage`）。その時刻は「記録された時刻」です。
- Collector由来の行は、同一 `import_key` の既存行が**その場で更新**されます（`app/collectors/importer.py`）。さらに、その行の `recorded_at` は行が書かれた時刻ではなく、取り込んだ利用期間の終わり（`period_end`）です（`app/collectors/importer.py:272-280`、更新時も同じ値を書き戻します: 同 339）。`usage_records` には行自体の更新時刻を保持する列がありません（更新されるのは親の `limit.updated_at` だけです）。

そのため行ごとにラベルを分けています（手入力・補正＝「記録」、API由来＝「対象期間の終了」）。**API由来の行を「最終更新」とは呼びません** — その意味を持つ値がデータ側に存在しないからです。History全体を「イベント履歴」とも表現しません。

期間フィルタもこの日時を基準にします。API由来の行では「利用期間がその範囲内で終わったもの」を選ぶことになります。

### 作らなかったもの

取得失敗の履歴は表示しません。scheduler側の失敗はprocess-memoryにしか残らず（`app/codex_rate_limits_scheduler.py`）、cooldown/in-progressによるskipは失敗としても記録されないため、履歴として提示できる事実が存在しないからです。現在の状態としてのみ「現在の状態」パネルに出します。

## 3. Analyze（Analysis Pack）

`data-view="analyze"` の新しいビューです。手元のAIツールへ貼り付けるためのテキストを、**画面がすでに取得済みのデータだけ**から組み立てます。

```text
「Analysis Packを生成」 → readonly textareaへ表示 → 内容を確認 → 「クリップボードへコピー」
```

- 生成した直後に自動コピーはしません。
- 外部AIサービスへ自動送信しません。connector・external APIの追加もありません。
- 生成時に新しいbackend API呼び出しを行いません（`state` に保持済みのレスポンスのみを読みます）。

### 先頭の固定文言

Packの先頭には必ず次の2行が入ります。

```text
以下はtemporal correlation dataであり、exact consumer attributionではありません。
GitHub APIはconsumer別のGraphQL消費内訳を返しません。
```

### pseudonymization

Diagnostic sessionの `label` / `repository` / `pr_number` はPackへ出しません。代わりに、そのPack内だけで有効な連番 `activity_1` / `activity_2` … を割り当てます。

- 生の値からhashやfingerprintを作りません。
- 対応表をlocalStorage・DB・DOM属性のいずれにも保存しません。関数の戻り値としてのみ存在します。
- Packを作り直すと番号は変わり得ます（安定した識別子ではありません）。
- 同一Pack内では、同じsessionは常に同じ番号になります。Sample Timelineの「その瞬間activeだったActivity」もこの番号で表現します。

### allowlist

Packはallowlist方式です。列挙したキーだけを写し、値が `null` / `undefined` のものは行ごと省略します。

| 領域 | 含めるキー |
|---|---|
| Usage Allowance bucket | `provider` `product_surface` `display_name` `limit_id_origin` `plan_type` `rate_limit_reached_type` `status` `source_kind` `provenance` `observed_at` |
| Usage Allowance window | `source_slot` `window_duration_minutes` `used_percent` `remaining_percent` `resets_at` |
| Usage Allowance unavailable | `provider` `product_surface` `status` `source_kind` |
| GitHub Rate Limit resource | `resource` `status` `limit` `used` `remaining` `usage_percent` `remaining_percent` `reset_at_utc` `seconds_until_reset`（＋ `overall.status` / `overall.reason` / snapshotの `collected_at`。`fetched=false` で `last_known` を使うときは `last_known.collected_at`） |
| Actions Billing | `status` `plan_name` `included_minutes` `discounted_standard_minutes` `billable_standard_minutes` `paid_non_included_minutes` `billing_year` `billing_month` `collected_at` `skipped_unknown_skus`（`stale=true` のときは補足行） |
| Diagnostic session | `activity_N` `actor_type` `started_at` `ended_at` `status` `stop_reason` `graphql_used_start` `graphql_used_end` `graphql_delta_total` `max_valid_interval_delta` |
| Diagnostic sample | `collected_at` `graphql_used` `graphql_limit` `graphql_remaining` `graphql_reset_at` `graphql_delta` `fetch_status` `attribution_status` |

### 取得時刻・未取得・0件の区別

- `seconds_until_reset` は `collected_at` 時点から数えた相対秒数です。Packの `generated_at` 基準ではないことを補足行で明記します。Actions Billingが `stale=true` のときは、現在値として扱わないよう補足行を出します。
- Diagnostic sessions / samples の取得状態は `state.diagnosticsHistoryStatus` に保持します。起動時の取得に失敗した場合は「取得失敗（0件ではありません）」、まだ取得していない場合は「未取得（0件ではありません）」と書き、取得済みで空の場合だけ「データなし（取得済み・0件）」と書きます。
- session節の `graphql_delta_total`（期間全体の差分）/ `max_valid_interval_delta`（期間中で最大の区間差分）は、どちらもアカウント全体のGraphQLカウンタから求めた値です。Activity自身の消費量ではないことを節の冒頭に明記します。
- Sample Timelineの `active_activities` は、取得済みのsession一覧（最新20件）だけから導出します。一覧外のsessionが動いていた可能性を否定できないため、該当が無い場合も「なし」と断定せず「取得済みsession一覧内に該当なし」と書きます。

### denylist（絶対に含めないもの）

`github_login` / `github_user_id` / 生の session label / repository / pr_number / `usage_records.note` / `collector_runs.error_message` / 生の `limit_id` / cache path / app-dataのpath / raw payload / token / credential / raw exception / filesystem path。

`used_included_minutes` / `remaining_minutes` / `usage_percentage` は構造的に常に `null` であり（`app/github_actions_billing.py`）、allowlistに載せていないためフィールドごと現れません。0として出すこともありません。

Packは `usage_records` を含みません。`note` 列にはcollectorが `project_id` / `organization_id` / `workspace_id` を文字列連結して格納するため、そもそも対象外としています。

### 読み方の注意（Pack末尾に常置）

- 同時刻に複数のActivityが動いていた場合、差分をActivity間で按分することはできません。
- 時間帯が重なるActivityの `graphql_delta_total` は同じ観測を重複して含み得ます。合算しないでください。
- 取得不能(unavailable)と使用率0%は別の状態です。
- 未取得・取得失敗と0件は別の状態です。
- reset境界やカウンタ減少を検出した区間では、差分そのものが判定不可です。

## 4. clipboard

textareaが主で、コピーボタンは補助です。

- `navigator.clipboard.writeText` が利用可能: ボタンを押したときだけコピーします。
- 利用不能（`navigator.clipboard` が無い、権限が無い、Promiseがrejectされる）: textareaはそのまま残り、固定文言で手動コピーを案内します。例外は外へ投げず、ページ全体を壊しません。失敗理由（例外オブジェクト）は読まず、画面にも出しません。

WebView内でclipboard APIが使えない環境でも、textareaを選択すれば内容を取り出せます。

## 5. Accessibility

- ビュー切り替えは既存の `aria-current="page"` 方式をそのまま拡張しています（nav 5件）。
- `aria-live="polite"` は、実際に短い通知が出る `#analysisPackStatus` にだけ付けています。`#analyzeView` 側に付けるとlive regionの入れ子になり、`#diagnosticsSummarySection` に付けると、`loadAll()` が各パネルのrenderから要約を4回再描画するたびに同じ4行が読み上げられてしまうためです。
- 期間filterには `aria-label`、textareaには `aria-label` を付けています。
- 状態は色だけでなく必ず文字列でも表現します。
- `.analysis-pack-output` に既存と同じ `:focus-visible` リング（`3px solid #0b5fff`）を付けています。共通の `input/select/button` 規則はtextareaを対象にしていないためです。

## 6. 変更していないもの

backend API / schemas / DB / models / crud / provider連携 / diagnostics engine / collectors / Generic Usage Allowance contract / Desktop lifecycle / dependencies / compact UI / `start_dashboard.bat`。remote asset・CDNの追加もありません。
