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

## Claude Desktop Codeタブ(Environment: Cloud)の扱い

このセクションは、Claude DesktopのCodeタブでEnvironmentを**Cloud**に固定して使っている場合(Localは使わずCLIをローカル作業に使う運用)を対象にしています。CLIの`statusLine`自動取得(このドキュメントの上のセクション)とは別経路です。

### 確認できた事実

- 上記の`statusLine`ブリッジは、CLI(ローカルで動くClaude Code本体)からのpush呼び出しにのみ反応します。現在のセッションでも、CLIを実行した直後にキャッシュのmtimeが更新されることを確認しています。
- Claude Codeの公式ドキュメント(`code.claude.com/docs/en/claude-code-on-the-web`、`code.claude.com/docs/en/sandbox-environments`)によれば、Claude Code on the webの各セッションはAnthropic管理の分離された仮想マシン上で動作し、ローカルマシンのファイルシステムへはアクセスできません。認証情報もプロキシ経由で扱われ、サンドボックス内には入らないとされています。コードの持ち込み・持ち出しについては、GitHubリポジトリからのclone、およびGitHubに接続していないローカルRepositoryをbundleとしてアップロードする経路が公式に提供されています。ただし、これらはいずれもコード・ブランチの同期経路であり、Cloud session側からこのPCのlocal usage cache(`claude-code-usage.json`)を直接更新できる公式経路は、今回調査したドキュメント上には見つかりませんでした。
- このlocal bridge構成では、CloudからのCode session実行結果がlocalの`claude-code-usage.json`キャッシュへ反映されることを確認できませんでした。これは現在のブリッジがCLIのstatusLine push呼び出しにのみ依存しているためで、Cloud session側から見た「更新できない理由」を断定するものではありません。
- ユーザーからは、「Desktop CloudでのCode使用が、1〜2日前まではこのダッシュボードのClaude使用率へ反映されているように見えていた」という観測が報告されています。この観測自体は事実として記録しますが、当時どの経路で反映されていたのか(例: 過去に異なるstatusLine設定を使っていた、Localで作業していた期間があった、など)は今回のリポジトリ内調査だけでは確認できておらず、未確定です。

### 明示的に採用しなかった/断定しなかったこと

- **Anthropicが機能を削除した、とは断定していません。** 確認できたのは「このlocal bridge構成ではCloudからlocal cacheへの更新経路を確認できなかった」という事実のみです。
- **「Desktop Cloudでは絶対にstatusLineが動かない」とも一般化していません。** 今回確認できたのは、現在のブリッジ実装(CLIのpush呼び出しにのみ依存する設計)の範囲内での結果です。
- Desktop内部キャッシュ・transcriptファイルの解析、画面スクレイピング、OCR、OAuthトークンやCookieの抽出、非公開APIの呼び出しは行っていません。

### Claude Desktop Cloud 手動fallback

上記の理由により、CLI自動取得を完全に維持したまま、Desktop Cloudの値を**手動で**入力できる別経路を追加しています。

- 保存先は`statusLine`のキャッシュ(`claude-code-usage.json`)とは別ファイルです(`%LOCALAPPDATA%\Cloud-LLM-LimitChecker\claude-desktop-cloud-usage.json`)。サーバー側でも2つのキャッシュ・2つのGETエンドポイント(`GET /api/claude-code-usage`と`GET /api/claude-code-usage/manual`)を最後まで分離しており、既存の`GET /api/claude-code-usage`の挙動・レスポンス形は変更していません。
- 入力は管理画面(`/`)の「Claude Desktop Cloud 使用率（手動入力）」パネルから行います。Claude Desktopの公式usage画面で確認した5時間枠・7日枠それぞれの残り%とreset日時を入力し、保存前に確認ダイアログを挟みます。5時間枠・7日枠は両方入力が必須です(理由は次項)。
- `used_percentage`は常にサーバー側で`100 - remaining_percentage`として計算し、クライアントからの入力は受け付けません。

### auto/manualの選択規則

`/compact`表示は、CLI自動取得(auto)とDesktop Cloud手動確認値(manual)のどちらか一方の snapshot を**丸ごと**選び、window(5時間枠・7日枠)単位で混ぜ合わせることはしません。

- 有効(`available: true`)なsnapshotだけを候補にします。
- 両方有効なら`observed_at`が新しい方を採用します。同時刻ならCLI自動取得を優先します。
- 手動保存(`PUT /api/claude-code-usage/manual`)は5時間枠・7日枠の両方を必須にしています。これは、片方だけの手動snapshotがより新しい`observed_at`を持ってしまうと、完全なauto snapshotの片方の枠を表示上覆い隠してしまうためです。
- CLIが後から新しい`observed_at`で書き込めば、選択は自動的にCLI自動取得側へ戻ります。手動値に固定されたままにはなりません。
- 表示には取得元のバッジ(「CLI自動取得」/「Desktop Cloud 手動確認値」)を出します。最終観測値が古い場合は、取得元のバッジとは別に「(古い可能性があります)」という注記を一度だけ表示します。手動値が自動取得のように見えるラベル("Desktop Cloud 自動取得"など)は使いません。
