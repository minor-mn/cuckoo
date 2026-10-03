# X自動投稿・フォローバック

設定したタイムゾーンで毎日13時台の最初の起動時に、設定ファイルの本文をXへ1件投稿します。投稿済み状態を管理するため、同じ日に重複投稿しません。

投稿本文は `config.toml` のテンプレートから生成します。テンプレートには次の日付トークンを使用できます。

- `yyyy`: 4桁の年
- `mm`: 2桁の月
- `dd`: 2桁の日

投稿済み状態とフォロワーIDは、それぞれ状態ファイルで管理します。日付の境界や状態ファイルの場所も設定できます。

## 準備

1. X Developer Consoleでアプリを用意します。
2. OAuth 1.0a のユーザー認証情報を取得します。`Consumer Key`、`Consumer Secret`、`Access Token`、`Access Token Secret` の4値が必要です。X Developer Consoleでは、前者2つは `API Key` / `API Key Secret` と表示されることがあります。
3. アプリ権限を読み取り・書き込み（Read and Write）に設定し、投稿先アカウントとしてアクセストークンを発行します。
4. 設定ファイルを作成します。

```sh
cp config.toml.example config.toml
chmod 600 config.toml
```

`config.toml` には、次の項目を設定してください。

- `[auth]`: OAuth 1.0a の4つの認証情報
- `[target]`: 投稿先アカウントと投稿本文テンプレート
- `[system]`: タイムゾーン、状態ファイル、APIの接続先など

`[auth]` の `bearer_token` には、フォロワー一覧取得に使うアプリのBearer Tokenも設定します。

フォロー処理は当日最初の起動時だけ実行します。`followers_file` に保存した前回までのフォロワーIDを基準に、先頭から未保存のIDだけをフォローします。初回実行時は既存フォロワーを一括フォローせず、一覧を保存して基準を作ります。

投稿本文テンプレートに日付トークンを含めると、実行時の設定タイムゾーンの日付へ置換されます。

例えば、投稿本文テンプレートを `yyyy.mm.dd Daily update` とした場合、2026年10月2日の13時台の実行では `2026.10.02 Daily update` として投稿されます。

## 手動テスト

まず投稿を作成しないドライランを実行します（13時台のみ投稿対象として確認します）。

```sh
./run.sh --dry-run
```

問題なければ、投稿処理を含む実行を行います。

```sh
./run.sh
```

## cron

定期実行する場合は、リポジトリの絶対パスを使ってcrontabに登録します。

```cron
*/10 * * * * /absolute/path/to/cuckoo/run.sh >> /absolute/path/to/cuckoo/monitor.log 2>&1
```

スクリプト自身がロックを取得するため、前回の通信が遅れても実行状態を管理します。

## テスト

```sh
python3 -m unittest -v
```

## API仕様

投稿作成はX API v2の [Create Posts](https://docs.x.com/x-api/posts/create-post)（`POST /2/tweets`）を使い、OAuth 1.0a（HMAC-SHA1）で署名します。アクセストークンの有効期限やX APIの利用プランは、X Developer Consoleの設定に従ってください。
