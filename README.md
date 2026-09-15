# 第五人格クラン 個人日程表BOT

各メンバーに鍵チャンネルを1つずつ作り、その人だけが見られる予定表で都合を回答するDiscord BOTです。

## 毎日更新・途中参加（最新版）

詳しい使い方は [ROLLING-GUIDE.md](ROLLING-GUIDE.md) を参照してください。

- `/schedule add-member member:@メンバー`：対象ロールが付いた1人だけ部屋・パネルを追加。既存部屋は再利用。
- `/schedule rolling`：回答を保存して、今日から7日間を毎日自動更新。受付終了後の再開にも使用。
- `/schedule history date:2026-09-14`：過去の回答を管理者だけに表示。
- `/schedule decide` または決定候補ボタン：`@everyone` 付きで確定告知。自動更新中は受付を継続。
- 全スラッシュコマンドはAdministrator権限を持つ管理者向けです。一般メンバーは本人の回答ボタンを使用します。

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
blocks: 昼 12-18時, 夜 18-24時
target_role: @クランメンバー
category: 個人予定
```

対象ロールを持つメンバーに個人チャンネルとパネルを作成します。日付を省略すると今日から7日間を毎日更新します。固定日程だけ `dates` を指定してください。

```text
/schedule result
/schedule close
```

`result` は実際のチャンネル一覧を公開せず、管理者本人にだけ集計を表示します。`close` で全員の回答を締め切ります。

### 管理者本人の部屋

```text
/schedule setup
```

管理者本人の専用チャンネルがまだ作られていない場合に作成します。一般メンバーは管理者の `add-member` で追加し、本人の部屋のボタンで回答します。

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

## VC通知を鳴らさないチャンネル

管理者用の会議室など、入室通知を鳴らしたくないVCは管理者が次のコマンドで登録できます。複数のVCを登録でき、チャンネル名を変更しても設定は維持されます。

```text
/schedule ignore-voice-channel channel:会議室
```

再び通知対象へ戻す場合は次を実行します。

```text
/schedule unignore-voice-channel channel:会議室
```
