// oasis_autopilot.html に埋め込まれたブックマークレットの構文検証
const fs = require('fs');
const h = fs.readFileSync(process.argv[2] || 'oasis_autopilot.html', 'utf8');
const m = h.match(/href="javascript:([\s\S]*?)">🛩/);
if (!m) { console.log('❌ href が見つかりません'); process.exit(1); }
const js = m[1].replace(/&quot;/g, '"').replace(/&#x27;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
try {
  new Function(js);
  console.log('✅ 構文OK  ' + js.length.toLocaleString() + '文字  改行:' + /[\r\n]/.test(js));
  console.log('   OasisModel 定義:', /OasisModel/.test(js));
  console.log('   IIFE で包まれている:', js.trim().startsWith('(()=>{'));
} catch (e) { console.log('❌ ' + e.message); process.exit(1); }
