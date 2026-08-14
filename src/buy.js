(async()=>{
const B='https://api.oasis.red';
const q=new URLSearchParams(location.search);
const G=q.get('guild'),S=q.get('race')||q.get('schedule_id'),U=q.get('user'),T=q.get('token');
if(!G||!S){alert('おあしすっち券購入ページで実行してください');return;}
const TRI_UNIT=10000, WIN_UNIT=1000, TRI_PER_REQ=10, TRI_MAX_UNITS=20, WIN_MAX_UNITS=100;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ov=document.createElement('div');
ov.style.cssText='position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;font-family:sans-serif';
ov.innerHTML='<div style="background:#1a1a2e;border:2px solid #e2b96f;border-radius:12px;padding:1.1rem;width:560px;max-width:96vw;max-height:92vh;overflow-y:auto;color:#fff">'
+'<b style="color:#e2b96f">🛒 一括購入 v2</b><span id=_st style="float:right;font-size:.75rem"></span>'
+'<p style="font-size:.75rem;color:#aaa;margin:.45rem 0">予測ツールの購入リストを貼り付け → 解析 → 内容を確認 → 購入</p>'
+'<textarea id=_in placeholder="✅A → B → C&#10;✅A → B → C   （同じ行を並べると口数になります）&#10;馬名 x3      （単勝）" style="width:100%;height:120px;background:#111;color:#e2b96f;border:1px solid #444;border-radius:6px;padding:.5rem;font-size:.78rem;resize:vertical"></textarea>'
+'<div style="display:flex;gap:.4rem;margin:.5rem 0">'
+'<button id=_p style="flex:1;padding:.45rem;background:#e2b96f;color:#1a1a2e;border:none;border-radius:6px;font-weight:700;cursor:pointer">解析</button>'
+'<button id=_c style="padding:.45rem .8rem;background:#444;color:#fff;border:none;border-radius:6px;cursor:pointer">✕</button></div>'
+'<div id=_l style="font-size:.74rem;max-height:210px;overflow-y:auto;line-height:1.55"></div>'
+'<div id=_sum style="display:none;margin-top:.6rem;padding:.5rem;background:#111;border:1px solid #444;border-radius:6px;font-size:.8rem"></div>'
+'<label id=_ck style="display:none;margin-top:.5rem;font-size:.78rem;cursor:pointer"><input type=checkbox id=_ok style="vertical-align:-1px"> 内容を確認しました（実際に rrc を消費します）</label>'
+'<button id=_b disabled style="display:none;width:100%;margin-top:.5rem;padding:.55rem;background:#2e7d32;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer;opacity:.5">🛒 購入する</button>'
+'</div>';
document.body.appendChild(ov);
const $=i=>document.getElementById(i);
const log=(m,c)=>{const e=$('_l');e.innerHTML+='<span style="color:'+(c||'#aaa')+'">'+m+'</span><br>';e.scrollTop=e.scrollHeight;};
$('_c').onclick=()=>ov.remove();

log('レースデータ取得中...');
let r0;
try{ r0=await(await fetch(B+'/api/race/by-id/'+G+'/'+S+'?user='+U)).json(); }
catch(e){ log('❌ 取得失敗: '+e.message,'#ef5350'); return; }
const pets=r0.pets||[];
// locked は「出走馬確定」であって投票締切ではない。締切判定は phase を見る。
 const open = r0.phase ? (r0.phase==='betting') : (r0.locked!==true);
$('_st').textContent = open?'🟢 受付中':'🔴 締切済み';
$('_st').style.color = open?'#81c784':'#ef5350';
if(!open) log('⚠ このレースは締め切られています（phase='+esc(r0.phase||'')+'）。購入できません。','#ffb74d');

// 名前 → pet_id。ゲームの #連番 / #pet_id / 素名 / 予測ツールの「 #n」すべて受ける
const cnt={}; pets.forEach(h=>{cnt[h.name]=(cnt[h.name]||0)+1;});
const seq={}; const idx={};
const ambiguous=new Set();
const put=(k,v)=>{k=String(k).trim(); if(k&&!(k in idx))idx[k]=v;};
pets.forEach(h=>{
  const dup=cnt[h.name]>1;
  if(dup){
    seq[h.name]=(seq[h.name]||0)+1; const n=seq[h.name];
    put(h.name+'#'+n,h.pet_id);
    put(h.name+' #'+n,h.pet_id);
    put(h.name+'\u2009#'+n,h.pet_id);
    ambiguous.add(h.name);
  } else {
    put(h.name,h.pet_id);
    if(h.display_name) put(h.display_name,h.pet_id);
  }
  put(h.name+'#'+h.pet_id,h.pet_id);
});
const nameOf={}; { const s2={};
 pets.forEach(h=>{ if(cnt[h.name]>1){ s2[h.name]=(s2[h.name]||0)+1; nameOf[h.pet_id]=h.name+'#'+s2[h.name]; }
   else nameOf[h.pet_id]=h.name; }); }
const ownWin=pets.reduce((a,h)=>a+(Number(h.my_amount)||0),0)/WIN_UNIT;
log(pets.length+'頭取得完了','#4caf50');

let plan=[], planOk=false;
$('_p').onclick=()=>{
  if(executed){ log('購入済みです。閉じて開き直してください。','#ffb74d'); return; }
  plan=[]; planOk=false; $('_l').innerHTML=''; $('_sum').style.display='none';
  $('_ck').style.display='none'; $('_b').style.display='none';
  $('_ok').checked=false; $('_b').disabled=true; $('_b').style.opacity=.5;
  if(!open) log('⚠ 締切済みのレースです。','#ffb74d');
  const tri={}, win={};
  const lines=$('_in').value.split('\n');
  let bad=0;
  for(let raw of lines){
    let l=raw.replace(/[✅◎●・]/g,'').trim();
    if(!l) continue;
    let mult=1;
    const mm=l.match(/[\sx×*]\s*(\d+)\s*口?\s*$/i);
    if(mm && !l.includes('→')){ mult=parseInt(mm[1]); l=l.slice(0,mm.index).trim(); }
    else { const m2=l.match(/(?:[x×*]\s*(\d+)|(\d+)\s*口)\s*$/i);
           if(m2){ mult=parseInt(m2[1]||m2[2]); l=l.slice(0,m2.index).trim(); } }
    if(l.includes('→')){
      const p=l.split('→').map(s=>s.replace(/\s{2,}[\s\S]*/,'').trim());
      if(p.length<3){ log('❌ 形式不明: '+esc(raw.trim()),'#ef5350'); bad++; continue; }
      const amb=p.slice(0,3).find(n=>ambiguous.has(n));
      if(amb){ log('❌ 同名馬がいるため「'+esc(amb)+'」だけでは特定できません。#1 / #2 を付けてください。','#ef5350'); bad++; continue; }
      const ids=p.slice(0,3).map(n=>idx[n]);
      if(ids.some(x=>!x)){ log('❌ 馬名が一致しません: '+esc(p.slice(0,3).join(' → ')),'#ef5350'); bad++; continue; }
      const k=ids.join('-'); tri[k]=(tri[k]||0)+mult;
    } else {
      if(ambiguous.has(l)){ log('❌ 同名馬がいるため「'+esc(l)+'」だけでは特定できません。#1 / #2 を付けてください。','#ef5350'); bad++; continue; }
      const id=idx[l];
      if(!id){ log('❌ 馬名が一致しません: '+esc(l),'#ef5350'); bad++; continue; }
      win[id]=(win[id]||0)+mult;
    }
  }
  let triU=0, winU=0;
  for(const k in tri){ const ids=k.split('-').map(Number); const u=tri[k]; triU+=u;
    plan.push({type:'3連単',ids:ids,units:u,label:ids.map(i=>nameOf[i]).join(' → ')});
    log('✅ 3連単 '+esc(ids.map(i=>nameOf[i]).join(' → '))+'　'+u+'口','#81c784'); }
  for(const id in win){ const u=win[id]; winU+=u;
    plan.push({type:'単勝',ids:[Number(id)],units:u,label:nameOf[id]});
    log('✅ 単勝 '+esc(nameOf[id])+'　'+u+'口','#81c784'); }
  if(!plan.length){ log('購入対象がありません。','#ffb74d'); return; }
  const total=triU*TRI_UNIT+winU*WIN_UNIT;
  $('_sum').style.display='block';
  $('_sum').innerHTML='<b style="color:#e2b96f">合計 '+total.toLocaleString()+' rrc</b><br>'
    +'3連単 '+triU+'口 × '+TRI_UNIT.toLocaleString()+' = '+(triU*TRI_UNIT).toLocaleString()+' rrc<br>'
    +'単勝 '+winU+'口 × '+WIN_UNIT.toLocaleString()+' = '+(winU*WIN_UNIT).toLocaleString()+' rrc'
    +(bad?'<br><span style="color:#ef5350">読めなかった行 '+bad+'（上の❌）</span>':'')
    +(ownWin?'<br><span style="color:#aaa">このレースで購入済みの単勝: '+Math.round(ownWin)+'口</span>':'');
  const overTri = triU>TRI_MAX_UNITS;
  const overWin = (winU+ownWin)>WIN_MAX_UNITS;
  if(overTri) $('_sum').innerHTML+='<br><span style="color:#ef5350">⛔ 3連単は1レース合計'
    +TRI_MAX_UNITS+'口まで（今回 '+triU+'口）。減らしてください。</span>';
  if(overWin) $('_sum').innerHTML+='<br><span style="color:#ef5350">⛔ 単勝は1レース合計'
    +WIN_MAX_UNITS+'口まで（購入済み '+Math.round(ownWin)+' + 今回 '+winU+' = '
    +Math.round(ownWin+winU)+'口）。減らしてください。</span>';
  if(overTri||overWin||!open){
    if(!open) log('締切済み（phase='+esc(r0.phase||'')+'）のため購入できません。','#ef5350');
    else log('上限を超えているため購入できません。口数を減らして解析し直してください。','#ef5350');
    plan=[]; planOk=false;
    $('_ck').style.display='none'; $('_b').style.display='none';
    $('_ok').checked=false; $('_b').disabled=true; $('_b').style.opacity=.5;
    return;
  }
  planOk=true;
  $('_ck').style.display='block'; $('_b').style.display='block';
};
let executed=false;
$('_ok').onchange=e=>{
 const okToBuy = !executed && planOk && plan.length>0 && e.target.checked;
 $('_b').disabled=!okToBuy; $('_b').style.opacity=okToBuy?1:.5;
};

$('_b').onclick=async()=>{
  if(executed){ log('この内容は購入済みです。買い直すには一度閉じて開き直してください。','#ffb74d'); return; }
  if(!planOk || !plan.length){ log('購入できる内容がありません。','#ef5350'); return; }
  executed=true;
  $('_b').disabled=true; $('_b').style.opacity=.5; $('_p').disabled=true; $('_ok').disabled=true;
  let ok=0,ng=0,spent=0,bal=null,unsure=0;
  const send=async(item,units)=>{
    const isTri=item.type==='3連単';
    const url=B+(isTri?'/api/trifecta/buy':'/api/bet');
    const body=isTri
      ? {user:U,guild:G,race:parseInt(S),first:item.ids[0],second:item.ids[1],third:item.ids[2],amount:units*TRI_UNIT,token:T}
      : {user:U,guild:G,race:parseInt(S),pet_id:item.ids[0],amount:units*WIN_UNIT,token:T};
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let d={}; try{ d=await r.json(); }catch(e){}
    if(r.ok && d.status!=='error'){
      ok+=units; spent+=body.amount; if(typeof d.balance==='number') bal=d.balance;
      log('✅ '+item.type+' '+esc(item.label)+' '+units+'口','#81c784');
    } else {
      ng+=units;
      log('❌ '+item.type+' '+esc(item.label)+' '+units+'口 '+esc(d.detail||d.message||('HTTP '+r.status)),'#ef5350');
    }
  };
  for(const item of plan){
    let left=item.units;
    while(left>0){
      const n=Math.min(left, item.type==='3連単'?TRI_PER_REQ:20);
      try{ await send(item,n); }
      catch(e){ ng+=n; unsure+=n*(item.type==='3連単'?TRI_UNIT:WIN_UNIT);
        log('⚠ '+item.type+' '+esc(item.label)+' '+n+'口 通信エラー: '+esc(e.message)
          +'　<b>送信済みかどうか不明</b>です。購入履歴で確認してください。','#ffb74d'); }
      left-=n;
      await new Promise(r=>setTimeout(r,350));
    }
  }
  // 最終残高を取り直して、実際にいくら減ったか照合できるようにする
  let balEnd=null;
  try{ const rr=await(await fetch(B+'/api/race/by-id/'+G+'/'+S+'?user='+U)).json();
       const w=(rr.pets||[]).reduce((a,h)=>a+(Number(h.my_amount)||0),0);
       log('確認: このレースの単勝 購入済み合計 '+w.toLocaleString()+' rrc','#aaa'); }catch(e){}
  log('― 完了 ✅'+ok+'口 / ❌'+ng+'口　確定した使用額 '+spent.toLocaleString()+' rrc'
      +(unsure?'　<b style="color:#ffb74d">不明 '+unsure.toLocaleString()+' rrc</b>':'')
      +(bal!==null?'　残高 '+bal.toLocaleString()+' rrc':''),'#e2b96f');
  if(unsure) log('⚠ 通信エラーぶんは送信されている可能性があります。'
    +'買い直す前に必ずゲーム側の購入履歴を確認してください。','#ffb74d');
  $('_b').textContent='完了 ✅'+ok+' / ❌'+ng;
  $('_b').style.opacity=1;
};
})();
