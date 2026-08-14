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
 btn.textContent=`🏇 3連単 ${total}通り 取得中...`;
 const combos=[];
 for(const a of pets)for(const b of pets){if(b.pet_id===a.pet_id)continue;
   for(const x of pets){if(x.pet_id===a.pet_id||x.pet_id===b.pet_id)continue;combos.push([a,b,x]);}}
 const results=[];
 for(let i=0;i<combos.length;i+=20){
   const batch=combos.slice(i,i+20);
   const got=await Promise.all(batch.map(async([a,b,x])=>{try{
     const d=await(await fetch(`${B}/api/trifecta/odds?guild=${G}&schedule_id=${S}&first=${a.pet_id}&second=${b.pet_id}&third=${x.pet_id}`)).json();
     return{first:a.displayName,second:b.displayName,third:x.displayName,odds:typeof d.odds==='number'?d.odds:null};
   }catch{return null;}}));
   results.push(...got.filter(Boolean));
   btn.textContent=`🏇 3連単 ${Math.min(i+20,combos.length)}/${combos.length} 取得中...`;
 }
 const withOdds=results.filter(r=>r.odds!==null).sort((a,b)=>a.odds-b.odds);
 const noOdds=results.filter(r=>r.odds===null);
 btn.textContent='🏇 プール取得中...';
 let poolAmt=0;
 try{poolAmt=(await(await fetch(`${B}/api/trifecta/pool?guild=${G}&schedule_id=${S}`)).json()).pool||0;}catch{}
 const clip=[`guild=${G}`,`schedule_id=${S}`,`pool=${poolAmt}`,'',
   '=== 出走馬一覧 ===',horseHeader,...horseRows,'',
   '=== パッシブ効果 ===','パッシブ,コード,説明',...effRows,'',
   '=== 3連単オッズ ===','順位,1着,2着,3着,オッズ',
   ...withOdds.map((r,i)=>`${i+1},${csv(r.first)},${csv(r.second)},${csv(r.third)},${r.odds}`),
   ...noOdds.map(r=>`未成立,${csv(r.first)},${csv(r.second)},${csv(r.third)},未成立`)].join('\n');
 let copied=true;
 try{await navigator.clipboard.writeText(clip);}catch(e){copied=false;}
 if(!copied){
   const ta=document.createElement('textarea');
   Object.assign(ta.style,{position:'fixed',left:'2%',top:'8%',width:'96%',height:'70%',zIndex:'99998',fontSize:'11px',fontFamily:'monospace'});
   ta.value=clip;document.body.appendChild(ta);ta.focus();ta.select();
   try{copied=document.execCommand('copy');}catch(e){}
   if(copied){ta.remove();}else{ta.title='Ctrl+A → Ctrl+C でコピーしてください';}
 }
 const warn=unknown.size?` ⚠未知コード:${[...unknown].join('/')}`:'';
 if(unknown.size)console.warn('Oasis: 辞書に無いパッシブコード:',[...unknown]);
 const n2=pets.filter(h=>h.p1&&h.p2).length;
 btn.style.background=unknown.size?'#e65100':'#1b5e20';btn.style.color='#fff';
 btn.style.borderColor=unknown.size?'#ff9800':'#4caf50';
 btn.textContent=`${copied?'✅':'📋'} ${n}頭(2枠${n2}) | スキル${effRows.length}種 | 3連単${withOdds.length}件 | プール${poolAmt.toLocaleString()}rrc | ${copied?'コピー完了':'下の枠を手動でコピー'}${warn}`;
 if(!dictOK)btn.textContent+=' ※辞書未検出';
 setTimeout(()=>btn.remove(),unknown.size?12000:7000);
}catch(e){btn.style.background='#b71c1c';btn.style.borderColor='#ef5350';btn.textContent='❌ エラー: '+e.message;setTimeout(()=>btn.remove(),6000);}
})();
