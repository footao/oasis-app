(async()=>{
const B='https://api.oasis.red';
const q=new URLSearchParams(location.search);
const G=q.get('guild'),S=q.get('race')||q.get('schedule_id'),U=q.get('user'),T=q.get('token');
if(!G||!S){alert('おあしすっち券購入ページで実行してください');return;}
const WIN_UNIT=1000, MAX_PROBE=5, ODD_STEP=0.01, TARGET_ERR=0.04;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const csv=v=>{const s=(v==null?'':String(v)).replace(/\r?\n/g,' ');return /[",]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const ov=document.createElement('div');
ov.style.cssText='position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;font-family:sans-serif';
ov.innerHTML='<div style="background:#1a1a2e;border:2px solid #e2b96f;border-radius:12px;padding:1.1rem;width:520px;max-width:96vw;max-height:92vh;overflow-y:auto;color:#fff">'
+'<b style="color:#e2b96f">🔬 単勝プール実測 + データ取得</b><span id=_st style="float:right;font-size:.75rem"></span>'
+'<div id=_l style="font-size:.76rem;max-height:300px;overflow-y:auto;line-height:1.6;margin-top:.5rem"></div>'
+'<div id=_ask style="display:none;margin-top:.7rem;padding:.6rem;background:#111;border:1px solid #e2b96f;border-radius:6px;font-size:.8rem"></div>'
+'<div style="display:flex;gap:.4rem;margin-top:.6rem">'
+'<button id=_go style="display:none;flex:1;padding:.5rem;background:#2e7d32;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer">実行する</button>'
+'<button id=_c style="padding:.5rem .9rem;background:#444;color:#fff;border:none;border-radius:6px;cursor:pointer">✕ 閉じる</button></div></div>';
document.body.appendChild(ov);
const $=i=>document.getElementById(i);
const log=(m,c)=>{const e=$('_l');e.innerHTML+='<span style="color:'+(c||'#aaa')+'">'+m+'</span><br>';e.scrollTop=e.scrollHeight;};
$('_c').onclick=()=>ov.remove();

try{
const getRace=async()=>(await(await fetch(B+'/api/race/by-id/'+G+'/'+S+'?user='+U)).json());
let r0;
log('レースデータ取得中…');
try{ r0=await getRace(); }catch(e){ log('❌ 取得失敗: '+e.message,'#ef5350'); return; }
// locked は「出走馬確定」であって投票締切ではない。締切判定は phase を見る。
 const open = r0.phase ? (r0.phase==='betting') : (r0.locked!==true);
$('_st').textContent=open?'🟢 受付中':'🔴 締切済み';
$('_st').style.color=open?'#81c784':'#ef5350';
const pets0=r0.pets||[];
log(pets0.length+'頭 / '+esc(r0.distance||'')+'・'+esc(r0.surface||''),'#4caf50');

// odds=1.5 は「まだ誰も賭けていない」プレースホルダ（実測で確認）。
// 実オッズを持つ馬＝測定の基準になる馬なので、そこを潰さないように試し買い先を選ぶ。
const PH=1.5;
const isPh=h=>Number(h.odds)===PH;
const real=pets0.filter(h=>!isPh(h)&&isFinite(Number(h.odds))&&Number(h.odds)>0);
const unbet=pets0.filter(isPh);
const own=pets0.reduce((s,h)=>s+(Number(h.my_amount)||0),0);
// 分かっている下限: どの馬でも P >= od_j * (その馬への自分の投入額)
let lower=0;
real.forEach(h=>{ const m=Number(h.my_amount)||0; if(m>0) lower=Math.max(lower,Number(h.odds)*m); });

let probe=null, mode='normal';
if(real.length===0){ mode='empty'; }
else if(real.length===1){ mode='single'; probe=unbet[0]||null; if(!probe) mode='empty'; }
else { probe=real[0]; real.forEach(h=>{ if(Number(h.odds)<Number(probe.odds)) probe=h; }); }
const already=own;
$('_ask').style.display='block';
if(mode==='empty' || !probe){
 $('_ask').innerHTML='<b style="color:#ffb74d">単勝プールが空です（まだ誰も賭けていません）。</b><br>'
  +'全馬のオッズが初期値 '+PH+' のままなので、動かすものがなく測定できません。<br>'
  +'この状態で単勝を買っても<b>自分の掛け金を自分で取り返すだけ</b>（実効オッズ 1.0）で、'
  +'期待値はマイナスです。<br><span style="color:#aaa;font-size:.92em">'
  +'締切前に他の人の投票が入ってから実行してください。試し買いはせず、データ取得だけ行います。</span>';
 $('_go').textContent='データ取得だけ実行';
}else{
 $('_ask').innerHTML='試し買いの対象: <b style="color:#e2b96f">'+esc(probe.display_name||probe.name)
  +'</b>（単勝オッズ '+esc(probe.odds)
  +(mode==='single'?' ＝ 未投票の馬':' ＝ 実オッズのある馬で最低')+'）<br>'
  +'<b>1口ずつ・最大 '+MAX_PROBE+'口（'+(MAX_PROBE*WIN_UNIT).toLocaleString()+' rrc）</b>まで購入します。'
  +'<br><span style="color:#aaa;font-size:.92em">オッズは小数2桁までしか出ないため1口では精度が出ません。'
  +'推定精度が ±'+Math.round(TARGET_ERR*200)+'%（95%目安）を切った時点で自動的に止めます。</span>'
  +(already?'<br>このレースで購入済み: '+already.toLocaleString()+' rrc':'')
  +(lower?'<br>現時点で分かるプールの下限: <b>'+Math.round(lower).toLocaleString()+' rrc 以上</b>':'')
  +(mode==='single'?'<br><span style="color:#ffb74d">⚠ 実オッズを持つ馬が1頭しかありません。'
     +'その馬を測定の基準として残すため、未投票の馬に試し買いします。'
     +'プールが小さい可能性が高いので結果をよく確認してください。</span>':'')
  +(open?'':'<br><span style="color:#ef5350">⚠ 締切済み（phase='+esc(r0.phase||'')+'）なので購入は通りません。</span>');
}
$('_go').style.display='block';

$('_go').onclick=async()=>{
 $('_go').disabled=true; $('_go').style.opacity=.5; $('_ask').style.display='none';
 const odds0={}; pets0.forEach(h=>{odds0[h.pet_id]=Number(h.odds);});
 const D_unit=WIN_UNIT;
 let spent=0, pool=null, relErr=null, spread=null, ests=[], rows=[], pets1=pets0, lastBal=null;

 // od の丸め（±ODD_STEP/2 の一様分布）を踏まえた重み付き推定。
 // ratio_j = od_j後/od_j前 は買っていない馬すべてで (P+Δ)/P に等しい。
 // 丸め誤差は od に反比例するので、重み od^2 で平均すると分散最小になる。
 const estimate=(petsNow, D)=>{
  let sw=0, sr=0, n=0; const per=[];
  petsNow.forEach(h=>{
   const a=odds0[h.pet_id], b=Number(h.odds), isP=(probe && h.pet_id===probe.pet_id);
   let e=null,note=isP?'試し買いした馬':'';
   if(!isFinite(a)||!isFinite(b)||a<=0||b<=0){ note=note||'オッズ不明'; }
   else if(!isP){
    const ra=b/a;
    if(Math.abs(ra-1)>1e-12){ const w=b*b; sw+=w; sr+=w*ra; n++; e=D/(ra-1); }
    else note='動かず';
   }
   per.push({id:h.pet_id,name:h.display_name||h.name,before:a,after:b,est:e,note:note});
  });
  if(!n||sw<=0) return {pool:null,relErr:null,spread:null,per:per,n:0};
  const R=sr/sw;
  if(!(R>1)) return {pool:null,relErr:null,spread:null,per:per,n:n};
  const P=D/(R-1);
  // 重み付き平均の標準偏差
  const sdR=(ODD_STEP/Math.sqrt(12))*Math.sqrt(2/sw);
  // P の相対誤差
  const rel=sdR*P/D;
  const vals=per.map(x=>x.est).filter(x=>x&&isFinite(x)&&x>0).sort((a,b)=>a-b);
  const sp=vals.length>1?(vals[vals.length-1]-vals[0])/P:0;
  return {pool:P,relErr:rel,spread:sp,per:per,n:n};
 };

 if(mode==='empty'){
  log('単勝プールが空のため試し買いはスキップしました。','#ffb74d');
 }
 log(mode==='empty'?'― データ取得のみ ―':'― 試し買い（1口ずつ・最大'+MAX_PROBE+'口）―','#e2b96f');
 for(let round=1; mode!=='empty' && probe && round<=MAX_PROBE; round++){
  try{
   const res=await fetch(B+'/api/bet',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({user:U,guild:G,race:parseInt(S),pet_id:probe.pet_id,amount:D_unit,token:T})});
   let d={}; try{ d=await res.json(); }catch(e){}
   if(!res.ok||d.status==='error'){
    log('❌ '+round+'口目 購入失敗: '+esc(d.detail||d.message||('HTTP '+res.status)),'#ef5350');
    break;
   }
   spent+=D_unit;
   if(typeof d.balance==='number') lastBal=d.balance;
  }catch(e){ log('❌ 購入エラー: '+esc(e.message),'#ef5350'); break; }

  let moved=false;
  for(let t=0;t<3 && !moved;t++){
   await sleep(t===0?900:1100);
   try{ const rr=await getRace(); if(rr&&rr.pets){ pets1=rr.pets; } }catch(e){ continue; }
   moved=pets1.some(h=>{const a=odds0[h.pet_id],b=Number(h.odds);
     return isFinite(a)&&isFinite(b)&&Math.abs(b-a)>1e-9;});
  }
  const r=estimate(pets1, spent);
  pool=r.pool; relErr=r.relErr; spread=r.spread; rows=r.per;
  ests=r.per.map(x=>x.est).filter(Boolean);
  if(pool===null){
   log(round+'口目（累計 '+spent.toLocaleString()+' rrc）→ まだオッズが動かず推定不能','#888');
  }else{
   log(round+'口目（累計 '+spent.toLocaleString()+' rrc）→ 現在のプール推定 '
       +Math.round(pool+spent).toLocaleString()+' rrc　精度 ±'+(relErr*200).toFixed(0)+'%',
       relErr<=TARGET_ERR?'#81c784':'#aaa');
   if(relErr<=TARGET_ERR){ log('✅ 目標精度に到達したので試し買いを終了します。','#81c784'); break; }
  }
 }
 if(lastBal!==null) log('残高 '+lastBal.toLocaleString()+' rrc　（試し買い合計 '+spent.toLocaleString()+' rrc）','#aaa');
 if(pool===null){ log('⚠ オッズが動かず推定できませんでした（プールが空 / 締切済み など）','#ffb74d'); }
 else if(relErr>TARGET_ERR){ log('△ 推定精度 ±'+(relErr*200).toFixed(0)+'%。プールが大きいと1口の影響が'
   +'小さく精度が出ません。必要ならもう一度実行してください（累積で精度が上がります）。','#ffb74d'); }

 // ここから通常のデータ取得（3連単は試し買いの影響を受けないので1回だけ）
 const DICT=(typeof PASSIVE_INFO!=='undefined'&&PASSIVE_INFO)||window.PASSIVE_INFO||{};
 const unknown=new Set();
 const info=c=>{ if(c==null||c==='none')return null; const d=DICT[c];
   if(d)return{code:c,label:d.label,desc:d.description||''}; unknown.add(c); return{code:c,label:c,desc:''}; };
 const dist=r0.distance||'', surf=r0.surface||'', ground=r0.ground||r0.track_condition||'';
 const cnt={}; pets1.forEach(h=>{cnt[h.name]=(cnt[h.name]||0)+1;});
 const seen={};
 const pets=pets1.map(h=>{ let dn=h.display_name||h.name;
   if(cnt[h.name]>1){seen[h.name]=(seen[h.name]||0)+1;dn=h.name+'#'+seen[h.name];}
   return {...h,displayName:dn,p1:info(h.passive_skill),p2:info(h.passive_skill_2)}; });
 const effRows=[],es=new Set();
 pets.forEach(h=>[h.p1,h.p2].forEach(p=>{ if(p&&!es.has(p.label)){es.add(p.label);
   effRows.push(csv(p.label)+','+csv(p.code)+','+csv(p.desc));} }));
 const horseHeader='レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額';
 const horseRows=pets.map(h=>[dist,surf,ground,h.displayName,h.adult_key||'',h.speed,h.power,h.stamina,
   h.condition_label,(h.p1?h.p1.label:''),(h.p2?h.p2.label:''),h.odds,(h.my_amount||0)].map(csv).join(','));
 const nameById={}; pets.forEach(h=>{nameById[h.pet_id]=h.displayName;});
 const probeRows=rows.map(r=>[nameById[r.id]||r.name,
   (isFinite(r.before)?r.before:''),(isFinite(r.after)?r.after:''),
   (r.est?Math.round(r.est):''),r.note].map(csv).join(','));

 const n=pets.length;
 let withOdds=[],noOdds=[];
 if(n>=3){
  const combos=[];
  for(const a of pets)for(const b of pets){ if(b.pet_id===a.pet_id)continue;
    for(const x of pets){ if(x.pet_id===a.pet_id||x.pet_id===b.pet_id)continue; combos.push([a,b,x]); } }
  for(let i=0;i<combos.length;i+=20){
   const got=await Promise.all(combos.slice(i,i+20).map(async([a,b,x])=>{ try{
     const d=await(await fetch(B+'/api/trifecta/odds?guild='+G+'&schedule_id='+S+'&first='+a.pet_id+'&second='+b.pet_id+'&third='+x.pet_id)).json();
     return{first:a.displayName,second:b.displayName,third:x.displayName,odds:typeof d.odds==='number'?d.odds:null};
   }catch(e){ return null; } }));
   got.filter(Boolean).forEach(r=>{ (r.odds!==null?withOdds:noOdds).push(r); });
   log('3連単 '+Math.min(i+20,combos.length)+'/'+combos.length+' 取得中…','#888');
  }
  withOdds.sort((a,b)=>a.odds-b.odds);
 }
 let poolAmt=0;
 try{ poolAmt=(await(await fetch(B+'/api/trifecta/pool?guild='+G+'&schedule_id='+S)).json()).pool||0; }catch(e){}

 const head=['guild='+G,'schedule_id='+S,'pool='+poolAmt];
 // own は試し買い後の自分の総投入額（＝出力するオッズと同じ時点に揃える）
 const ownNow=pets1.reduce((a,h)=>a+(Number(h.my_amount)||0),0) || (own+spent);
 head.push('win_market='+mode,'win_own='+ownNow);
 if(lower>0) head.push('win_pool_min='+Math.round(lower));
 // pool は試し買い"前"の総額。出力するオッズは試し買い"後"なので、対応させて spent を足す。
 if(pool!==null){ head.push('win_pool='+Math.round(pool+spent),'win_pool_before='+Math.round(pool),
   'win_pool_delta='+spent,
   'win_pool_n='+rows.filter(r=>r.est).length,
   'win_pool_err='+(relErr!==null?relErr.toFixed(4):''),
   'win_pool_spread='+(spread!==null?spread.toFixed(4):'')); }
 const clip=head.concat(['',
  '=== 出走馬一覧 ===',horseHeader,...horseRows,'',
  '=== パッシブ効果 ===','パッシブ,コード,説明',...effRows,'',
  '=== 単勝プール実測 ===','馬名,試し買い前,試し買い後,この馬からの推定,備考',...probeRows,'',
  '=== 3連単オッズ ===','順位,1着,2着,3着,オッズ',
  ...withOdds.map((r,i)=>(i+1)+','+csv(r.first)+','+csv(r.second)+','+csv(r.third)+','+r.odds),
  ...noOdds.map(r=>'未成立,'+csv(r.first)+','+csv(r.second)+','+csv(r.third)+',未成立')]).join('\n');

 let copied=true;
 try{ await navigator.clipboard.writeText(clip); }catch(e){ copied=false; }
 if(!copied){ const ta=document.createElement('textarea');
   Object.assign(ta.style,{position:'fixed',left:'2%',top:'8%',width:'96%',height:'70%',zIndex:'100000',fontSize:'11px',fontFamily:'monospace'});
   ta.value=clip; document.body.appendChild(ta); ta.focus(); ta.select();
   try{ copied=document.execCommand('copy'); }catch(e){}
   if(copied) ta.remove();
 }
 log('― 完了 ―','#e2b96f');
 log((copied?'✅ クリップボードにコピーしました':'📋 下の枠を手動でコピーしてください')
   +'　'+n+'頭 / 3連単'+withOdds.length+'件'
   +(pool!==null?' / 推定単勝プール '+Math.round(pool+spent).toLocaleString()+' rrc ±'+(relErr*200).toFixed(0)+'%':''),'#81c784');
 if(unknown.size) log('⚠ 辞書に無いコード: '+esc([...unknown].join('/')),'#ffb74d');
 $('_go').textContent='完了';
};
} catch(e){ try{ log('❌ 想定外のエラー: '+esc(e&&e.message||e),'#ef5350'); }catch(_){ alert('エラー: '+e); } }
})();
