(async () => {
const B='https://api.oasis.red';
const q=new URLSearchParams(location.search);
const G=q.get('guild'), S=q.get('race')||q.get('schedule_id'), U=q.get('user');
if(!G||!S){alert('おあしすっち券購入ページで実行してください');return;}
const btn=document.createElement('div');
Object.assign(btn.style,{position:'fixed',top:'12px',right:'12px',zIndex:'99999',background:'#1a1a2e',color:'#e2b96f',padding:'10px 18px',borderRadius:'8px',fontFamily:'sans-serif',fontSize:'14px',fontWeight:'600',boxShadow:'0 4px 12px rgba(0,0,0,.4)',border:'1px solid #e2b96f'});
btn.textContent='🏇 取得中...';document.body.appendChild(btn);
const csv=v=>{const s=(v==null?'':String(v)).replace(/\r?\n/g,' ');return /[",]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;};
try{
 // ページが持つパッシブ辞書（コード→名前・説明）をそのまま使う。無ければ最低限の辞書。
 const DICT=(typeof PASSIVE_INFO!=='undefined'&&PASSIVE_INFO)||(window.PASSIVE_INFO)||{};
 const dictOK=Object.keys(DICT).length>0;
 const unknown=new Set();
 const info=code=>{
   if(code==null||code==='none')return null;
   const d=DICT[code];
   if(d)return{code:code,label:d.label,emoji:d.emoji||'',desc:d.description||''};
   unknown.add(code);return{code:code,label:code,emoji:'',desc:''};
 };
 btn.textContent='🏇 レースデータ取得中...';
 const race=await(await fetch(`${B}/api/race/by-id/${G}/${S}?user=${U}`)).json();
 const raw=race.pets||[], dist=race.distance||'', surf=race.surface||'';
 const ground=race.ground||race.track_condition||race.ground_condition||'';
 const cnt={};raw.forEach(h=>{cnt[h.name]=(cnt[h.name]||0)+1;});
 const seen={};
 const pets=raw.map(h=>{
   let dn=h.display_name||h.name;
   if(cnt[h.name]>1){seen[h.name]=(seen[h.name]||0)+1;dn=`${h.name}#${seen[h.name]}`;}
   const p1=info(h.passive_skill), p2=info(h.passive_skill_2);
   return {...h,displayName:dn,p1:p1,p2:p2};
 });
 // パッシブ効果セクション（このレースに出ているスキルの説明文）
 const effRows=[],effSeen=new Set();
 pets.forEach(h=>[h.p1,h.p2].forEach(p=>{
   if(p&&!effSeen.has(p.label)){effSeen.add(p.label);
     effRows.push(`${csv(p.label)},${csv(p.code)},${csv(p.desc)}`);}
 }));
 const horseHeader='レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額';
 const horseRows=pets.map(h=>[dist,surf,ground,h.displayName,h.adult_key||'',h.speed,h.power,h.stamina,h.condition_label,(h.p1?h.p1.label:''),(h.p2?h.p2.label:''),h.odds,(h.my_amount||0)].map(csv).join(','));
 const n=pets.length, total=n*(n-1)*(n-2);
 // ---- 打ち切り条件のためにプールを先に見る ----
 // 表示オッズは (プール総額 − 初期プール金20万) 基準なので 賭け金[組]=BASE/オッズ。
 // 取得済みの Σ(BASE/od) が「もう見つけた金額」で、BASE から引いた残りが1口(10,000rrc)
 // 未満になったら、**未取得の組には1口も入っていないと確定する**ので打ち切ってよい。
 // 判定に追加リクエストは不要。全組の Σ(1/od) は必ず 1.000 になる。
 btn.textContent='🏇 プール確認中...';
 let pool0=0;
 try{pool0=(await(await fetch(`${B}/api/trifecta/pool?guild=${G}&schedule_id=${S}`)).json()).pool||0;}catch{}
 const SEED=200000, UNIT=10000, BASE=Math.max(pool0-SEED,0);
 // 金が乗っていそうな順に取りたい。単勝オッズは下限1.5に張り付いていて
 // （直近20レースの45%が全馬同値）人気の代理にならないので使わない。
 //   初期順  : SPEED の高い組から（人が見て強そうな順の代わり）
 //   以降    : 金が乗っていた組に出ていた馬を重くして、取りながら並べ替える
 // 順番が外れても打ち切り条件（残額<1口）は変わらないので、遅くなるだけで結果は同じ。
 const sp=h=>Math.max(Number(h.speed)||1,1);
 const w=new Map(pets.map(h=>[h.pet_id,1]));
 const sc=c=>w.get(c[0].pet_id)*w.get(c[1].pet_id)*w.get(c[2].pet_id);
 btn.textContent=`🏇 3連単 ${total}通り 取得中...`;
 const combos=[];
 for(const a of pets)for(const b of pets){if(b.pet_id===a.pet_id)continue;
   for(const x of pets){if(x.pet_id===a.pet_id||x.pet_id===b.pet_id)continue;combos.push([a,b,x]);}}
 combos.sort((p,q)=>sp(q[0])*sp(q[1])*sp(q[2])-sp(p[0])*sp(p[1])*sp(p[2]));
 const queue=combos.slice(), results=[]; let seenAmt=0, cut=0;
 while(queue.length){
   const batch=queue.splice(0,20);
   const got=await Promise.all(batch.map(async([a,b,x])=>{try{
     const d=await(await fetch(`${B}/api/trifecta/odds?guild=${G}&schedule_id=${S}&first=${a.pet_id}&second=${b.pet_id}&third=${x.pet_id}`)).json();
     return{first:a.displayName,second:b.displayName,third:x.displayName,odds:typeof d.odds==='number'?d.odds:null};
   }catch{return null;}}));
   results.push(...got.filter(Boolean));
   cut+=batch.length;
   // 取りこぼした組は seenAmt に入らない＝残額を多めに見積もる方向なので、
   // 打ち切りが早まることはない（安全側）。
   // 表示オッズは小数2桁なので 賭け金=BASE/od には最大 0.005/od の相対誤差が乗る。
   // 積み上がった誤差 err のぶん残額を多めに見て、取り逃しが起きない側に倒す。
   let err=0;
   seenAmt=results.reduce((s,r)=>{if(!r.odds)return s;
     const b=BASE/r.odds;err+=b*0.005/r.odds;return s+b;},0);
   if(BASE>0&&BASE-seenAmt+err<UNIT)break;
   // 当たった組の馬を重くして残りを並べ替える
   let hit=false;
   got.forEach((r,k)=>{if(r&&r.odds!==null){hit=true;
     for(const h of batch[k])w.set(h.pet_id,w.get(h.pet_id)*3);}});
   if(hit)queue.sort((p,q)=>sc(q)-sc(p));
   btn.textContent=`🏇 3連単 ${cut}/${combos.length} 取得中`
     +(BASE>0?`（残 ${Math.max(Math.round(BASE-seenAmt),0).toLocaleString()}rrc）`:'');
 }
 // 残った分は上の判定で「1口も入っていない」ことが確定しているので未成立として出す。
 // 出力の書式は今までと同じ（「未取得」という状態は作らない）。
 const rest=queue.map(([a,b,x])=>
   ({first:a.displayName,second:b.displayName,third:x.displayName,odds:null}));
 const withOdds=results.filter(r=>r.odds!==null).sort((a,b)=>a.odds-b.odds);
 const noOdds=[...results.filter(r=>r.odds===null),...rest];
 btn.textContent='🏇 プール取得中...';
 let poolAmt=0;
 try{poolAmt=(await(await fetch(`${B}/api/trifecta/pool?guild=${G}&schedule_id=${S}`)).json()).pool||0;}catch{}
 const clip=[`guild=${G}`,`schedule_id=${S}`,`pool=${poolAmt}`,'',
   '=== 出走馬一覧 ===',horseHeader,...horseRows,'',
   '=== パッシブ効果 ===','パッシブ,コード,説明',...effRows,'',
   '=== 3連単オッズ ===','順位,1着,2着,3着,オッズ',
   ...withOdds.map((r,i)=>`${i+1},${csv(r.first)},${csv(r.second)},${csv(r.third)},${r.odds}`),
   ...noOdds.map(r=>`未成立,${csv(r.first)},${csv(r.second)},${csv(r.third)},未成立`)].join('\n');
 // iOS Safari は await をまたぐとタップの権限が切れて writeText が拒否される。
 // その場合は消えないボタンにして、**新しいタップの中で**コピーし直す。
 let copied=true;
 try{await navigator.clipboard.writeText(clip);}catch(e){copied=false;}
 const warn=unknown.size?` ⚠未知コード:${[...unknown].join('/')}`:'';
 if(unknown.size)console.warn('Oasis: 辞書に無いパッシブコード:',[...unknown]);
 const n2=pets.filter(h=>h.p1&&h.p2).length;
 const stat=`${n}頭(2枠${n2}) | スキル${effRows.length}種 | 3連単${withOdds.length}件(${cut}/${total}点取得) | プール${poolAmt.toLocaleString()}rrc`;
 btn.style.background=unknown.size?'#e65100':'#1b5e20';btn.style.color='#fff';
 btn.style.borderColor=unknown.size?'#ff9800':'#4caf50';
 const done=()=>{btn.textContent=`✅ ${stat} | コピー完了${warn}`;
   setTimeout(()=>btn.remove(),unknown.size?12000:7000);};
 if(copied){done();}
 else{
   Object.assign(btn.style,{cursor:'pointer',maxWidth:'92vw',whiteSpace:'normal',lineHeight:'1.5'});
   btn.textContent=`📋 ${stat} | ここをタップしてコピー${warn}`;
   btn.onclick=async()=>{
     try{await navigator.clipboard.writeText(clip);done();return;}catch(e){}
     try{await navigator.share({text:clip});done();return;}catch(e){}
     // 最後の手。iOS は readOnly だと選択できないので普通の textarea にして
     // setSelectionRange で全選択しておく（長押し→コピーがすぐできる）。
     const ta=document.createElement('textarea');
     Object.assign(ta.style,{position:'fixed',left:'2%',top:'8%',width:'96%',height:'70%',zIndex:'99998',fontSize:'11px',fontFamily:'monospace'});
     ta.value=clip;document.body.appendChild(ta);ta.focus();ta.setSelectionRange(0,clip.length);
     let ok=false;try{ok=document.execCommand('copy');}catch(e){}
     if(ok){ta.remove();done();}
     else{ta.title='全選択済みです。長押し→コピー（PCは Ctrl+C）';}
   };
 }
 if(!dictOK)btn.textContent+=' ※辞書未検出';
}catch(e){btn.style.background='#b71c1c';btn.style.borderColor='#ef5350';btn.textContent='❌ エラー: '+e.message;setTimeout(()=>btn.remove(),6000);}
})();
