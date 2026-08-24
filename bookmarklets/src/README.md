# ブックマークレットのソース

`../..` にある3つの HTML は、この `.js` を1行化して `href="javascript:..."` に
埋め込んだものです。中身を直したいときは `.js` を編集して、次を実行してください。

```bash
python minify.py ../../oasis_bookmarklet_v2.html      bm.js      "🏇 レースデータ取得"
python minify.py ../../oasis_buy_bookmarklet_v2.html  buy.js     "🛒 一括購入 v2"
python minify.py ../../oasis_harvest_bookmarklet.html harvest.js "📥 結果を採取"
```

| ソース | 役割 | rrc を使うか |
|---|---|---|
| `bm.js` | オッズ・出走馬・パッシブ効果の取得 | 使わない |
| `buy.js` | 買い目の一括購入（3連単・単勝） | **使う** |
| `harvest.js` | 過去の確定レースを一括取得し、loggと同じ書式で学習データ化 | 使わない |
| `model.js` | `model.json` を読んで**Pythonと同じ予測**をブラウザで行う（単体では何もしない） | 使わない |
| `autopilot.js` | 定期開催レースを自動で解析し買い目を用意。**購入だけ手動1クリック** | **使う** |

`harvest.js` は `result API` から過去レースの着順・score・ステータス・コンディション・
パッシブ2枠を取得し、Discordログと同じ書式で出力します。保存して `logg` に入れると学習データが増えます。
（`parse_results` はこの書式の `📉 コンディション：` 行を直接読めるよう v3.0 で対応済み。）

## オートパイロット（半自動）のビルド

`model.js` と `autopilot.js` を**1つのIIFEに包んで**結合し、`oasis_autopilot.html` に注入します
（`javascript:` URL はページのグローバルスコープで動くため、包まないと2回目に
`const` の再宣言でエラーになります）。

```bash
python - <<'EOF'
import io, html as H, re, sys
sys.path.insert(0, '.')
import minify
js = '(()=>{' + minify.minify_js(open('model.js', encoding='utf-8').read()) + ' ' \
   + minify.minify_js(open('autopilot.js', encoding='utf-8').read()) + '})();'
page = io.open('../../oasis_autopilot.html', encoding='utf-8').read()
page, n = re.subn(r'href="javascript:.*?">🛩 オートパイロット',
                  lambda m: 'href="' + H.escape('javascript:' + js, quote=True) + '">🛩 オートパイロット',
                  page, count=1, flags=re.S)
assert n == 1, '置換できませんでした'
io.open('../../oasis_autopilot.html', 'w', encoding='utf-8').write(page)
EOF
node ../verify_autopilot.js ../../oasis_autopilot.html   # 構文チェック
node ../test_logic.js                                    # 判定と安全弁のテスト
```

モデルは `oc.export_model_json(bundle, 'model.json')` で書き出し、GitHubに置いて
`autopilot.js` の `MODEL_URL` から読みます。**再学習したら model.json も更新すること**
（更新しないと画面のモデルと自動売買のモデルがズレます）。

`minify.py` は行頭・行末の `//` コメントを安全に落として1行化します
（`//` を素朴に残したまま1行化すると、以降のコードが全部コメント扱いになって壊れます）。
除去はヒューリスティック（クォートの偶数判定など）なので、**ビルド後は必ず**
`node verify.js` を実行して4本とも構文OKであることを確認してください。

`harvest.js` は結果ブロックに `🏁 第{schedule_id}レース 結果` を出力します（v3.3.0〜）。
ツール側の `race_key` は「日付＋時刻＋レース番号」で作るため、番号が無いと
`race_time` が取れず `0:00` になったレースが**1レースに合成**されてしまうためです。
番号を外すと学習データが壊れるので変更しないでください。
python minify.py ../../oasis_buy_pick_bookmarklet.html buy_pick.js "🎫 馬券を選んで購入"
