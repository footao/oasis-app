// ページに埋まっている辞書（PASSIVE_INFO のような コード→説明 のオブジェクト）を全部探して
// クリップボードにコピーする調査用ブックマークレット。装備・お守りの効果説明を探すのに使う。
//
//   javascript:(d=>{var s=d.createElement('script');s.src='https://cdn.jsdelivr.net/gh/footao/oasis-app@main/bookmarklets/src/dump_dicts.js';d.body.appendChild(s)})(document)
//
// DevTools のコンソールに中身をそのまま貼っても動く。
(() => {
const KEYWORD = /装備|お守り|equipment|charm|item|passive|skill|効果/i;
const out = { globals: {}, scripts: [], fetched: [] };

// ① window 直下の「コード→オブジェクト」形の辞書
for (const k of Object.keys(window)) {
  let v; try { v = window[k]; } catch (e) { continue; }
  if (!v || typeof v !== 'object' || Array.isArray(v)) continue;
  if (v instanceof Node || v === window || v instanceof Window) continue;
  const ks = Object.keys(v);
  if (ks.length < 2 || ks.length > 2000) continue;
  const first = v[ks[0]];
  if (first && typeof first === 'object') out.globals[k] = v;   // 辞書っぽい
  else if (KEYWORD.test(k)) out.globals[k] = v;                 // 名前が怪しい
}

// ② モジュールスコープに隠れている場合に備えて、インラインscriptの中身も見る。
//    window に出ていない辞書はここでしか拾えない。
for (const s of document.querySelectorAll('script')) {
  const t = s.textContent || '';
  if (t && KEYWORD.test(t)) out.scripts.push({ len: t.length, text: t });
}

// ③ 外部 js も、キーワードを含むものだけ取ってくる
const srcs = [...document.querySelectorAll('script[src]')].map(s => s.src);
Promise.all(srcs.map(u => fetch(u).then(r => r.text())
    .then(t => KEYWORD.test(t) ? { url: u, len: t.length, text: t } : null)
    .catch(() => null)))
  .then(rs => {
    out.fetched = rs.filter(Boolean);
    const txt = JSON.stringify(out, null, 1);
    const names = Object.keys(out.globals);
    const msg = `辞書 ${names.length}件: ${names.join(', ') || 'なし'}\n`
      + `インラインscript ${out.scripts.length}件 / 外部js ${out.fetched.length}件`;
    console.log('%cOasis dump_dicts', 'font-weight:700', out);
    // コンソールに貼って実行した場合、navigator.clipboard はタップの権限が無いので必ず失敗する。
    // DevTools の組み込み関数 copy() は権限が要らないので、あればそちらを使う。
    try { if (typeof copy === 'function') { copy(txt); alert('コピーしました\n\n' + msg); return; } } catch (e) {}
    navigator.clipboard.writeText(txt).then(() => alert('コピーしました\n\n' + msg), () => {
      // 最後の手: ファイルとして落とす（コピー権限が要らない）
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([txt], { type: 'application/json' }));
      a.download = 'oasis_dump.json'; document.body.appendChild(a); a.click(); a.remove();
      alert('コピーできないので oasis_dump.json として保存しました\n\n' + msg);
    });
  });
})();
