# ブックマークレットのソース

`../..` にある3つの HTML は、この `.js` を1行化して `href="javascript:..."` に
埋め込んだものです。中身を直したいときは `.js` を編集して、次を実行してください。

```bash
python minify.py ../../oasis_bookmarklet_v2.html      bm.js    "🏇 レースデータ取得"
python minify.py ../../oasis_buy_bookmarklet_v2.html  buy.js   "🛒 一括購入 v2"
python minify.py ../../oasis_probe_bookmarklet.html   probe.js "🔬 単勝プール実測"
```

| ソース | 役割 | rrc を使うか |
|---|---|---|
| `bm.js` | オッズ・出走馬・パッシブ効果の取得 | 使わない |
| `probe.js` | 試し買いで単勝プールを実測 ＋ 全データ取得 | **使う**（最大5,000 rrc） |
| `buy.js` | 買い目の一括購入（3連単・単勝） | **使う** |

`minify.py` は行頭・行末の `//` コメントを安全に落として1行化します
（`//` を素朴に残したまま1行化すると、以降のコードが全部コメント扱いになって壊れます）。
