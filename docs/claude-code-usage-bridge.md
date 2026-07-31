# Claude Code Usage statusLine bridge(opt-in）

Claude Code公式の`statusLine`フック(公式ドキュメント: https://code.claude.com/docs/en/statusline.md )が渡すJSONペイロードから、契約上の利用率(5時間枠・7日枠のrate limit)だけを抽出してローカルキャッシュへ保存する仕組みです。

このリポジトリが自動で `~/.claude/settings.json` を書き換えることはありません。有効化するかどうか、既存の`statusLine`設定とどう共存させるかは、必ずユーザー自身が判断してください。

## 前提・制約

- `statusLine`はClaude Codeが**能動的に呼び出すpush型**の仕組みです。任意のタイミングでこちらから取得しにいくAPIではありません。
- そのため、キャッシュの値は常に「最終観測値」です。Claude Codeセッションが動いていない間は値が更新されません。
- 公式に確認できているのは Claude.ai の **Pro / Max** プランです。Team等他プランでの`rate_limits`フィールドの存在は、実環境で確認できるまで断定しません。
- セッション内で最初のAPI応答が返る前は`rate_limits`自体が存在しない場合があります。この場合、対応する枠(five_hour / seven_day)は「未観測」として扱われます。
- five_hourとseven_dayは個別に欠落し得ます(片方だけ観測できるケースがあります)。

## ブリッジが保存するフィールド

```json
{
  "schema_version": 1,
  "source": "claude_code_statusline",
  "observed_at": "2026-01-01T12:00:00+00:00",
  "five_hour": {
    "used_percentage": 42.0,
    "remaining_percentage": 58.0,
    "resets_at": "2026-01-01T17:00:00+00:00"
  },
  "seven_day": {
    "used_percentage": 18.0,
    "remaining_percentage": 82.0,
    "resets_at": "2026-01-07T12:00:00+00:00"
  }
}
```

`session_id` / `session_name` / `transcript_path` / `cwd` / モデル情報 / cost / context window / token数 / 認証情報など、statusLineペイロードに含まれるその他のフィールドは一切読み取らず、保存もしません。stdinで受け取った生のJSONもログに出しません。

## キャッシュの保存先

Windows環境では以下に保存されます(このリポジトリの外、`~/.claude/`の外です)。

```text
%LOCALAPPDATA%\Cloud-LLM-LimitChecker\claude-code-usage.json
```

書き込みは一時ファイルへ書いてから`os.replace`で原子的に置換するため、読み取り側が壊れた/半端なJSONを見ることはありません。

## opt-in手順(手動)

1. リポジトリの絶対パスと、このプロジェクトの仮想環境のPythonパスを確認します。

   ```text
   C:\Users\shuns\.codex\project\Cloud-LLM-LimitChecker-git\.venv\Scripts\python.exe
   C:\Users\shuns\.codex\project\Cloud-LLM-LimitChecker-git\app\claude_code_usage_bridge.py
   ```

2. **既にstatusLineを設定している場合は、ここで一度止まってください。** 上書きすると既存のカスタムstatus line(コスト表示やモデル名表示など)が失われます。両方の情報を出したい場合は、既存のstatusLineコマンドの中でこのブリッジを呼び出し、その出力を自分のスクリプトの出力へ合成するラッパーを自作してください。このリポジトリは自動でのマージや上書きを行いません。
3. `~/.claude/settings.json`(または該当スコープの設定ファイル)に、以下のように**手動で**追記します。認証設定(`apiKeyHelper`等)には触れないでください。

   ```jsonc
   {
     "statusLine": {
       "type": "command",
       "command": "\"C:\\Users\\shuns\\.codex\\project\\Cloud-LLM-LimitChecker-git\\.venv\\Scripts\\python.exe\" \"C:\\Users\\shuns\\.codex\\project\\Cloud-LLM-LimitChecker-git\\app\\claude_code_usage_bridge.py\""
     }
   }
   ```

4. 新しいClaude Codeセッションを開始すると、最初のAPI応答の後からstatus lineに `Claude 5h: XX% used | 7d: YY% used` のような表示が出るようになります(`rate_limits`がまだ存在しない場合は `Claude usage: waiting for first response` と表示されます)。
5. このアプリの `/compact` 画面(30秒ごとのローカルGETのみ、Claude Codeや外部APIへの追加通信は発生しません)に「Claude Code Usage」カードとして反映されます。

## 無効化したい場合

`~/.claude/settings.json`の`statusLine`エントリを削除するか、元の設定に戻してください。このリポジトリ側のキャッシュファイル(`%LOCALAPPDATA%\Cloud-LLM-LimitChecker\claude-code-usage.json`)はただのローカルJSONファイルなので、不要であれば手動で削除して構いません。
