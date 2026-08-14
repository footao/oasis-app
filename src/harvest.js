(async () => {
// ===== おあしすっち 結果採取ブックマークレット =====
// 過去の確定レースを result API からまとめて取得し、
// logg フォルダに置けるテキスト（Discordログと同じ書式）を作る。
// これで公式スコアつきの学習データを増やせる。
const B='https://api.oasis.red';
const q=new URLSearchParams(location.search);
const G=q.get('guild'), U=q.get('user')||'';
const S=parseInt(q.get('race')||q.get('schedule_id'),10);
if(!G||!S){alert('おあしすっちのレースページ（guild と race がURLにある）で実行してください');return;}
// 何レースさかのぼるか（プロンプトで変更可・キャンセルで既定）
let N=120;
try{const v=prompt('何レースさかのぼって採取しますか？（確定済みのみ取得）',String(N));if(v!==null&&/^\d+$/.test(v.trim()))N=Math.max(1,Math.min(1000,parseInt(v.trim(),10)));}catch(e){}
const btn=document.createElement('div');
Object.assign(btn.style,{position:'fixed',top:'12px',right:'12px',zIndex:'99999',background:'#1a1a2e',color:'#e2b96f',padding:'10px 18px',borderRadius:'8px',fontFamily:'sans-serif',fontSize:'14px',fontWeight:'600',boxShadow:'0 4px 12px rgba(0,0,0,.4)',border:'1px solid #e2b96f',maxWidth:'70%'});
btn.textContent='📥 採取準備...';document.body.appendChild(btn);
// パッシブ辞書（コード→日本語名）。ページの辞書を最優先。
const DICT=(typeof PASSIVE_INFO!=='undefined'&&PASSIVE_INFO)||(window.PASSIVE_INFO)||{};
const unknown=new Set();
const label=code=>{
  if(code==null||code==='none'||code==='')return null;
  const d=DICT[code];
  if(d&&d.label)return d.label;
  unknown.add(code);return code;
};
const RANK=r=>r===1?'🥇':r===2?'🥈':r===3?'🥉':(r+'着');
const dstr=s=>String(s||'').replace(/-/g,'/');            // 2026-08-02 -> 2026/08/02
const jget=async u=>{try{const r=await fetch(u);if(!r.ok)return null;return await r.json();}catch(e){return null;}};
const groundOf=o=>{for(const k of ['ground','track_condition','ground_condition','baba','condition'])if(o&&o[k])return o[k];return '不明';};

// 1レース分のテキストを作る（確定していなければ null）
const oneRace=async(s)=>{
  const info=await jget(`${B}/api/race/by-id/${G}/${s}?user=${U}`);
  if(!info||!Array.isArray(info.pets)||!info.pets.length)return {sid:s,state:'nodata'};
  const date=info.race_date, time=info.race_time||'0:00';
  const dist=info.distance||'', surf=info.surface||'';
  const res=await jget(`${B}/api/race/result/${G}/${date}/${s}?user=${U}`);
  const rr=res&&Array.isArray(res.results)?res.results:null;
  if(!rr||!rr.length)return {sid:s,state:'unfinished'};
  const ground=groundOf(res)!=='不明'?groundOf(res):groundOf(info);
  // pet_id -> 着順/score
  const byId={};rr.forEach(r=>{byId[r.pet_id]={rank:r.rank,score:r.score};});
  // 同名対策（レース内で重複する名前に #n を付ける）
  const cnt={};info.pets.forEach(h=>{cnt[h.name]=(cnt[h.name]||0)+1;});
  const seen={};
  const rows=info.pets.map(h=>{
    const rs=byId[h.pet_id];
    if(!rs||rs.rank==null||rs.score==null)return null;
    let nm=h.display_name||h.name;
    if(cnt[h.name]>1){seen[h.name]=(seen[h.name]||0)+1;nm=`${h.name}#${seen[h.name]}`;}
    const ps=[label(h.passive_skill),label(h.passive_skill_2)].filter(Boolean);
    return {rank:rs.rank,score:rs.score,name:nm,speed:h.speed,power:h.power,
            stamina:h.stamina,cond:h.condition_label||'普通',passives:ps};
  }).filter(Boolean);
  if(!rows.length||rows.length!==info.pets.length)return {sid:s,state:'partial'};
  rows.sort((a,b)=>a.rank-b.rank);
  // レース番号として schedule_id をそのまま入れる。
  // ツール側の race_key は「日付＋時刻＋レース番号」で作るため、番号が無いと
  // 同日同時刻の別レース（とくに race_time が取れず 0:00 になったもの）が
  // 1レースに合成されてしまう。schedule_id は一意なので衝突しない。
  const lines=[`[${dstr(date)} ${time}] Oasis-API`,'',`🏁 第${s}レース 結果`,
              `🕘 ${time}｜${dist}｜${surf}｜${ground}`];
  rows.forEach(r=>{
    lines.push(`${RANK(r.rank)} ${r.name}`,`@Unknown`,
      `🏃 スピード ${r.speed}`,`🫀 スタミナ ${r.stamina}`,`💥 パワー ${r.power} `,
      `📊 score ${r.score}`,`📉 コンディション：${r.cond}`);
    if(r.passives.length)lines.push(`✨ パッシブ：${r.passives.join('、')}`);
  });
  lines.push('','');
  return {sid:s,state:'ok',date:date,text:lines.join('\n'),n:rows.length};
};

try{
  const blocks=[];
  let done=0, okCount=0, miss=0, unfinished=0, minDate=null, maxDate=null;
  const ids=[];for(let i=0;i<N;i++)ids.push(S-i);
  const BATCH=6, STOP_AFTER_MISSES=18;   // 連続欠番がこれだけ続いたら打ち切り
  let streak=0, stopped=false;
  for(let i=0;i<ids.length;i+=BATCH){
    const part=ids.slice(i,i+BATCH);
    const got=await Promise.all(part.map(oneRace));
    for(const g of got){
      done++;
      if(g.state==='ok'){okCount++;streak=0;blocks.push([g.sid,g.text]);
        if(!minDate||g.date<minDate)minDate=g.date;
        if(!maxDate||g.date>maxDate)maxDate=g.date;}
      else if(g.state==='unfinished'||g.state==='partial'){unfinished++;streak=0;}
      else {miss++;streak++;}
    }
    btn.textContent=`📥 採取中 ${done}/${N}（確定 ${okCount}）...`;
    // schedule_id は降順に遡るので、欠番が続く＝ギルド開始より前まで来た可能性が高い。
    // それ以上叩いても無駄なので打ち切る（APIへの無駄な負荷も避ける）。
    if(streak>=STOP_AFTER_MISSES){stopped=true;break;}
  }
  if(stopped) console.info('Oasis: 欠番が'+STOP_AFTER_MISSES+'件続いたため'+done+'件で打ち切りました');
  // schedule_id 昇順（古い→新しい）で並べる
  blocks.sort((a,b)=>a[0]-b[0]);
  const lo=blocks.length?blocks[0][0]:(S-done+1), hi=blocks.length?blocks[blocks.length-1][0]:S;
  const header=`# おあしすっち 結果採取（result API）\n`+
    `# guild=${G}  採取した schedule_id ${lo}〜${hi}  確定 ${okCount}レース`+
    `${stopped?`（欠番が続いたため ${done}/${N} で打切り）`:''}\n`+
    `# 期間 ${minDate||'?'} 〜 ${maxDate||'?'}\n\n`;
  const out=header+blocks.map(b=>b[1]).join('');
  let copied=true;
  try{await navigator.clipboard.writeText(out);}catch(e){copied=false;}
  if(!copied){
    const ta=document.createElement('textarea');
    Object.assign(ta.style,{position:'fixed',left:'2%',top:'8%',width:'96%',height:'74%',zIndex:'99998',fontSize:'11px',fontFamily:'monospace'});
    ta.value=out;document.body.appendChild(ta);ta.focus();ta.select();
    try{copied=document.execCommand('copy');}catch(e){}
    if(copied)ta.remove();else ta.title='Ctrl+A → Ctrl+C でコピー';
  }
  const warn=unknown.size?` ⚠未知コード:${[...unknown].join('/')}`:'';
  if(unknown.size)console.warn('Oasis: 辞書に無いパッシブコード:',[...unknown]);
  btn.style.background=okCount?'#1b5e20':'#e65100';btn.style.color='#fff';
  btn.style.borderColor=okCount?'#4caf50':'#ff9800';
  btn.textContent=`${copied?'✅':'📋'} 確定${okCount}レース採取 | 未確定${unfinished} | 欠番${miss}`+
    `${stopped?`（欠番が続いたため${done}/${N}で打切り）`:''} | `+
    `${copied?'コピー完了→txtで保存しloggへ':'下の枠を手動コピー'}${warn}`;
  setTimeout(()=>btn.remove(),unknown.size?15000:10000);
}catch(e){btn.style.background='#b71c1c';btn.style.borderColor='#ef5350';btn.textContent='❌ エラー: '+e.message;setTimeout(()=>btn.remove(),8000);}
})();
