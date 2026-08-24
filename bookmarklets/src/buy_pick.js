(async()=>{
// 手打ち専用の一括購入ブックマークレット（予測ツールを持っていない人向け）。
// buy.js との違いは**入力方法だけ**で、購入処理・上限チェック・二重購入防止は同じ。
// 貼り付け欄をやめて、出走馬をタップして買い目を組み立てる形にしている。
const BM_VER='1.0.0';
const B='https://api.oasis.red';
const q=new URLSearchParams(location.search);
const G=q.get('guild'),S=q.get('race')||q.get('schedule_id'),U=q.get('user'),T=q.get('token');
if(!G||!S){alert('おあしすっち券購入ページで実行してください');return;}
const TRI_UNIT=10000, WIN_UNIT=1000, TRI_PER_REQ=10, TRI_MAX_UNITS=20, WIN_MAX_UNITS=100;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const yen=n=>Number(n).toLocaleString();
const ov=document.createElement('div');
ov.style.cssText='position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.8);display:flex;align-items:flex-start;justify-content:center;font-family:sans-serif;overflow-y:auto;padding:12px 0';
ov.innerHTML='<div style="background:#1a1a2e;border:2px solid #e2b96f;border-radius:12px;padding:1rem;width:620px;max-width:96vw;color:#fff">'
+'<b style="color:#e2b96f">🎫 馬券を選んで購入 v'+BM_VER+'</b>'
+'<span id=_st style="float:right;font-size:.75rem"></span>'
+'<div id=_info style="font-size:.75rem;color:#aaa;margin:.35rem 0 .6rem"></div>'
+'<div style="display:flex;gap:.4rem;margin-bottom:.6rem">'
+'<button id=_m3 style="flex:1;padding:.5rem;border:none;border-radius:6px;font-weight:700;cursor:pointer">3連単</button>'
+'<button id=_m1 style="flex:1;padding:.5rem;border:none;border-radius:6px;font-weight:700;cursor:pointer">単勝</button>'
+'</div>'
+'<div id=_slots style="margin-bottom:.5rem"></div>'
+'<div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.5rem;font-size:.8rem">'
+'<span>口数</span>'
+'<button id=_um style="width:34px;height:34px;background:#444;color:#fff;border:none;border-radius:6px;font-size:1.1rem;cursor:pointer">−</button>'
+'<b id=_un style="min-width:2.2rem;text-align:center;color:#e2b96f;font-size:1rem">1</b>'
+'<button id=_up style="width:34px;height:34px;background:#444;color:#fff;border:none;border-radius:6px;font-size:1.1rem;cursor:pointer">＋</button>'
+'<span id=_ucost style="color:#aaa"></span>'
+'<button id=_add style="flex:1;margin-left:auto;padding:.5rem;background:#e2b96f;color:#1a1a2e;border:none;border-radius:6px;font-weight:700;cursor:pointer">カートに入れる</button>'
+'</div>'
+'<div style="display:flex;justify-content:space-between;align-items:center;font-size:.72rem;color:#aaa;margin-bottom:.25rem">'
+'<span>出走馬（タップで選択）</span>'
+'<button id=_sort style="padding:.2rem .5rem;background:#333;color:#aaa;border:1px solid #555;border-radius:4px;font-size:.7rem;cursor:pointer">並び: 人気順</button></div>'
+'<div id=_horses style="max-height:230px;overflow-y:auto;border:1px solid #444;border-radius:6px;padding:.3rem"></div>'
+'<div style="font-size:.72rem;color:#aaa;margin:.6rem 0 .25rem">カート</div>'
+'<div id=_cart style="min-height:2.2rem;max-height:160px;overflow-y:auto;border:1px solid #444;border-radius:6px;padding:.3rem;font-size:.78rem"></div>'
+'<div id=_sum style="margin-top:.5rem;padding:.5rem;background:#111;border:1px solid #444;border-radius:6px;font-size:.8rem"></div>'
+'<label id=_ck style="display:none;margin-top:.5rem;font-size:.78rem;cursor:pointer"><input type=checkbox id=_ok style="vertical-align:-1px"> 内容を確認しました（実際に rrc を消費します）</label>'
+'<button id=_b disabled style="display:none;width:100%;margin-top:.5rem;padding:.6rem;background:#2e7d32;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer;opacity:.5">🛒 購入する</button>'
+'<div id=_l style="font-size:.74rem;max-height:170px;overflow-y:auto;line-height:1.55;margin-top:.5rem"></div>'
+'<button id=_c style="width:100%;margin-top:.5rem;padding:.45rem;background:#444;color:#fff;border:none;border-radius:6px;cursor:pointer">✕ 閉じる</button>'
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
$('_info').textContent = (r0.distance||'')+'｜'+(r0.surface||'')+'｜'+(r0.ground||r0.track_condition||'')
  +'　'+pets.length+'頭';
if(!open) log('⚠ このレースは締め切られています。購入できません。','#ffb74d');

// 同名馬は #1 #2 を付けて区別（ゲーム側の表示と揃える）
const cnt={}; pets.forEach(h=>{cnt[h.name]=(cnt[h.name]||0)+1;});
const nameOf={}; { const s2={};
 pets.forEach(h=>{ if(cnt[h.name]>1){ s2[h.name]=(s2[h.name]||0)+1; nameOf[h.pet_id]=h.name+'#'+s2[h.name]; }
   else nameOf[h.pet_id]=h.name; }); }
const ownWin=pets.reduce((a,h)=>a+(Number(h.my_amount)||0),0)/WIN_UNIT;
// ページが持つパッシブ辞書があれば日本語名で出す
const DICT=(typeof PASSIVE_INFO!=='undefined'&&PASSIVE_INFO)||(window.PASSIVE_INFO)||{};
const plabel=c=>(c&&c!=='none')?((DICT[c]&&DICT[c].label)||c):'';

let mode='tri', units=1, slots=[null,null,null], cart=[], sortBy='odds', executed=false;

const capLeft=()=>{
 let t=0,w=0; cart.forEach(x=>{ if(x.type==='3連単')t+=x.units; else w+=x.units; });
 return {tri:TRI_MAX_UNITS-t, win:WIN_MAX_UNITS-Math.round(ownWin)-w, triU:t, winU:w};
};
const maxUnits=()=>Math.max(1, mode==='tri'?capLeft().tri:capLeft().win);

function drawMode(){
 const on='background:#e2b96f;color:#1a1a2e', off='background:#333;color:#aaa';
 $('_m3').style.cssText='flex:1;padding:.5rem;border:none;border-radius:6px;font-weight:700;cursor:pointer;'+(mode==='tri'?on:off);
 $('_m1').style.cssText='flex:1;padding:.5rem;border:none;border-radius:6px;font-weight:700;cursor:pointer;'+(mode==='win'?on:off);
 drawSlots(); drawUnits();
}
function drawSlots(){
 const lab=['1着','2着','3着'];
 if(mode==='tri'){
  $('_slots').innerHTML='<div style="display:flex;gap:.4rem">'+slots.map((s,i)=>
   '<div data-slot="'+i+'" style="flex:1;padding:.5rem;border:1px dashed '+(s?'#e2b96f':'#555')+';border-radius:6px;text-align:center;font-size:.8rem;cursor:pointer;background:'+(s?'#2a2a40':'transparent')+'">'
   +'<div style="font-size:.65rem;color:#aaa">'+lab[i]+'</div>'
   +(s?esc(nameOf[s])+' <span style="color:#ef5350">✕</span>':'<span style="color:#666">未選択</span>')+'</div>').join('')+'</div>';
  $('_slots').querySelectorAll('[data-slot]').forEach(el=>el.onclick=()=>{
   slots[+el.dataset.slot]=null; drawSlots(); drawHorses(); });
 }else{
  $('_slots').innerHTML='<div style="padding:.5rem;border:1px dashed '+(slots[0]?'#e2b96f':'#555')+';border-radius:6px;text-align:center;font-size:.8rem;background:'+(slots[0]?'#2a2a40':'transparent')+'">'
   +'<div style="font-size:.65rem;color:#aaa">単勝で買う馬</div>'
   +(slots[0]?esc(nameOf[slots[0]])+' <span id=_cl style="color:#ef5350;cursor:pointer">✕</span>':'<span style="color:#666">未選択</span>')+'</div>';
  if($('_cl'))$('_cl').onclick=()=>{slots[0]=null;drawSlots();drawHorses();};
 }
}
function drawUnits(){
 const m=maxUnits(); if(units>m)units=m; if(units<1)units=1;
 $('_un').textContent=units;
 const u=mode==='tri'?TRI_UNIT:WIN_UNIT;
 $('_ucost').textContent='= '+yen(units*u)+' rrc（残枠 '+m+'口）';
}
function drawHorses(){
 const list=pets.slice();
 if(sortBy==='odds')list.sort((a,b)=>(Number(a.odds)||999)-(Number(b.odds)||999));
 else list.sort((a,b)=>(a.gate||a.pet_id)-(b.gate||b.pet_id));
 $('_sort').textContent='並び: '+(sortBy==='odds'?'人気順':'枠番順');
 $('_horses').innerHTML=list.map(h=>{
  const picked=slots.includes(h.pet_id);
  const ps=[plabel(h.passive_skill),plabel(h.passive_skill_2)].filter(Boolean).join(' / ');
  const gear=(h.equipment||h.charm)?' 🎗️':'';
  return '<div data-id="'+h.pet_id+'" style="padding:.4rem .5rem;margin:.15rem 0;border-radius:6px;cursor:pointer;'
   +'background:'+(picked?'#2e7d32':'#222')+';border:1px solid '+(picked?'#81c784':'#333')+'">'
   +'<div style="display:flex;justify-content:space-between;font-size:.82rem">'
   +'<b>'+esc(nameOf[h.pet_id])+gear+'</b>'
   +'<span style="color:#e2b96f">'+(Number(h.odds)?Number(h.odds).toFixed(2)+'倍':'—')+'</span></div>'
   +'<div style="font-size:.68rem;color:#9a9aa8">SP '+(h.speed||0)+' / ST '+(h.stamina||0)+' / PW '+(h.power||0)
   +'　'+esc(h.condition_label||'')+(ps?'　'+esc(ps):'')
   +((Number(h.my_amount)||0)?'　<span style="color:#81c784">購入済 '+Math.round(Number(h.my_amount)/WIN_UNIT)+'口</span>':'')
   +'</div></div>';
 }).join('');
 $('_horses').querySelectorAll('[data-id]').forEach(el=>el.onclick=()=>pick(+el.dataset.id));
}
function pick(id){
 if(mode==='win'){ slots=[id,null,null]; }
 else{
  const at=slots.indexOf(id);
  if(at>=0){ slots[at]=null; }               // 選び直し
  else{ const i=slots.indexOf(null); if(i<0){ log('3着まで選択済みです。✕で外してから選び直してください。','#ffb74d'); return; } slots[i]=id; }
 }
 drawSlots(); drawHorses();
}
function drawCart(){
 if(!cart.length){ $('_cart').innerHTML='<span style="color:#666">まだ何も入っていません</span>'; }
 else $('_cart').innerHTML=cart.map((x,i)=>
  '<div style="display:flex;justify-content:space-between;align-items:center;padding:.25rem .35rem;border-bottom:1px solid #333">'
  +'<span>'+(x.type==='3連単'?'🎯':'🥇')+' '+esc(x.label)+'</span>'
  +'<span><b style="color:#e2b96f">'+x.units+'口</b> '
  +'<span data-del="'+i+'" style="color:#ef5350;cursor:pointer;padding:0 .3rem">✕</span></span></div>').join('');
 $('_cart').querySelectorAll('[data-del]').forEach(el=>el.onclick=()=>{
  cart.splice(+el.dataset.del,1); drawCart(); drawUnits(); });
 const c=capLeft();
 const total=c.triU*TRI_UNIT+c.winU*WIN_UNIT;
 $('_sum').innerHTML='<b style="color:#e2b96f">合計 '+yen(total)+' rrc</b>'
  +'<br>3連単 '+c.triU+'口 / '+TRI_MAX_UNITS+'口　単勝 '+c.winU+'口'
  +(ownWin?'（購入済 '+Math.round(ownWin)+'口）':'')+' / '+WIN_MAX_UNITS+'口';
 const bad=(c.tri<0)||(c.win<0)||!open||!cart.length;
 if(c.tri<0)$('_sum').innerHTML+='<br><span style="color:#ef5350">⛔ 3連単が上限を超えています</span>';
 if(c.win<0)$('_sum').innerHTML+='<br><span style="color:#ef5350">⛔ 単勝が上限を超えています</span>';
 $('_ck').style.display=bad?'none':'block';
 $('_b').style.display=bad?'none':'block';
 if(bad){ $('_ok').checked=false; $('_b').disabled=true; $('_b').style.opacity=.5; }
}
$('_m3').onclick=()=>{mode='tri';slots=[null,null,null];units=1;drawMode();drawHorses();};
$('_m1').onclick=()=>{mode='win';slots=[null,null,null];units=1;drawMode();drawHorses();};
$('_um').onclick=()=>{units--;drawUnits();};
$('_up').onclick=()=>{units++;drawUnits();};
$('_sort').onclick=()=>{sortBy=(sortBy==='odds')?'gate':'odds';drawHorses();};
$('_add').onclick=()=>{
 if(executed){ log('購入済みです。閉じて開き直してください。','#ffb74d'); return; }
 if(mode==='tri'){
  if(slots.some(x=>!x)){ log('1着・2着・3着をすべて選んでください。','#ffb74d'); return; }
  const ids=slots.slice(); const key=ids.join('-');
  const ex=cart.find(x=>x.type==='3連単'&&x.ids.join('-')===key);
  if(ex)ex.units+=units; else cart.push({type:'3連単',ids:ids,units:units,label:ids.map(i=>nameOf[i]).join(' → ')});
  slots=[null,null,null];
 }else{
  if(!slots[0]){ log('馬を選んでください。','#ffb74d'); return; }
  const id=slots[0];
  const ex=cart.find(x=>x.type==='単勝'&&x.ids[0]===id);
  if(ex)ex.units+=units; else cart.push({type:'単勝',ids:[id],units:units,label:nameOf[id]});
  slots=[null,null,null];
 }
 units=1; drawSlots(); drawHorses(); drawCart(); drawUnits();
};
$('_ok').onchange=e=>{
 const c=capLeft();
 const okToBuy = !executed && open && cart.length>0 && c.tri>=0 && c.win>=0 && e.target.checked;
 $('_b').disabled=!okToBuy; $('_b').style.opacity=okToBuy?1:.5;
};
$('_b').onclick=async()=>{
 if(executed){ log('この内容は購入済みです。買い直すには一度閉じて開き直してください。','#ffb74d'); return; }
 if(!cart.length){ log('カートが空です。','#ef5350'); return; }
 executed=true;
 $('_b').disabled=true; $('_b').style.opacity=.5; $('_add').disabled=true; $('_ok').disabled=true;
 let ok=0,ng=0,spent=0,bal=null,unsure=0;
 const send=async(item,n)=>{
  const isTri=item.type==='3連単';
  const url=B+(isTri?'/api/trifecta/buy':'/api/bet');
  const body=isTri
   ? {user:U,guild:G,race:parseInt(S),first:item.ids[0],second:item.ids[1],third:item.ids[2],amount:n*TRI_UNIT,token:T}
   : {user:U,guild:G,race:parseInt(S),pet_id:item.ids[0],amount:n*WIN_UNIT,token:T};
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let d={}; try{ d=await r.json(); }catch(e){}
  if(r.ok && d.status!=='error'){
   ok+=n; spent+=body.amount; if(typeof d.balance==='number') bal=d.balance;
   log('✅ '+item.type+' '+esc(item.label)+' '+n+'口','#81c784');
  }else{
   ng+=n; log('❌ '+item.type+' '+esc(item.label)+' '+n+'口 '+esc(d.detail||d.message||('HTTP '+r.status)),'#ef5350');
  }
 };
 for(const item of cart){
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
 try{ const rr=await(await fetch(B+'/api/race/by-id/'+G+'/'+S+'?user='+U)).json();
      const w=(rr.pets||[]).reduce((a,h)=>a+(Number(h.my_amount)||0),0);
      log('確認: このレースの単勝 購入済み合計 '+yen(w)+' rrc','#aaa'); }catch(e){}
 log('― 完了 ✅'+ok+'口 / ❌'+ng+'口　確定した使用額 '+yen(spent)+' rrc'
     +(unsure?'　<b style="color:#ffb74d">不明 '+yen(unsure)+' rrc</b>':'')
     +(bal!==null?'　残高 '+yen(bal)+' rrc':''),'#e2b96f');
 if(unsure) log('⚠ 通信エラーぶんは送信されている可能性があります。'
   +'買い直す前に必ずゲーム側の購入履歴を確認してください。','#ffb74d');
 $('_b').textContent='完了 ✅'+ok+' / ❌'+ng;
 $('_b').style.opacity=1;
};
drawMode(); drawHorses(); drawCart(); drawUnits();
log(pets.length+'頭 読み込み完了。馬をタップして買い目を作ってください。','#4caf50');
})();
