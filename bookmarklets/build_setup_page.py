# -*- coding: utf-8 -*-
"""oasis_autopilot_setup.html を生成する。

「動かない」ときの切り分けページ。
  手順1: ごく短い診断ブックマークレット（動けばコード以外が原因と分かる）
  手順2: ローダー版（本体は外部・登録は200文字程度）
  手順3: 全部入り（従来どおり）
"""
import html as H
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# リポジトリが違う場合はここを書き換える
CDN = 'https://cdn.jsdelivr.net/gh/footao/oasis-app@main/autopilot.bundle.js'

DIAG = (
    "javascript:(()=>{var q=new URLSearchParams(location.search);"
    "var m='OK: ブックマークレットは動いています\\n\\nhost: '+location.host"
    "+'\\nguild: '+(q.get('guild')||'なし')"
    "+'\\ntoken: '+(q.get('token')?'あり':'なし')"
    "+'\\nfetch: '+(typeof fetch)+'\\n\\n(タップで閉じる)';"
    "try{var d=document.createElement('div');"
    "d.style.cssText='position:fixed;left:8px;right:8px;top:12px;z-index:2147483647;"
    "background:#12121f;color:#7fd1a0;border:2px solid #e2b96f;border-radius:8px;"
    "padding:12px;font:13px/1.6 monospace;white-space:pre-wrap';"
    "d.textContent=m;d.onclick=function(){d.remove()};document.body.appendChild(d);}"
    "catch(e){alert(m)}})()"
)

# ⚠ jsDelivr は @main を12時間キャッシュし、クエリ文字列を無視する。
# push 直後に古いバンドルを掴む事故を bm.js で実際にやったので、
# 「raw（キャッシュなし）→ raw.githack（キャッシュなし）→ jsDelivr（最後の砦）」
# の順に落とす。他のブックマークレットのローダーと同じ構成。
RAW = 'https://raw.githubusercontent.com/footao/oasis-app/main/autopilot.bundle.js'
GHACK = 'https://raw.githack.com/footao/oasis-app/main/autopilot.bundle.js'
LOADER = (
    "javascript:(t=>{fetch('" + RAW + "?'+t).then(r=>r.text()).then(x=>{(0,eval)(x)})"
    ".catch(e=>{console.warn('Oasis autopilot loader fallback:',e);"
    "var a=document.createElement('script');a.src='" + GHACK + "?'+t;"
    "a.onerror=function(){var b=document.createElement('script');"
    "b.src='" + CDN + "?'+t;document.body.appendChild(b)};"
    "document.body.appendChild(a)})})(Date.now())"
)


def build():
    full = io.open(os.path.join('/tmp', 'combined.js'), encoding='utf-8').read()
    e = lambda x: H.escape(x, quote=True)
    parts = []
    A = parts.append
    A('<!DOCTYPE html>\n<meta charset="utf-8">\n')
    A('<meta name="viewport" content="width=device-width,initial-scale=1">\n')
    A('<title>オートパイロット — 動かないときの切り分け</title>\n<style>\n')
    A('body{font-family:system-ui,-apple-system,sans-serif;max-width:800px;margin:0 auto;'
      'padding:1.5rem 1rem 4rem;background:#12121f;color:#eee;line-height:1.8}\n'
      'a.bm{display:inline-block;background:#e2b96f;color:#12121f;padding:.75rem 1.3rem;'
      'border-radius:8px;font-weight:700;text-decoration:none}\n'
      'a.sm{background:#4a5568;color:#fff}\n'
      'code{background:#0b0b14;padding:.15rem .4rem;border-radius:4px;font-size:.88em;word-break:break-all}\n'
      'h2{color:#e2b96f;margin-top:2.2rem;border-bottom:1px solid #333;padding-bottom:.3rem}\n'
      '.box{background:#0b0b14;border:1px solid #333;border-radius:6px;padding:.8rem 1rem;margin:.8rem 0}\n'
      '.ok{border-left:4px solid #81c784}.ng{border-left:4px solid #ef5350}\n'
      '.warn{border-left:4px solid #ffb74d}\n'
      'textarea{width:100%;height:70px;background:#0b0b14;color:#7fd1a0;border:1px solid #444;'
      'border-radius:6px;padding:.5rem;font-size:.68rem;font-family:monospace}\n'
      'button.cp{background:#2e7d32;color:#fff;border:none;border-radius:6px;padding:.6rem 1.1rem;'
      'font-weight:700;cursor:pointer}\n'
      'table{border-collapse:collapse;width:100%}td,th{border:1px solid #333;padding:.45rem .6rem;'
      'font-size:.9rem;text-align:left}\n</style>\n')

    A('<h1>動かないときの切り分け</h1>\n')
    A('<p>上から順に試すと、どこで止まっているか1分で分かります。</p>\n')

    A('<h2>手順1 — まずこれが動くか</h2>\n')
    A('<p>いちばん短いブックマークレットです。'
      '<b>これが動かなければ原因は私のコードではありません</b>'
      '（Safariの登録ミスか、サイト側のCSP）。</p>\n')
    A('<p><a class="bm sm" href="%s">🔍 診断（%d文字）</a></p>\n' % (e(DIAG), len(DIAG)))
    A('<div class="box ok"><b>緑の枠が出た場合</b> → ブックマークレット自体は動きます。手順2へ。</div>\n')
    A('<div class="box ng"><b>何も起きない場合</b> → 次のどちらかです。<br>\n'
      '・<b>Safariが <code>javascript:</code> を消した</b>…登録し直してください。'
      'URL欄に貼ったあと、先頭が <code>javascript:</code> で始まっているか確認を。'
      '消えていたら手で打ち足します。<br>\n'
      '・<b>サイトのCSPがブロック</b>…この場合ブックマークレット方式は使えません。'
      '下の「最終手段」へ。</div>\n')

    A('<h2>手順2 — ローダー版（推奨）</h2>\n')
    A('<p>本体を外部に置き、<b>%d文字</b>だけ登録します。'
      '18,000文字を貼る必要がなくなり、コードを更新してもブックマークの登録し直しが'
      '不要になります。</p>\n' % len(LOADER))
    A('<p><a class="bm" href="%s">🛩 オートパイロット（ローダー）</a></p>\n' % e(LOADER))
    A('<div class="box warn"><b>先に1回だけ準備が要ります。</b>\n'
      '<code>autopilot.bundle.js</code> をリポジトリ直下に置いて GitHub に push してください。<br>\n'
      '読み込み先: <code>%s</code><br>\n'
      'リポジトリ名やブランチが違う場合は、下のコード内のURLを書き換えてから登録してください'
      '（<code>@main</code> がブランチ名）。</div>\n' % H.escape(CDN))
    A('<p><button class="cp" id="c2">📋 ローダーをコピー</button> <span id="m2"></span></p>\n')
    A('<textarea id="s2" readonly>%s</textarea>\n' % H.escape(LOADER))

    A('<h2>手順3 — 全部入り（従来版）</h2>\n')
    A('<p>外部に置きたくない場合はこちら。%s文字あるので、iOSでは登録に失敗することがあります。</p>\n'
      % format(len(full), ','))
    A('<p><button class="cp" id="c3">📋 全部入りをコピー</button> <span id="m3"></span></p>\n')
    A('<textarea id="s3" readonly>javascript:%s</textarea>\n' % H.escape(full))

    A('<h2>最終手段 — CSPで弾かれる場合</h2>\n')
    A('<p>ブックマークレットが一切動かないサイトでは、次のどちらかになります。</p>\n')
    A('<table>\n<tr><th>PC</th><td>Chrome拡張の <b>Tampermonkey</b> にバンドルを登録'
      '（CSPの影響を受けません）</td></tr>\n'
      '<tr><th>iOS</th><td>Safari拡張の <b>Userscripts</b>（App Store・無料）に'
      'バンドルを登録</td></tr>\n</table>\n')
    A('<p style="font-size:.9rem;color:#aaa">どちらも「対象サイトで実行するスクリプト」として '
      '<code>autopilot.bundle.js</code> の中身をそのまま貼れば動きます。</p>\n')

    A('<h2>それでも動かないとき</h2>\n')
    A('<p>Safari の <b>設定 → Safari → 詳細 → Webインスペクタ</b> をオンにし、Macがあれば繋いで'
      'コンソールのエラーを見てください。無ければ、手順1の診断結果と'
      '「パネルが出るのか／出ないのか」「ログに何が出ているか」を教えてください。</p>\n')

    A('<script>\n'
      'function cp(b,t,m){document.getElementById(b).onclick=async()=>{\n'
      '  const ta=document.getElementById(t), ms=document.getElementById(m);\n'
      '  try{await navigator.clipboard.writeText(ta.value);\n'
      '    ms.textContent="✅ コピーしました";ms.style.color="#81c784";}\n'
      '  catch(e){ta.focus();ta.setSelectionRange(0,ta.value.length);\n'
      '    ms.textContent="枠を長押し →「すべて選択」→「コピー」";ms.style.color="#ffb74d";}\n'
      '};}\n'
      'cp("c2","s2","m2");cp("c3","s3","m3");\n'
      '</script>\n')

    out = ''.join(parts)
    io.open(os.path.join(ROOT, 'oasis_autopilot_setup.html'), 'w', encoding='utf-8').write(out)
    return out, full


if __name__ == '__main__':
    out, full = build()
    print('oasis_autopilot_setup.html: %s bytes' % format(len(out.encode()), ','))
    print('  診断用   : %d 文字' % len(DIAG))
    print('  ローダー : %d 文字' % len(LOADER))
    print('  全部入り : %s 文字' % format(len(full), ','))
