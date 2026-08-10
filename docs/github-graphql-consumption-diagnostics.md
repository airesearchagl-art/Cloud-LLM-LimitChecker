# GitHub GraphQL Consumption Diagnostics (v0.1)

- 対象: `app/github_graphql_diagnostics*.py`、`app/models.py`（`GitHubDiagnosticSession`/`GitHubRateSample`）、`app/crud.py`/`app/schemas.py`の追加分、`app/main.py`の`/api/github-graphql-diagnostics*`系routes、`static/app.js`/`static/compact.js`のDiagnostics UI、`app/github_graphql_diagnostics_export.py`
- 実施していないこと: GitHub GraphQL APIへの呼び出し（`gh api graphql`/`/graphql`リクエストのいずれも一切実装していない）、実credentialでのlive確認、DB migration（既存の`Base.metadata.create_all`方式にそのまま乗る2テーブル追加のみ）

## 1. 目的と非目的

既存の`GitHub API Rate Limit`監視（`app/github_rate_limit*.py`）は`GET /rate_limit`のcore/graphql/search各resourceのスナップショットを表示するのみで、「何が消費したか」は一切示さない。本機能は、ユーザーが「Claude Codeでレビュー中」のような**activity session**を開始/終了しながら`graphql` resourceを定期的に再サンプリングすることで、GraphQL primary quotaの消費と、その時間帯にactiveだったactivityとの**時間的相関(temporal correlation)**を目視できるようにする。

**非目的（絶対に行わないこと）**:
- GitHubの`GET /rate_limit`は、どのclient/token/processがGraphQLを消費したかのconsumer別内訳を一切返さない。したがって本機能は「Xが消費した」「confirmed」「exact」という断定を一切行わない。
- 2つ以上のactivity sessionが重複していた場合の、delta値の按分（比例配分）・推測配分は一切行わない。該当サンプルは`OVERLAPPING_ACTIVITIES`として、個別内訳なしで記録される。
- 実装のいかなる箇所からも、GitHub GraphQL API（`/graphql`エンドポイント、`gh api graphql`）を呼び出さない。

## 2. GitHub API境界

本機能が呼び出すGitHub関連コマンドは以下の2つのみ、いずれもREST:

- `gh api rate_limit`（既存の`app/github_rate_limit_cli.py::fetch_github_rate_limit`をそのまま再利用。新規呼び出しコードは書いていない）
- `gh api user`（新規、`app/github_graphql_diagnostics_identity.py::fetch_github_identity`。Session開始時に1回だけ呼び、レスポンスから`login`と数値`id`の2フィールドのみを抽出し、他の全フィールド（email/name/avatar_url等）は即座に破棄する。他のいかなる場所にも保存・ログ出力しない）

`GET /rate_limit`自体はGitHub公式上REST primary quotaを消費しないが、secondary rate limitの対象にはなり得る。そのため、本機能はDiagnostic Mode（`GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED=true`）が有効かつ、少なくとも1つのactivity sessionがACTIVEな間だけ定期サンプリングを行う。無効時・active sessionが0件の時は、このための追加pollingを一切行わない（既存のGitHub Rate Limit監視自体の挙動は無変更）。

## 3. Config

| env var | 既定値 | 説明 |
|---|---|---|
| `GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED` | `false`（**opt-in**） | 本機能全体の有効/無効。Codexの自動更新schedulerとは異なり、本機能は既存cacheの読み取りではなく`gh api rate_limit`への**追加**呼び出しを発生させるため、既定で無効 |
| `GITHUB_GRAPHQL_DIAGNOSTIC_SAMPLE_SECONDS` | `10` | サンプリング間隔（秒）。`5`秒未満は`5`秒へclamp（GitHub公式が定めた値ではなく、本アプリのoperational safety floor） |
| `GITHUB_GRAPHQL_DIAGNOSTIC_MAX_MINUTES` | `15` | 1 sessionの最大継続時間（分）。到達時は自動的に`AUTO_STOPPED`（`stop_reason=MAX_DURATION`） |

いずれも不正な値は既定値へfallback（例外を投げない）。`docs`にのみ明記されたapp独自のoperational defaultであり、GitHub公式の推奨値ではない。

## 4. Delta / Reset / Counter Regression semantics

`app/github_graphql_diagnostics.py`（pure domain、`gh`呼び出し・DB・network I/Oを一切含まない）が唯一の計算ロジック。

- **同一reset windowかつ`current.used >= previous.used`のときのみ** `delta = current.used - previous.used`を計算する（`delta=0`も有効な値として区別し、`None`にはしない）。
- **reset windowを跨いだ場合**（`previous.reset_at_utc != current.reset_at_utc`）: `delta=None`、`fetch_status="reset_boundary"`。異なるreset window同士の`used`値を減算しない。
- **同一reset windowでcounter減少を検出した場合**（`current.used < previous.used`）: `delta=None`、`fetch_status="counter_regression"`。原因（credential/account context変化、provider側の不整合等）は一切推測しない。
- Sessionの`graphql_delta_total`（Session全体のobserved delta）も同じ規約に従う: **sessionがreset windowを跨いだ場合は`None`**（spec section 8）。異なるreset window同士の合計を1つの「Session消費量」として合算しない。UIでは「reset境界を跨いだためSession総差分は判定不可」と表示する。

## 5. Attribution semantics

サンプル取得instant時点でACTIVEなactivity session数から分類する（`classify_attribution`）:

| active session数 | attribution_status |
|---|---|
| 0 | `UNATTRIBUTED` |
| 1 | `SINGLE_ACTIVITY_CORRELATION` |
| 2以上 | `OVERLAPPING_ACTIVITIES` |

（reset境界検出時は`RESET_BOUNDARY`、counter減少検出時は`COUNTER_REGRESSION`、fetch失敗時は`FETCH_FAILED`が優先される）

`SINGLE_ACTIVITY_CORRELATION`であっても「そのsessionが確実に消費した」ことを意味しない — 同じtokenを使う別のprocess/ブラウザタブ/CI等が同時にGraphQLを呼んでいた可能性は排除できない。`OVERLAPPING_ACTIVITIES`の場合、delta値をsession間で按分・推測配分することは一切行わない（該当するコードも存在しない）。

## 6. Sampling architecture（process-wide、session単位ではない）

`GitHubGraphQLDiagnosticsController`が**process内で単一のsampler instance**（`GitHubGraphQLDiagnosticsSampler`）を保持する。sessionごとに個別のsampling taskを作ることはない。active session数が0→1になった時にsamplerを起動、1→0になった時に停止する。複数sessionが同時にactiveでも、samplingは設定した間隔（既定10秒）につき1回のみ`gh api rate_limit`を呼ぶ（N sessions × pollingにはならない）。

samplesは**1本のglobal timeline**であり、`trigger_session_id`は「どのsessionのstart/stop操作がこの即時サンプルを発火させたか」の補助情報にすぎない。samplesとsessionの相関は常に`sample.collected_at`と`session.started_at`/`ended_at`の比較から都度導出し、所有関係としては一切扱わない。

## 7. Session lifecycle / 自動遷移

- **開始** (`POST /api/github-graphql-diagnostics/start`): config有効確認 → `gh api user`で`login`/数値`id`のみ取得 → 既存ACTIVE sessionが別GitHubアカウントに属する場合は`ACCOUNT_CONTEXT_CHANGED`としてstart自体を拒否（既存sessionには一切触れない） → baseline sample取得・記録 → session作成 → sampler起動。
- **終了** (`POST /api/github-graphql-diagnostics/{id}/stop`): ACTIVEなsessionのみ対象。final sample取得・記録、`graphql_delta_total`計算、`status=STOPPED`/`stop_reason=USER_STOP`。既に終了済みのsessionへの再呼び出しはidempotentに成功を返すが、新しいsampleは作らない。active session数が0になればsampler停止。
- **MAX_DURATION到達**: 該当sessionを`AUTO_STOPPED`/`stop_reason=MAX_DURATION`へ自動遷移。
- **GraphQL残量0** (`graphql.remaining == 0`): 全ACTIVE sessionを`EXHAUSTED`/`stop_reason=GRAPHQL_EXHAUSTED`へ自動遷移。既にremaining=0の状態でstartした場合、そのsessionは最初から`EXHAUSTED`として作成される（ACTIVEにはしない）。
- **プロセス再起動**: 起動時、DBに残っている`status=ACTIVE`のsessionは全て`ABORTED`/`stop_reason=PROCESS_RESTART`へ強制的にreconcileされる（`graphql_used_end`/`graphql_delta_total`は`None`のまま — 旧processのcontextで最終sampleを捏造しない）。再起動後にsamplerが自動的に再開することはない（新しいstart呼び出しがあって初めて起動する）。

## 8. 既知のv0.1の制約

- **自動遷移でactive setが空になってもsamplerは自分自身を停止しない**: scheduled tickの中で`AUTO_STOPPED`/`EXHAUSTED`遷移が発生してactive sessionが0件になっても、そのtick自身の中からsamplerを停止しない（自分が実行中のcoroutineを自己参照的にcancel/awaitするデッドロックリスクを避けるため）。その後のtickは単に`UNATTRIBUTED`のglobal sampleを記録し続け、無害だが無駄ではある。明示的な`stop_session`呼び出し（active数を0にする）またはプロセス再起動でのみ実際にsamplerが止まる。
- **Sessionの`attribution_status`列はbaseline sample時点で1回だけ設定され、以後更新されない**: session全体の履歴を通じた正確なattributionは常に個々の`GitHubRateSample`行を参照する必要がある。
- `graphql_delta_total`は「observed delta」であり、正確な「消費量」ではない（前提となる`used`値そのものがconsumer別内訳を持たないため）。

## 9. DB永続化

新規テーブル: `github_diagnostic_sessions`、`github_rate_samples`（`Base.metadata.create_all`により起動時に自動作成、Alembic等のmigrationは使用していない）。credential/token/raw authorization header/raw `gh` stderr・stdoutはいずれも一切保存しない。

## 10. API

| method | path |
|---|---|
| GET | `/api/github-graphql-diagnostics` |
| POST | `/api/github-graphql-diagnostics/start` |
| POST | `/api/github-graphql-diagnostics/{session_id}/stop` |
| GET | `/api/github-graphql-diagnostics/samples?limit=&offset=` |
| GET | `/api/github-graphql-diagnostics/sessions?limit=&offset=` |
| GET | `/api/export/github-graphql-samples.csv` |
| GET | `/api/export/github-graphql-sessions.csv` |

`limit`は`[1, 500]`へ常にclampされ、無制限の全件取得はできない。既存のOptional Basic Auth middleware（`ENABLE_BASIC_AUTH`）がこれらのendpoint全てへ自動的に適用される（このfeature専用の別認証は実装していない）。

## 11. CSV Security

`label`/`repository`（ユーザー入力由来）を含むCSVは、Excel/Google Sheets等のformula injection対策として、セル値が`=`/`+`/`-`/`@`で始まる場合に先頭へ`'`を付与してからexportする（`app/github_graphql_diagnostics_export.py::safe_csv_cell`）。既存の`app/exporter.py`（この対策を持たない）は変更していない — 本機能専用の別ヘルパーとして実装した。

## 12. UI

Main dashboardに`GraphQL消費診断`パネルを追加（既存の`GitHub API Rate Limit`/`GitHub Actions`パネルと同じ配置規約）。開始フォーム・ACTIVE sessionカード・最終観測sample行を表示。Compact dashboardには既存`section.github`カード群へ「有効/無効」「計測中N件」のみを示す最小indicatorカード（`github.graphql-diagnostics`）を追加（v0.1では詳細timeline・per-session内訳は表示しない）。いずれのUIテキストも「観測された差分」「相関候補」等の非断定的な表現のみを用い、「Xが消費しました」「confirmed」「exact」は一切使用しない。
