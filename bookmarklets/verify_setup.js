// oasis_autopilot_setup.html に埋め込んだ3種のブックマークレットを検証する
const fs = require('fs');
const h = fs.readFileSync(process.argv[2] || '../oasis_autopilot_setup.html', 'utf8');
const dec = s => s.replace(/&quot;/g, '"').replace(/&#x27;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

let ng = 0;
// href に埋めた2本
const hrefs = [...h.matchAll(/href="javascript:([\s\S]*?)"/g)].map(m => dec(m[1]));
// textarea に入れた2本
const tas = [...h.matchAll(/<textarea[^>]*>javascript:([\s\S]*?)<\/textarea>/g)].map(m => dec(m[1]));
const all = [...hrefs.map((s, i) => ['href#' + (i + 1), s]),
             ...tas.map((s, i) => ['textarea#' + (i + 1), s])];

for (const [name, js] of all) {
  try {
    new Function(js);
    console.log(`✅ ${name.padEnd(12)} 構文OK  ${js.length.toLocaleString().padStart(7)}文字`
      + `  改行:${/[\r\n]/.test(js)}`);
  } catch (e) { console.log(`❌ ${name}: ${e.message}`); ng++; }
}
if (!all.length) { console.log('❌ ブックマークレットが見つかりません'); ng++; }
console.log(ng ? `\n❌ ${ng}件が不正` : '\n✅ 埋め込んだブックマークレットはすべて有効');
process.exit(ng ? 1 : 0);
