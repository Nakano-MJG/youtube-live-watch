# YouTube ライブ配信ウォッチャー

登録したチャンネル（5〜10ch程度）が **ライブ配信中かどうか** と **同時視聴者数** を GitHub Actions で定期チェックします。

- 配信が始まったら / 終わったら → **Teams に通知**
- 配信中の同時視聴者数 → `data/viewers_YYYY-MM.csv` に記録（月ごとのファイル）
- 配信1回ごとのまとめ（開始・終了・配信時間・最大/平均視聴者数）→ `data/sessions.csv`

CSV は Excel でそのまま開けます（文字化けしない形式で保存）。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `channels.csv` | 監視するチャンネルの一覧（ここだけ編集すればOK） |
| `watch.py` | チェック本体 |
| `.github/workflows/watch.yml` | 5分ごとに `watch.py` を動かす設定 |
| `state.json` / `data/` | 実行すると自動で作られる（配信中の状態と記録） |

---

## 運用方針：Public リポジトリ ＋ 5分ごとの予約

- GitHub の定期実行は混雑時に予約を飛ばすことが多いため、5分ごとに予約して「どれかは動く」ようにしています。
- Public リポジトリなので Actions の実行時間は無料・無制限です。
- APIキーと Teams の Webhook URL は Secrets に入れるので公開されません。公開されるのは、このリポジトリのファイル（監視チャンネル一覧・配信状況・視聴者数の記録）と Actions の実行ログです。
- 初回実行（state.json が無いとき）は、その時点で配信中のものを記録するだけで通知しません。

YouTube API の使用量は 1回あたり約20ユニット。全部実行されても 1日約5,800ユニット（無料上限 10,000/日）です。

---

## セットアップ手順

### 1. YouTube Data API のAPIキーを作る（無料）

1. https://console.cloud.google.com/ に会社 or 個人の Google アカウントでログイン
2. 上部のプロジェクト選択 →「新しいプロジェクト」→ 名前（例：`youtube-live-watch`）を付けて作成
3. 左メニュー「APIとサービス」→「ライブラリ」→ **YouTube Data API v3** を検索 →「有効にする」
4. 「APIとサービス」→「認証情報」→「＋認証情報を作成」→「APIキー」
5. 作成されたキーを開き、「APIの制限」で **YouTube Data API v3 のみ** を選んで保存（悪用防止）
6. キー（`AIza...` で始まる文字列）をメモ

※ 請求先アカウント（クレジットカード）の登録は不要です。

### 2. Teams の Webhook URL を用意する（通知先のチャネルごと）

YouTube チャンネルごとに別の Teams チャネルへ通知できます。通知先にしたい **Teams チャネルの数だけ** 作ります：

1. 通知したい Teams チャネルの「…」→「ワークフロー」
2. 「Webhook 要求を受信したらチャネルに投稿する」を選択
3. 作成後に表示される URL をメモ（どのチャネルの URL か分かるように）

### 3. GitHub リポジトリ

このリポジトリ（Public）にファイル一式が入っています。

### 4. Secrets（秘密の値）を登録する

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で登録：

| Name | Secret |
|---|---|
| `YOUTUBE_API_KEY` | 手順1のAPIキー |
| `TEAMS_通知先名` | 通知先ごとの Webhook URL（通知先の数だけ作る。下の説明） |
| `TEAMS_WEBHOOK_URL` | （任意）`notify` 列が空のチャンネルの通知先 URL |

**通知先の追加は「Secret を1つ作る」＋「watch.yml に1行追記」だけです。** Name は `TEAMS_` ＋ 通知先名（半角英大文字・数字・`_` のみ）、Secret に Webhook URL を1つだけ入れます：

| Name | Secret |
|---|---|
| `TEAMS_SAPPORO` | 札幌のチャネルの Webhook URL |
| `TEAMS_HAKODATE` | 函館のチャネルの Webhook URL |

続けて `.github/workflows/watch.yml` の「通知先を増やすときは…」の下に、その Secret を渡す行を追記します：

```yaml
          TEAMS_SAPPORO: ${{ secrets.TEAMS_SAPPORO }}
          TEAMS_HAKODATE: ${{ secrets.TEAMS_HAKODATE }}
```

既存の Secret を書き直す必要はありません（安全のため、ワークフローには明示した Secret だけを渡す作りにしています）。

### 5. 監視するチャンネルを登録する

GitHub 上で `channels.csv` を開き、鉛筆アイコンで編集：

```
name,channel,notify
札幌店,@example_sapporo,SAPPORO
函館店,https://www.youtube.com/@example_hakodate,HAKODATE
本社,UCxxxxxxxxxxxxxxxxxxxxxx,SAPPORO;HAKODATE
```

- `channel` には **@ハンドル / チャンネルページのURL / UC〜で始まるID** のどれでもOK
- `name` は通知に出る名前（空欄なら YouTube 上のチャンネル名）
- `notify` には **通知先名**（Secret `TEAMS_SAPPORO` なら `SAPPORO`）。「;」区切りで複数チャネルにも送れます。空欄なら `TEAMS_WEBHOOK_URL` へ（それも無ければ通知なし、記録だけ）
- 通知先が見つからないときは、実行ログに「Secret『TEAMS_〇〇』の登録と watch.yml への追記を確認してください」と警告が出ます

### 6. 動作確認

1. リポジトリの **Actions** タブ →（初回は「I understand… enable them」を押す）
2. 左の「YouTube ライブ監視」→「Run workflow」→「Run workflow」
3. 1分ほどで緑のチェックになれば成功。クリックすると「配信状況をチェック」の欄に結果が出ます
4. 以降は自動で5分ごとに動きます（GitHub の都合で飛ばされる回もあります）

---

## 結果の見方

- リポジトリの `data` フォルダ内の CSV をクリック →「Download raw file」で Excel で開けます
- `viewers_2026-10.csv`：チェックごとの同時視聴者数（配信中のときだけ行が増えます）
- `sessions.csv`：配信1回ごとのまとめ

## 知っておきたいこと

- **初回実行時にすでに配信中のチャンネルは「配信開始」として通知されます。**
- チャンネル側で視聴者数を非公開にしている場合、人数は「非公開」（CSVでは空欄）になります。
- 開始・終了の検知はチェック間隔ぶん（5分＋GitHubが実行を飛ばした分）遅れます。短い配信は、チェックとチェックの間に始まって終わると記録されません。
- 平均/最大視聴者数は「チェックした時点の値」から計算した大まかな目安です。
- エラーで止まった場合は Actions の実行ログに日本語で理由が出ます（APIキー誤り・API上限超過など）。
