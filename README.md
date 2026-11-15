# dot-to-devin

Dot から Slack を経由してローカルの Devin を利用するための Slack 側ブリッジです。Slack のメンションを受けて Devin を起動し、結果をスレッドへ返します。Dot から Slack への連携処理は、このリポジトリには含みません。Slack の Socket Mode を使うため、ブリッジの公開受信口は不要です。

## Slack アプリとトークンの設定

1. [Slack のアプリ管理画面](https://api.slack.com/apps) で、このブリッジ専用のアプリを作成します。既存の別用途のアプリとトークンや Socket Mode の設定を共有しないでください。
2. `OAuth & Permissions` の `Bot Token Scopes` で `app_mentions:read`、`groups:history`、`chat:write` を追加します。長い出力をファイル添付するために `files:write` も追加します。目のリアクションを使う場合は `reactions:write` も追加します。
3. `Event Subscriptions` で `Enable Events` をオンにし、`Subscribe to bot events` に `app_mention` を追加します。ダイレクトメッセージも使う場合に限り、`message.im` を購読し、ボット権限に `im:history` を追加します。専用チャンネルのみで使う場合は追加しません。
4. `OAuth & Permissions` の `Install to Workspace` でアプリをインストールし、同ページに表示される `Bot User OAuth Access Token` を `.env` の `SLACK_BOT_TOKEN` に設定します。権限を後から追加した場合はアプリを再インストールして反映してください。
5. `Basic Information` の `App-Level Tokens` で `Generate Token and Scopes` を選び、`connections:write` を付けて発行したアプリ用トークンを `.env` の `SLACK_APP_TOKEN` に設定します。`Socket Mode` もオンにします。ボット用とアプリ用には、それぞれこの専用アプリのトークンを使います。
6. Slack のプロフィールから自分のユーザー識別子をコピーし、`.env` の `ALLOWED_USERS` に設定します。
7. 専用の**非公開チャンネル**を作り、人間の参加者はご本人だけにします。ブリッジのボットをチャンネルに招待し、他のボット・アプリや外部組織の参加者を加えないでください。ブリッジのボットも他のチャンネルへ招待しないでください。

## ローカルで起動

Python 3.10 以上と Devin のコマンドをインストールし、作業ディレクトリで以下を実行します。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

`.env` の `SLACK_BOT_TOKEN`、`SLACK_APP_TOKEN`、`ALLOWED_USERS` を設定し、`DEVIN_HOME` を自分の作業ディレクトリに合わせて変更してから `python3 bridge.py` を実行してください。トークン値は公開・コミットせず、安全に保管してください。

トークンローテーションを有効にしている場合は、[Slack の公式手順](https://docs.slack.dev/authentication/using-token-rotation/) と `.env.example` を参照し、対象の更新用トークンを `BRIDGE_APP_REFRESH_TOKEN` / `BRIDGE_BOT_REFRESH_TOKEN`（発行されたもののみ）、`Basic Information` のアプリ認証情報を `BRIDGE_CLIENT_ID` / `BRIDGE_CLIENT_SECRET` に設定してください。更新用トークンは使い捨てです。生成される `token_state.json` も秘密として保護し、`SLACK_CLIENT_*` という環境変数名は使わないでください。

チャンネルでは `@ボット名 repo:リポジトリ名 依頼内容` と送ります。`repo:` の名前は `DEVIN_HOME` 直下のディレクトリとして解決されます。スレッドへのメンション返信では、保存された Devin セッションの再開を試みます。

## セキュリティと運用上の注意

- **通信経路の暗号化は、内容を本人だけに隠す仕組みではありません。** 標準の Slack 接続と Devin の外部通信は通常、暗号化された通信路を使います。そのためネットワーク途中の通常の受動的な盗聴には対策がありますが、Slack、Devin とモデルの処理先では依頼内容を扱います。端末が信頼する通信検査プロキシがあれば、そこで内容を検査できる場合もあります。端から端まで本人しか復号できない暗号化ではありません。
- **投稿先の可視性に注意してください。** Devin の標準出力とエラー出力はスレッドに投稿され、長い出力は全文がファイル添付されます。非公開チャンネルでも Slack 側の保持設定や管理者向けのエクスポートの対象となり得ます。依頼文・出力にパスワードやトークンなどの秘密を含めないでください。
- **専用チャンネルの利用は現在、運用上のルールです。** `ALLOWED_USERS` は実行者だけを制限し、チャンネルは制限しません。許可されたユーザーがボットのいる別のチャンネルでメンションすると、その場に結果が投稿されます。`message.im` を購読していればダイレクトメッセージも処理します。チャンネルを強制的に限定するにはコードの変更が必要です。
- **ローカル実行にも注意してください。** Devin は `--permission-mode dangerous` と `--respect-workspace-trust false` で起動され、確認なしにホスト上の操作を行い得ます。信頼できるリポジトリと端末でのみ利用してください。依頼文は Devin の起動引数になり、端末上の監視手段などから見える可能性があります。
- **ブリッジの起動環境に別の秘密を置かないでください。** 子プロセスから除外される環境変数は `SLACK_` と `BRIDGE_` で始まる名前だけです。他のアプリのトークン等を別名で設定すると Devin に渡り得ます。
- **セッション継続には既知の制約があります。** 同じ作業ディレクトリで複数の Devin セッションが存在すると、スレッドに別のセッションが紐付く可能性があります。実用時にはこの問題の修正が必要です。
- **利用者の判断と責任で使用してください。** このツールは個人利用向けであり、実行による変更・情報の送信・データ損失を防ぐ保証はありません。実行する依頼と対象のリポジトリを確認した上で利用してください。この注意書きは、上記の安全対策や既知の不具合の修正を代替するものではありません。

参考: [Slack の通信保護](https://slack.com/trust/data-requests)、[Devin の通信保護](https://docs.devin.ai/admin/security)。
