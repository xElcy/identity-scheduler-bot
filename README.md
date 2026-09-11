# 第五人格クラン 個人日程表BOT

各メンバーに鍵チャンネルを1つずつ作り、その人だけが見られる予定表で都合を回答するDiscord BOTです。

## 画面の考え方

- 日付ごとに「昼」「夜」などの時間帯ボタンを表示
- ボタンを押すたびに `— → ○ → △ → × → —` と切り替え
- `○` は行ける、`△` は未定、`×` は行けない
- 他のメンバーは個人チャンネルを見られない
- 管理者は `/schedule result` で人数だけの集計結果を確認
- 日付が多い場合は4日ずつページ切り替え

## コマンド

### 管理者

```text
/schedule create
title: 9月クラン戦の都合確認
dates: 9/20, 9/21, 9/22, 9/23, 9/24
blocks: 昼 12-18時, 夜 18-24時
```

日程表を作成すると、サーバー内のBOT以外のメンバーへ個人チャンネルを作成し、個人用パネルを投稿します。

```text
/schedule result
/schedule close
```

`result` は実際のチャンネル一覧を公開せず、管理者本人にだけ集計を表示します。`close` で全員の回答を締め切ります。

### メンバー

```text
/schedule setup
```

自分の専用チャンネルがまだ作られていない場合に作成します。日程表のボタンを押すだけで回答できます。

## Discord側で必要な権限

Botには次の権限が必要です。

- `Manage Channels`：メンバーごとの鍵チャンネル作成
- `View Channels`
- `Send Messages`
- `Embed Links`
- `Read Message History`
- `Manage Messages`：個人パネルの更新

Developer PortalのBot設定で `Server Members Intent` も有効にしてください。作成時にサーバーメンバー一覧を取得するために使います。

## ローカル起動

1. Discord Developer PortalでApplicationを作成し、Botを追加する。
2. `.env.example` を `.env` にコピーする。
3. `DISCORD_TOKEN`、`GUILD_ID`を設定する。
4. 必要なら、個人チャンネルを置くカテゴリのIDを `PRIVATE_CATEGORY_ID` に設定する。
5. Botを `bot` スコープ＋`applications.commands` スコープで招待する。

```powershell
py -3.12 -m venv .venv
\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m bot
```

初心者向けの無料サーバーへの配置手順は、[DEPLOY-GUIDE.md](DEPLOY-GUIDE.md)にまとめています。

## GitHub・無料サーバーへの配置

このフォルダをGitHubリポジトリへ置き、`Dockerfile` が使える常時稼働型の無料プランまたは自宅PC/VPSへデプロイできます。トークンはコードへ書かず、ホスティング側の環境変数に設定してください。

GitHub Actionsは実行時間制限があるため、BOT本体を常時稼働させる場所としては使わず、GitHubはソース管理、BOTは別の実行環境という分け方にします。

## セキュリティ設計

- 回答内容を公開チャンネルへ投稿しない
- 個人チャンネルは対象メンバーとBOTだけが閲覧可能
- 集計結果は管理者のスラッシュコマンドへ非公開返信
- Discordトークンは`.env`やホスティングの環境変数で管理

※ Discordの仕様上、サーバーのAdministrator権限を持つ人はチャンネル権限を bypass できます。完全に見えなくするには、管理者権限の運用も分けてください。
