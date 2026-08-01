# Codex App Server rate limit 自動取得（opt-in、Phase 1）

Codex CLIには公式のCodex App Server（`codex app-server --stdio`、JSON-RPC）があり、`account/rateLimits/read`というread-onlyメソッドでChatGPTプラン上のCodex利用枠（5時間枠・週次枠）を構造化データとして取得できます。この機能は、そのメソッドだけを使って利用枠を自動取得し、`/compact`と通常管理画面へ反映する仕組みです。

## 前提

- 使用するのは`account/rateLimits/read`だけです。`account/rateLimitResetCredit/consume`等のreset credit関連method、task/thread/prompt関連methodは一切呼び出しません。
- `account/rateLimits/updated`通知の継続購読、常駐App Server（プロセスを起動しっぱなしにする方式）はPhase 1の対象外です。時間間隔による定期更新（10分polling）はPhase 1で実装済みです（下記「定期更新」参照）。
- `~/.codex/auth.json`等の認証ファイルやOS credential storeを直接読むことはありません。App Serverが自身の既存認証（ChatGPT認証など）を内部的に使用するだけです。
- ページ表示自体はApp Serverを起動しません。App Serverが起動するのは、(1) 管理画面の「今すぐ更新」ボタンを押したとき、(2) サーバー側の定期更新scheduler（後述）が周期到来したとき、のいずれかだけです。

## 動作（one-shot取得の手順）

1. 「今すぐ更新」ボタンを押す、または定期更新schedulerの周期が到来する
2. `codex app-server --stdio`を一時的に起動し、公式手順どおり`initialize` → `initialized`通知 → `account/rateLimits/read`を1回だけ実行
3. 成功時のみ、5時間枠・週次枠のデータをローカルキャッシュへ保存
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
- `uvicorn --workers 2`以上で起動した場合、workerごとに独立したschedulerが起動し、それぞれが未協調のままAPI Serverを起動するため、重複取得の可能性があります。分散lockやDB lockはPhase 1では実装していません。single-worker運用を前提とし、複数worker対応は非対応と明記します。

## 保存先

Windows環境では以下に保存されます（手動入力snapshot・Claude Code Usageキャッシュとはいずれも別ファイルです）。

```text
%LOCALAPPDATA%\Cloud-LLM-LimitChecker\codex-rate-limits.json
```

## 保存するフィールド

- `schema_version` / `source`（固定値 `codex_app_server`）
- `observed_at`
- `five_hour` / `weekly`（それぞれ`used_percentage` / `remaining_percentage` / `resets_at` / `window_duration_minutes`）

`primary` / `secondary`という応答上の位置ではなく、`windowDurationMins`（300分=5時間枠、10080分=週次枠）で機械的に判定します。

## 保存しないもの

- `account/rateLimits/read`のresponse全体
- `rateLimitsByLimitId`（複数limit id対応はPhase 1の対象外）
- `rateLimitResetCredits`（credits・balance・reset creditはPhase 1の対象外）
- account情報、email、account id、user id、organization、session id、thread id、token、stdout/stderr

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
  - 定期更新関連の追加フィールド: `auto_refresh_enabled` / `auto_refresh_interval_seconds` / `auto_refresh_running` / `next_auto_refresh_at` / `last_auto_refresh_attempt_at` / `last_auto_refresh_success_at` / `last_auto_refresh_error_type`（いずれもtimezone-aware ISO 8601文字列またはbool/int/null。`last_auto_refresh_error_type`は固定文言のerror_typeのみで、内部例外メッセージは含みません）
  - これらはprocess-localな状態です（複数workerでは共有されません、上記「single-worker前提」参照）
- `POST /api/codex-rate-limits/refresh`: `account/rateLimits/read`を1回だけ実行する唯一の即時実行エンドポイントです（定期更新とは別に、手動で今すぐ実行したい場合に使います）。

## 将来案として検討する内容（Phase 1の対象外）

- `account/rateLimits/updated`通知の継続購読による、利用率変化に近いタイミングでのpush型更新
  - ただし常駐App Server、再接続処理、プロセス監視、認証切れハンドリング、shutdown処理、通知の重複処理が別途必要になります
  - Phase 1では10分pollingを採用し、運用上それで十分かを確認したうえでpush方式への移行を判断します
- 複数limit id（`rateLimitsByLimitId`）対応
- credits / reset credit関連表示
- 複数worker対応（分散lock等）
- reset時刻ぴったりの更新、使用率変化検知による即時更新
- Windows service化・system tray常駐
