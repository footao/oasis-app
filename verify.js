const fs=require('fs');
const checks=[
 ['oasis_bookmarklet_v2.html','🏇 レースデータ取得'],
 ['oasis_buy_bookmarklet_v2.html','🛒 一括購入 v2'],
 ['oasis_probe_bookmarklet.html','🔬 単勝プール実測'],
 ['oasis_harvest_bookmarklet.html','📥 結果を採取'],
];
let allok=true;
for(const [f,mark] of checks){
  const h=fs.readFileSync(f,'utf8');
  const re=new RegExp('href="javascript:([\\s\\S]*?)">'+mark);
  const m=h.match(re);
  if(!m){ console.log('❌ '+f+' : href が見つからない'); allok=false; continue; }
  const js=m[1]
    .replace(/&quot;/g,'"').replace(/&#x27;/g,"'")
    .replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/&amp;/g,'&');
  try{ new Function(js); console.log('✅ '+f.padEnd(34)+' 構文OK  '+String(js.length).padStart(5)+'文字  改行:'+/[\r\n]/.test(js)); }
  catch(e){ console.log('❌ '+f+' : '+e.message); allok=false; }
}
console.log(allok?`\n${checks.length}つとも有効なブックマークレット`:'\n要修正');
