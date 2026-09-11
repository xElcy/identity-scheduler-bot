# 初心者向け：無料で常時稼働させる手順

おすすめ構成は、GitHubにコードを置き、Oracle Cloud Always FreeのUbuntuサーバーでDocker起動する方法です。

RenderやKoyebの無料サービスはアイドル時に停止する仕様があるため、Discord Gatewayへ常時接続するBOTには向きません。

## 0. Discord側の準備

1. [Discord Developer Portal](https://discord.com/developers/applications)を開き、`New Application`。
2. `Bot` → `Reset Token` → `Copy`でトークンを取得する。トークンは後で`.env`にだけ貼り付ける。
3. `Bot`画面で `Server Members Intent` をONにして `Save Changes`。
4. `Installation`画面のInstall Linkで、Scopesに `bot` と `applications.commands` を選ぶ。
5. Bot Permissionsに `Manage Channels`、`View Channels`、`Send Messages`、`Embed Links`、`Read Message History`、`Manage Messages` を選び、表示された招待URLで自分のサーバーへ追加する。
6. Discordのユーザー設定 → `詳細設定` → `開発者モード`をONにする。
7. サーバー名を右クリック → `サーバーIDをコピー`。これが `GUILD_ID`。

## 1. GitHubへコードを置く

1. GitHubで `New repository` を押す。
2. Repository nameを `identity-scheduler-bot` にする。
3. `Public` を選ぶ。コード内にトークンは入れないので公開しても問題ありません。非公開にしたい場合は後で設定できます。
4. 作成したリポジトリを開き、`Add file` → `Upload files` を押す。
5. このフォルダの中身をすべてアップロードする。
6. `.env` はアップロードしない。アップロードするのは `.env.example` です。
7. `Commit changes` を押す。

## 2. Oracle Cloudの無料サーバーを作る

1. [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/)を開く。
2. 無料アカウントを作成する。本人確認や支払い方法の登録を求められる場合があります。
3. Consoleに入ったら、左上メニューから `Compute` → `Instances` → `Create instance`。
4. 名前は `identity-scheduler-bot`。
5. Imageは `Ubuntu 24.04`。
6. Shapeは `VM.Standard.A1.Flex`、OCPUは `1`、メモリは `6 GB`。
7. `Assign a public IPv4 address` を有効にする。
8. SSHキーは `Generate a key pair for me` を選び、秘密鍵を必ずダウンロードする。
9. `Create`を押して、インスタンスが`Running`になるまで待つ。

OracleのAlways Free枠には、Ampere A1の無料コンピュート枠があります。ただし無料枠の空きがない場合は作成できないことがあります。その場合はAvailability Domainを変えて再試行します。[公式の無料枠説明](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

## 3. サーバーへ接続する

WindowsならPowerShellを開き、秘密鍵をダウンロードしたフォルダで次を実行します。

```powershell
ssh -i .\ダウンロードした秘密鍵の名前.key ubuntu@サーバーのパブリックIP
```

初回に質問が出たら `yes` と入力します。

## 4. Dockerをインストールする

サーバーへ接続した後、次をそのまま貼り付けます。

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git docker.io
sudo systemctl enable --now docker
```

## 5. GitHubからBOTを取得する

`あなたのGitHubユーザー名`は自分の名前に置き換えます。

```bash
git clone https://github.com/あなたのGitHubユーザー名/identity-scheduler-bot.git
cd identity-scheduler-bot
mkdir -p data
cp .env.example .env
nano .env
```

`nano`が開いたら、以下の3項目を入力します。

```text
DISCORD_TOKEN=DiscordのBotトークン
GUILD_ID=DiscordサーバーID
TIMEZONE=Asia/Tokyo
PRIVATE_CATEGORY_ID=
```

保存方法は、`Ctrl + O` → `Enter` → `Ctrl + X` です。

Botトークンは誰にも見せないでください。GitHubへ絶対にアップロードしないでください。

## 6. BOTを起動する

```bash
sudo docker build -t identity-scheduler-bot .
sudo docker run -d \
  --name identity-scheduler-bot \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  identity-scheduler-bot
```

これでサーバー再起動後もBOTが自動起動します。

起動確認：

```bash
sudo docker logs -f identity-scheduler-bot
```

`Logged in as` と表示されたら成功です。終了する場合は `Ctrl + C` です。Ctrl+Cはログ表示を終了するだけで、BOTは動き続けます。

## 7. Discordで動作確認

1. `/help` が表示されるか確認。
2. 管理権限のあるアカウントで `/schedule create`。
3. メンバーごとの鍵チャンネルができるか確認。
4. 自分の鍵チャンネルで日付ボタンを押す。
5. 管理者が `/schedule result` で集計を確認。

## 更新方法

コードを更新したら、サーバーで次を実行します。

```bash
cd ~/identity-scheduler-bot
git pull
sudo docker build -t identity-scheduler-bot .
sudo docker rm -f identity-scheduler-bot
sudo docker run -d \
  --name identity-scheduler-bot \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  identity-scheduler-bot
```

`data`フォルダを残しているため、予定や回答は更新後も維持されます。

## うまくいかないとき

```bash
sudo docker ps
sudo docker logs identity-scheduler-bot
```

この2つの結果のスクリーンショットを送れば、原因を確認できます。
