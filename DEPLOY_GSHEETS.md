# Google Sheets 対応 & Community Cloud デプロイ手順

このアプリのベットログ（`oasis_bet_log.csv` 相当）を **Google スプレッドシート**に
保存するようにしました。これにより Streamlit Community Cloud 上でも、
記録・精算した結果が再起動で消えずに残ります。

- **何が変わったか**: ベットログの保存先だけ。解析ロジック・モデル学習は一切変更なし。
- **挙動の切り替え**: `secrets` に Sheets 設定があれば自動で Sheets を使い、
  無ければ従来どおりローカルCSVに保存します（ローカル実行はそのままでもOK）。
- **学習データ `OASISUTTI.txt`** は読み込み専用なので、リポジトリに置くだけで動きます。

---

## 全体の流れ

1. Google Cloud でサービスアカウントを作り、鍵(JSON)を発行
2. スプレッドシートを1枚用意して、そのサービスアカウントに共有
3. GitHub にコードを上げる
4. Community Cloud でデプロイし、Secrets に鍵とシートIDを貼る

所要 15〜20 分ほどです。

---

## 1. Google Cloud 側の準備

1. <https://console.cloud.google.com/> にアクセスし、プロジェクトを作成（既存でも可）。
2. 「APIとサービス ▸ ライブラリ」で次の2つを **有効化**:
   - **Google Sheets API**
   - **Google Drive API**
3. 「APIとサービス ▸ 認証情報 ▸ 認証情報を作成 ▸ サービスアカウント」を選び、
   名前（例 `oasis-bot`）を付けて作成。ロールは付けなくてOK。
4. 作ったサービスアカウントを開き、「キー ▸ 鍵を追加 ▸ 新しい鍵を作成 ▸ JSON」。
   JSONファイルがダウンロードされます（これが資格情報。後で中身を Secrets に貼ります）。
5. そのサービスアカウントの **メールアドレス**（`xxxx@xxxx.iam.gserviceaccount.com`）を控えておく。

---

## 2. スプレッドシートの用意

1. Google スプレッドシートを新規作成（名前は任意、例 `oasis_bet_log`）。
2. 右上「共有」で、**手順1で控えたサービスアカウントのメール**を
   **編集者(Editor)** として追加。← これを忘れると書き込めません。
3. URL から **スプレッドシートID**を控える:
   `https://docs.google.com/spreadsheets/d/【この部分がID】/edit`

タブ（ワークシート）は自分で作らなくて大丈夫です。
アプリ初回書き込み時に `bet_log` タブとヘッダ行を自動生成します。

---

## 3. （任意）ローカルで先に動作確認

1. 同梱の `.streamlit/secrets.toml.example` を `.streamlit/secrets.toml` にコピー。
2. ダウンロードしたJSONの各値と、スプレッドシートIDを埋める。
   - `private_key` は改行を `\n` のままにして、例のとおり三連クオート `"""..."""` で囲む。
3. 依存をインストールして起動:
   ```bash
   pip install -r requirements.txt
   streamlit run oasis_app.py
   ```
4. 画面下の実績ログ欄に「🗂 保存先: Google スプレッドシート」と出れば接続成功。
   記録→精算してみて、スプレッドシートに行が増えればOK。

> `secrets.toml` を置かなければ、これまでどおりローカルCSVに保存されます。

---

## 4. GitHub にアップロード

このフォルダ一式を GitHub リポジトリに push します。

- `.gitignore` で `secrets.toml` と `oasis_bet_log.csv` は除外済み。
  **鍵(secrets.toml)は絶対にコミットしないでください。**
- `OASISUTTI.txt`（学習データ）を公開したくない場合は、
  **private リポジトリ**にしてください（Community Cloud は private でもデプロイ可）。

---

## 5. Community Cloud でデプロイ

1. <https://share.streamlit.io/> にGitHubでログイン。
2. 「Create app」→ リポジトリ／ブランチ／メインファイル `oasis_app.py` を指定。
3. デプロイ前（または後）に **「Settings ▸ Secrets」** を開き、
   `secrets.toml.example` と同じ形式で、実際の鍵とシートIDを貼り付けて保存。
4. デプロイ完了後、`https://<アプリ名>.streamlit.app` の固定URLでアクセスできます。
   外出先からはこのURLを開くだけ（PCを起動しておく必要もありません）。

---

## 補足・トラブルシュート

- **「Google Sheets 接続に失敗」と出る**: 画面に理由が表示されます。よくある原因は
  ① シートをサービスアカウントに共有し忘れ、② シートIDの誤り、
  ③ Sheets/Drive API が未有効化、④ `private_key` の改行崩れ。
- **複数人で同時に使う**: シート全体を読み書きする方式なので、
  同時に精算すると後から保存した方で上書きされます。個人利用なら問題ありません。
- **モデルを育てたい（`OASISUTTI.txt` に追記して再学習）**: Cloud上ではファイル追記が
  永続しないため、その運用をするなら学習データもSheetsやDB等に移す必要があります
  （現状のアプリは追記しないので当面は不要）。
- **ローカルexe版**: 従来の `run_app.py`／ビルド手順はそのまま使えます。
  secrets を置かなければローカルCSV保存のままです。
