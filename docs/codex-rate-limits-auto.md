# Codex App Server rate limit 自動取得（opt-in、Phase 1）

Codex CLIには公式のCodex App Server（`codex app-server --stdio`、JSON-RPC）があり、`account/rateLimits/read`というread-onlyメソッドでChatGPTプラン上のCodex利用枠（5時間枠・週次枠）を構造化データとして取得できます。この機能は、そのメソッドだけを使って利用枠を自動取得し、`/compact`と通常管理画面へ反映する仕組みです。

## 前提

- 使用するのは`account/rateLimits/read`だけです。`account/rateLimitResetCredit/consume`等のreset credit関連method、task/thread/prompt関連methodは一切呼び出しません。
- `account/rateLimits/updated`通知の継続購読、常駐App Server、時間間隔による自動バックグラウンド更新はPhase 1の対象外です。
- `~/.codex/auth.json`等の認証ファイルやOS credential storeを直接読むことはありません。App Serverが自身の既存認証（ChatGPT認証など）を内部的に使用するだけです。
- ページ表示だけではApp Serverを起動しません。管理画面の「自動取得」ボタンを押したときだけ、one-shotでApp Serverを起動します。

## 動作

1. 「自動取得」ボタンを押す
2. `codex app-server --stdio`を一時的に起動し、公式手順どおり`initialize` → `initialized`通知 → `account/rateLimits/read`を1回だけ実行
3. 成功時のみ、5時間枠・週次枠のデータをローカルキャッシュへ保存
4. 失敗時は既存キャッシュを上書きせず、固定文言のエラーメッセージだけを表示
5. App Serverプロセスは毎回確実に終了させます（stdinを閉じる→終了待ち→必要ならterminate→さらに必要ならkill）

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

- `GET /api/codex-rate-limits`: read-only。保存済みキャッシュと直近のrefresh状態を返すだけで、App Serverは起動しません。
- `POST /api/codex-rate-limits/refresh`: `account/rateLimits/read`を1回だけ実行する唯一のエンドポイントです。

## 後続Phase（今回の対象外）

- `account/rateLimits/updated`通知の継続購読
- 時間間隔による自動バックグラウンド更新、reset後の自動更新
- 複数limit id（`rateLimitsByLimitId`）対応
- credits / reset credit関連表示
