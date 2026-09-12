# Windows Desktop Shell（MVP）

既存のFastAPI dashboardを、ブラウザではなくネイティブウィンドウで開くための薄いShellです。Shellが持つ責務は**ローカルbackendの生存管理**と**ウィンドウ表示**の2つだけで、API・cache・provider・永続化・診断は既存実装をそのまま再利用します。

## 構成

| 対象 | 役割 |
|---|---|
| `app/desktop/__main__.py` | エントリポイント。環境準備 → backend確保 → ウィンドウ表示 → 後始末 |
| `app/desktop/lifecycle.py` | port・identity・readiness・ownership・shutdown（GUI非依存） |
| `app/desktop/paths.py` | 実行ファイル／リソース／app-data／`.env`／DBのpath契約 |
| `CloudLLMLimitChecker.spec` | PyInstaller（onedir）のbuild定義 |

`app/desktop/` はFastAPIアプリから一切importされません。`webview` のimportはウィンドウを開く関数の内部だけで行うため、GUI未導入の環境でもbackendとテストは動きます。

## 依存関係の分離

| ファイル | 内容 | 用途 |
|---|---|---|
| `requirements.txt` | 既存のまま（GUI依存なし） | backend実行・CI |
| `requirements-desktop.txt` | `-r requirements.txt` ＋ `pywebview==6.2.1` | Desktop実行 |
| `requirements-build.txt` | `-r requirements-desktop.txt` ＋ `pyinstaller==6.22.2` | packaging時のみ |

CIは従来どおり `requirements.txt` だけを使います。

## 開発時の起動

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.venv\Scripts\python.exe -m app.desktop
```

## port契約

- bindは `127.0.0.1` 固定です。`0.0.0.0` へbindする経路はありません。
- 既定portは `8001` で、既存の `start_dashboard.bat` と同じです（`.bat` は変更していません）。
- `CLOUD_LLM_DESKTOP_PORT` でDesktopのみ上書きできます。受け付けるのはASCII数字だけで、範囲は1〜65535です。`+8001`・`8_001`・全角数字のような紛らわしい表記は、黙って解釈せず起動時にエラーにします。
- MVPではephemeral portへ移行しません。

## identityとbackendの所有権

`GET /api/desktop/identity` は固定値 `{"app": "Cloud-LLM-LimitChecker", "desktop_protocol": 1}` を返す read-only endpointです。version・path・account・credentialは含みません。Basic Auth有効時も `/api/health` と同じ狭い免除対象です。

port上のサービスは3通りに分類します。

| 状態 | 判定 | 動作 |
|---|---|---|
| 応答なし | ABSENT | 自分でbackendを起動（**OWNED**） |
| health OK ＋ identity一致 | OURS | 既存backendへ接続（**ATTACHED**） |
| 応答はあるがidentity不一致 | FOREIGN | **ウィンドウを開かず終了**（fail closed） |

HTTPを話さないプロセスがportを握っている場合も、接続自体は成立するためFOREIGNとして扱います（空きportとみなしてbindしに行き、分かりにくい失敗になるのを防ぐためです）。接続が拒否された場合だけABSENTです。

healthだけでは判定しません。ブラウザ用に `start_dashboard.bat` で起動済みの場合は ATTACHED になり、ウィンドウを閉じてもそのbackendは停止しません。OWNEDのときだけ停止します。

## 起動と終了

```text
環境準備 → port決定 → identity判定 → (必要なら)backend起動 → readiness待ち
→ ウィンドウ表示 → クローズ → OWNEDならshutdown → プロセス終了
```

- backendは**サブプロセスではなく同一プロセスのdaemon thread**でuvicornを起動します。子プロセスが無いため、孤児プロセスが構造的に発生しません。
- packaged（windowed）ビルドでは `sys.stdout` / `sys.stderr` が `None` になります。uvicornはlog formatter生成時に `sys.stdout.isatty()` を呼ぶため、そのままでは**backendが起動できません**。launcherは起動直後にstdout/stderrをログファイルへ向けます。出力先は `%LOCALAPPDATA%\Cloud-LLM-LimitChecker\desktop.log`（`LOCALAPPDATA` が無い環境では `XDG_DATA_HOME` または `~/.local/share` 配下）で、書き込めない場合はnullへfallbackし、起動は継続します。source実行では何も変更しません。
- ログは追記式で、番号付きrotationは行いません。サイズが約1MBを超えていた場合のみ、次回起動時に切り捨てて書き直します。
- ウィンドウ表示中にbackendが落ちた場合の自動クローズ（watchdog）は、Shellがbackendを起動した **OWNED のときだけ**動作します。ATTACHEDでは他者のbackendなので監視しません。
- 起動に失敗した場合は、ログへの記録に加えてネイティブのダイアログでも通知します（コンソールが無くても「何も起きずに終了」しないため）。表示するのは固定の一般的な文言とログのパスだけで、環境変数値やcredentialは含めません。
- readinessは最大20秒。timeout・backend thread死亡・foreign応答のいずれもウィンドウを開かずに失敗させます。
- 終了は `should_exit` による協調停止で、既存lifespan（scheduler停止・診断shutdown）がそのまま走ります。joinは上限付きで、停止しきらない場合は警告を出します。
- ウィンドウ表示中にbackend threadが死んだ場合は、watchdog（OWNED時のみ）がウィンドウを閉じます。ユーザーのクローズと同時に発生した場合は、破棄済みウィンドウに触れないよう停止フラグを再確認してから処理します。

## データベース

自動migration・自動コピーは行いません。優先順位は次のとおりです。

1. `APP_DB_URL` が設定済み → **そのまま使用**
2. source実行（開発） → 既存どおり `sqlite:///./limit_checker.db`
3. packaged実行で未設定 → `%LOCALAPPDATA%\Cloud-LLM-LimitChecker\limit_checker.db`

packaged版で既存の履歴を使いたい場合は、`APP_DB_URL` に明示指定してください（例：`APP_DB_URL=sqlite:///C:/path/to/limit_checker.db`）。既存DBを勝手に読み込んだりコピーしたりはしません。

## .env

- `.env` は**bundleしません**。
- launcherは `app.main` のimport前に、次の順で探索して読み込みます（`override=False`）。
  1. packaged：実行ファイルと同じフォルダ → `%LOCALAPPDATA%\Cloud-LLM-LimitChecker\`
  2. source：リポジトリroot → `%LOCALAPPDATA%\Cloud-LLM-LimitChecker\`
- **プロセス環境変数が常に優先**です。読み込んだ値をログ・画面・診断出力に出しません。

## 静的リソースとworking directory

`app/main.py` の `STATIC_DIR` は `__file__` 基準に解決します（CWD非依存）。`StaticFiles` と `/compact` の両方が同じ定数を使い、source実行とpackaged実行で同一コードパスです。`sys._MEIPASS` 分岐は `app/main.py` に持ち込んでいません。

packaged実行時のみ、launcherがworking directoryをbundle rootへ固定します。`config/seed.yaml` が相対パスで読まれ、見つからない場合は「seed対象なし」として静かに続行する実装のため、ショートカットの起動場所によって挙動が変わらないようにするためです。source実行では変更しません。

## packaging

- 形式は **onedir**（onefileは後回し）。
- 定義は `CloudLLMLimitChecker.spec`（追跡対象）。
- bundleするもの：アプリケーションコード（`app/` はPYZアーカイブ内のバイトコードとして格納され、`_internal/app/` というフォルダは生成されません）、`static/`、`config/`
- bundleしないもの：`.env`、データベース、cacheファイル
- 生成物のレイアウトは `CloudLLMLimitChecker.exe` ＋ `_internal/`（`_internal/static/`、`_internal/config/` を含む）です。`STATIC_DIR` は `app.main.__file__` から解決され、packaged時は `_internal/static` を指します。
- `build/` `dist/` はgitignore済みで、成果物はcommitしません。

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe -m PyInstaller CloudLLMLimitChecker.spec --noconfirm
```

## security境界

- bindは `127.0.0.1` のみ。
- ウィンドウの初期URLは検証済みのloopback URLだけです。
- 任意のリモートページをWebView内に表示する機能は追加していません。pywebviewにはnavigation制限の公式APIが無いため、URL生成をShell側に閉じることで担保しています。
  - 現行Dashboard（`static/index.html` / `app.js` / `compact.html` / `compact.js`）を確認したところ、外部URLへ遷移する経路（`<a href>`、`window.open`、`location` への外部URL代入、`form action`）は**存在しません**。
  - 外部リンクを既定ブラウザへ委譲する処理は**未実装**です。将来Dashboardに外部リンクを追加する場合は、委譲処理をセットで実装してください（下記「既知の制約」）。
- devtoolsはrelease buildで無効（`debug=False`）。
- JS API bridgeはMVPでは追加しません。
- `shell=True` を使いません。サブプロセスも起動しません。
- credential値の表示・保存・自動refreshは行いません。既存の `.env` / `gh auth` の扱いは変更していません。

## テスト方針

GUIはCIで起動しません。port検証・identity判定・readiness・ownership・shutdown・path契約・stdio/ログ・エラー通知・ウィンドウ生成（fake webview）はすべて依存注入で検証しています（`tests/test_desktop_shell.py`）。pywebview自体の動作と、packaged実レイアウト（`_internal/` 配下でのstatic解決、起動から終了までの一連の流れ）はCIでは検証せず、ローカルの手動smokeで担保します。

## 既知の制約（MVP対象外）

- installer・コード署名・自動更新なし
- macOS / Linux未対応
- UI再設計なし（既存Dashboardをそのまま表示）
- 外部リンクを既定ブラウザへ委譲する処理は未実装（現行Dashboardに外部リンクが無いため、追加時に対応が必要）
- **Basic Auth有効時（`ENABLE_BASIC_AUTH=true`）はDesktopから利用できません。** health / identity は免除されるためreadinessは成功しますが、Dashboard本体は401を返し、ネイティブウィンドウには資格情報を入力する手段がありません。この場合はウィンドウを開かず、理由を表示して終了します。ブラウザから利用するか、Desktop利用時のみ無効化してください。
- JSONエクスポートボタン（`app.js` の `/api/export/json` への遷移）は、`Content-Disposition` が付かないためWebView内に生JSONを表示します。戻る操作が無いウィンドウでは復帰できません（CSVはattachment指定があるため影響なし）。Dashboard UIとエクスポート仕様は本MVPの変更対象外のため、既知の制約として記載します。
