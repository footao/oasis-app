# デプロイ手順（このフォルダをまとめて push する）

CORE_VERSION **3.6.0** 時点の一式です。`oasis_app.py` と `oasis_core.py` は
**必ずセットで**差し替えてください（版が合わないと起動時に止まります）。

---

## 0. push する前に必ず1回

### ① 学習ログを置く

`logg/` に Discord エクスポートの `.txt` を入れてください。
**⚠ 実ユーザー名が含まれます。** public リポジトリにする場合は `logg/README.md` を読んで
private にするか伏せ字にするか決めてください（学習に owner 列は使っていないので、
伏せても精度は落ちません）。

### ② オートパイロットのモデルを生成する ← 忘れやすい

```bash
python build_autopilot.py
```

`model.json` と `autopilot.bundle.js` は**いま雛形が入っています**。
このコマンドを実行しないと、オートパイロットは
「model.json が雛形のままです」と出て動きません（わざとそうしてあります。
中身のないモデルで賭けに行かせないためです）。

生成されるもの:

| ファイル | 中身 |
|---|---|
| `model.json` | 学習済み係数35個＋σ。Streamlit 側とは独立に読まれる |
| `autopilot.bundle.js` | model.js + モデル + autopilot.js を1つにしたもの（外部fetch不要） |
| `oasis_autopilot_setup.html` | 全部入りブックマークレットを最新コードで作り直す |

`build_autopilot.py` はステップ4で `parity_test.py` を回します。`oasis_core.py` に
特徴量を足して `bookmarklets/src/model.js` を直し忘れていると、**ここで止まります**。

**再学習したら毎回これを実行してください。** 忘れると画面のモデルと
オートパイロットのモデルがズレます。

---

## 1. GitHub に push

このフォルダの中身をまとめてアップロードします。

```bash
git add -A
git commit -m "v3.6.0: スタミナ不足を特徴量に追加 + Python↔JS一致テスト"
git push
```

Streamlit Community Cloud は push を検知して自動で再デプロイします。
反映されないときは `Manage app` → `Reboot app`。

---

## 2. オートパイロットのURLを合わせる

`bookmarklets/src/autopilot.js` の `MODEL_URL` と、
`bookmarklets/build_setup_page.py` の `CDN` が
**あなたのリポジトリを指しているか**確認してください。既定はこうなっています。

```
MODEL_URL : https://raw.githubusercontent.com/footao/oasis-app/main/model.json
CDN       : https://cdn.jsdelivr.net/gh/footao/oasis-app@main/autopilot.bundle.js
```

リポジトリ名やブランチが違う場合は書き換えてから `build_autopilot.py` を実行し直してください。
（`raw.githubusercontent.com` は `text/plain` で返るため `<script src>` では実行できません。
本体の読み込みには jsDelivr を使っています。）

---

## 3. 動作確認

```bash
python selftest.py logg     # 解析→学習→予測→ベットログ→回帰テスト18件
node bookmarklets/verify.js                 # 既存4種のブックマークレット
node bookmarklets/verify_setup.js oasis_autopilot_setup.html
node bookmarklets/test_logic.js             # 判定と安全弁
```

ブラウザ側は `oasis_autopilot_setup.html` を開いて、手順1の「🔍 診断」から順に。

---

## push してはいけないもの

- `.streamlit/secrets.toml`（`.example` だけ push する）
- `oasis_bet_log.csv`
- **購入リンクや token を含むメモ** — token は購入権限そのものです
- `__pycache__/`

`.gitignore` で弾くようにしてありますが、`git status` で最終確認を。

---

## このフォルダの中身

| 種別 | ファイル |
|---|---|
| アプリ | `oasis_app.py` `oasis_core.py` `passive_spec.json` |
| 保存/取得 | `sheets_backend.py` `drive_backend.py` |
| 生成 | `build_autopilot.py` `make_formula_doc.py` |
| オートパイロット | `model.json` `autopilot.bundle.js` `oasis_autopilot.html` `oasis_autopilot_setup.html` |
| ブックマークレット | `oasis_*_bookmarklet*.html` / `bookmarklets/src/*.js` |
| テスト | `selftest.py` `bookmarklets/verify*.js` `bookmarklets/test_logic.js` |
| 資料 | `README.md` `DEPLOY.md` `HANDOFF.md` `REVIEW.md` `CODE_REVIEW_20260809.md` |
