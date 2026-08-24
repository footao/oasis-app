(async () => {
// このファイルの版。ローダー経由で本当に最新が読めているかを目視で確かめるため、
// 完了バッジの末尾に出す。古い版が読まれていたらここの数字が古いまま出る。
const BM_VER='3.15.0';
const B='https://api.oasis.red';
const q=new URLSearchParams(location.search);
const G=q.get('guild'), S=q.get('race')||q.get('schedule_id'), U=q.get('user');
// 残高の取得にだけ使う。**トークンは出力に一切載せない**（貼り付け先に漏れるため）。
const T=q.get('token');
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
 let raw=race.pets||[];
 const dist=race.distance||'', surf=race.surface||'';
 const ground=race.ground||race.track_condition||race.ground_condition||'';
 // 金が乗っていそうな順に取りたい。単勝オッズは下限1.5に張り付いていて
 // （直近20レースの45%が全馬同値）人気の代理にならないので使わない。
 //   初期順  : 簡易スコア（距離重み×パッシブ×スタミナ収支）の高い組から
 //   以降    : 金が乗っていた組に出ていた馬を重くして、取りながら並べ替える
 // 順番が外れても打ち切り条件（残額<1口）は変わらないので、遅くなるだけで結果は同じ。
 // 初期順は「強い組から」。SPEEDだけだと距離バランスを無視するので、実測の強さとの
 // 順位相関は 0.271 しかない（races.jsonl 302レース）。距離重み＋パッシブ倍率＋
 // スタミナ切れ補正まで入れた簡易スコアだと 0.820、実上位3頭を上位3に入れられる数も
 // 1.20/3 → 2.19/3 に上がる。数字の出所は docs/race_formula.pdf。
 // 順番が外れても打ち切り条件（残額<1口）は変わらないので、外しても遅くなるだけ。
 const WD={'短距離':[1.96,.68,.375],'マイル':[1.40,.85,.75],'中距離':[1.26,1.105,.975],'長距離':[.84,.85,1.05]}[dist]||[1.4,.85,.75];
 const BL={'短距離':[1.4,.8,.5],'マイル':[1,1,1],'中距離':[.9,1.3,1.3],'長距離':[.6,1,1.4]}[dist]||[1,1,1];
 const SL={'短距離':[.0132,2.125,2.879,10],'マイル':[.0197,2.234,3.067,15],'中距離':[.03065,2.541,3.737,20],'長距離':[.04109,2.57,3.68,25]}[dist];
 const PMUL={speed_star:[1.35,1,.9],muscle_head:[.9,1.35,1],steady_runner:[1,.9,1.35],jack_of_all:[1.05,1.05,1.05],speed_l:[1.25,1,1],power_l:[1,1.25,1],stamina_l:[1,1,1.25],speed_s:[1.15,1,1],power_s:[1,1.15,1],stamina_s:[1,1,1.15]};
 const APT={turf_specialist:[surf,'芝',1.10],dirt_specialist:[surf,'ダート',1.10],short_special:[dist,'短距離',1.15],mile_special:[dist,'マイル',1.15],middle_special:[dist,'中距離',1.15],long_special:[dist,'長距離',1.15]};
 const spc={};raw.forEach(h=>{spc[h.adult_key]=(spc[h.adult_key]||0)+1;});
 const strength=h=>{
   let s=Number(h.speed)||1,p=Number(h.power)||1,t=Number(h.stamina)||1;
   for(const c of [h.passive_skill,h.passive_skill_2]){
     const m=PMUL[c]; if(m){s*=m[0];p*=m[1];t*=m[2];continue;}
     const a=APT[c]; if(a){if(a[0]===a[1]){s*=a[2];p*=a[2];t*=a[2];}continue;}
     if(c==='same_kind_boost'&&spc[h.adult_key]>1){s*=1.2;p*=1.2;t*=1.2;}
   }
   let r=s*WD[0]+p*WD[1]+t*WD[2];
   if(SL){ // スタミナ切れは最大 -35%、余りは +3% で頭打ち（非対称）
     const d=Math.floor(t)-Math.min(Math.max(SL[0]*(s*.6*BL[0]+p*.3*BL[1]+t*.1*BL[2]),SL[1]),SL[2])*SL[3];
     r*= d<0 ? Math.max(.65,1+.02*d) : Math.min(1.03,1+.0012*d);}
   return Math.max(r,1);
 };
 // ---- 単勝プールの実測（試し買い）----
 // 単勝は控除0%の純パリミュチュエルなので Σ(1/od)=1.000。つまり**オッズはシェアしか
 // 表さず、プール総額の情報を含まない**。自分で少額入れて前後の動きから逆算するしかない。
 //   od_j = P / P_j。自分が Δ 入れると P→P+Δ。**自分が買っていない**馬 j は P_j 不変なので
 //   od_j後 / od_j前 = (P+Δ)/P = R（全馬共通）→ P = Δ/(R−1)
 // オッズは小数2桁なので丸め誤差は od に反比例する。比 R は**重み od² の加重平均**で取る
 // （分散最小）。MAX_PROBE 口まで**1口ずつ**買って、目標精度に届いた時点で止める。
 // 1発で3口買うより要求精度に対して無駄がない。全馬のオッズが同じ（NPCが均等に賭けた等）
 // だと丸め誤差が平均化されず精度が出ないので、その場合だけ口数が伸びる。
 // 0 にすれば試し買いしない。1口=1,000rrc。
 const MAX_PROBE=5, WIN_UNIT=1000, ODD_STEP=0.01, TARGET_ERR=0.04;
 let wp=null;
 if(MAX_PROBE>0&&T){
  const num2=v=>Number(v);
  const usable=h=>{const o=num2(h.odds);return isFinite(o)&&o>0&&o!==1.5;};
  const basis=raw.filter(usable);
  if(basis.length<3){
   btn.textContent='🏇 単勝プールは測れません（オッズの出ている馬が少ない）';
  }else{
   // 試し買い先は簡易スコアが最も高い馬。どのみち買いたい馬なので試し買いが無駄にならない。
   let tgt=null;for(const h of basis){if(!tgt||strength(h)>strength(tgt))tgt=h;}
   const before=new Map(raw.map(h=>[h.pet_id,num2(h.odds)]));
   let spent=0, cur=raw;
   for(let k=0;k<MAX_PROBE;k++){
    btn.textContent=`🏇 単勝プール実測中… ${tgt.name} に ${k+1}/${MAX_PROBE}口`;
    try{
     const r=await fetch(`${B}/api/bet`,{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({user:U,guild:G,race:parseInt(S),pet_id:tgt.pet_id,amount:WIN_UNIT,token:T})});
     if(!r.ok){if(spent===0)btn.textContent='🏇 単勝の試し買いに失敗（プール実測なし）';break;}
     spent+=WIN_UNIT;
    }catch(e){break;}
    try{cur=(await(await fetch(`${B}/api/race/by-id/${G}/${S}?user=${U}`)).json()).pets||cur;}catch(e){break;}
    // 買った馬は P_j が動くので比に使えない。残りの馬で od² 加重平均を取る。
    let sw=0,sr=0,n=0; const seenOd=new Map();
    for(const h of cur){
     if(h.pet_id===tgt.pet_id)continue;
     const ob=before.get(h.pet_id), oa=num2(h.odds);
     if(!ob||!isFinite(oa)||oa<=0||ob===1.5||oa===1.5)continue;
     const w=oa*oa; sw+=w; sr+=w*(oa/ob); n++;
     // 誤差の見積もりだけは**オッズが同じ馬をまとめて1つ**として数える。
     // 丸め誤差は「オッズの値」に対して決まるので、同値の馬を独立サンプル扱いすると
     // 1/√n ぶん精度を過大評価する（NPCが均等に賭けて全馬同オッズのとき実際に外した）。
     if(!seenOd.has(oa))seenOd.set(oa,w);
    }
    if(sw<=0||n<2)continue;
    const R=sr/sw; if(R<=1)continue;
    let P=spent/(R-1);
    let swErr=0; for(const w of seenOd.values())swErr+=w;
    const sdR=(ODD_STEP/Math.sqrt(12))*Math.sqrt(2/swErr);
    let rel=sdR*P/spent; const sdAbs=rel*P;
    let exact=false;
    if(sdAbs<WIN_UNIT){                      // 1σ が1格子未満 → 1,000rrc単位に確定できる
     P=Math.round(P/WIN_UNIT)*WIN_UNIT;
     if(sdAbs<WIN_UNIT/4)exact=true;
     // 格子に載せた以上、誤差は**半格子より小さくは名乗れない**（量子化の下限）
     rel=Math.max(rel,(WIN_UNIT/2)/P);
    }
    wp={pool:P+spent,before:P,delta:spent,n:n,err:rel,exact:exact,
        own:cur.reduce((s2,h)=>s2+(num2(h.my_amount)||0),0)};
    if(exact||rel<=TARGET_ERR)break;          // 目標精度に到達したら打ち切り
   }
   // 出力するオッズは試し買い**後**に揃える（win_pool と同じ時点にする）
   if(spent>0&&cur.length)raw=cur;
  }
 }
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
 // 2026/08/17 に API へ増えた項目。購入ページ自身が
 //   「表示値＝個体値＋特訓＋装備品。倍率・条件スキルはレース中に適用」
 // と書いているとおり、SPEED/POWER/STAMINA は装備の加算まで入った値なので
 // **数値ぶんはモデルがそのまま追従する**。
 // 問題は effect_label / effect_description のほうで、これはパッシブと同じ
 // 「レース中に効く別効果」。モデルは見ていないので、付いている馬がいたら警告する。
 // equipment / charm は文字列ではなく
 //   {name, rarity, rarity_label, icon_url, stat_speed, stat_power, stat_stamina,
 //    effect_label, effect_description}
 // というオブジェクト。そのまま出すと [object Object] になるので名前を取り出す。
 const iname=x=>(x&&x.name)?x.name:'';
 const ieff=x=>(x&&(x.effect_label||x.effect_description))
   ?`${x.effect_label||''}：${x.effect_description||''}`:'';
 // effect_key は効果の正体（gear_/charm_ を外すとパッシブのコード）。
 // 説明文だけだと「中盤 → 1/3」と読める区間限定効果の**実測 duty** を引くのに要る。
 const ikey=x=>(x&&x.effect_key)?x.effect_key:'';
 // 2026/08/23 アプデ: 最大値ロールだと数値の後ろに ☆ が付く。文字列で来ても壊れないよう、
 // 数字だけ取り出す（"12☆" → 12 / "-2" → -2）。
 const num=v=>{if(v==null)return 0;const m=String(v).match(/-?\d+(?:\.\d+)?/);return m?Number(m[0]):0;};
 const star=v=>/[☆★]/.test(String(v==null?'':v))?'☆':'';
 const ibs=h=>{const b=h.item_bonus||{};
   return [['SP',b.speed],['PW',b.power],['ST',b.stamina]]
     .filter(([,v])=>num(v)).map(([k,v])=>k+(num(v)>0?'+':'')+num(v)+star(v)).join('/');};
 const geared=pets.filter(h=>h.equipment||h.charm||ibs(h));
 // 装備品の効果説明（パッシブ効果と同じ形。貼れば倍率を学習できる）
 const itemRows=[],itemSeen=new Set();
 pets.forEach(h=>[[h.equipment,'装備'],[h.charm,'お守り']].forEach(([x,slot])=>{
   if(!x||!x.name||itemSeen.has(slot+x.name))return;
   itemSeen.add(slot+x.name);
   itemRows.push([slot,x.name,x.template_key||'',x.rarity_label||x.rarity||'',
     [['SP',x.stat_speed],['PW',x.stat_power],['ST',x.stat_stamina]]
       .filter(([,v])=>num(v)).map(([k,v])=>k+(num(v)>0?'+':'')+num(v)+star(v)).join('/'),
     x.effect_label||'',x.effect_description||'',
     x.effect_key||'',(x.effect_value==null?'':x.effect_value)].map(csv).join(','));
 }));
 // 想定しているアイテムのフィールド。ここに無いキーが増えたら（例: 強化/呪いの mods、
 // 2つ目の効果）**黙って落とすと予想がずれる**ので、名前を出して警告する。
 // ステータス加算（呪いの SP-2 など負値も含む）は表示値に入っているので追加対応は不要。
 const IKNOWN=new Set(['id','guild_id','owner_user_id','equipped_pet_id','created_at','item_type',
   'template_key','name','rarity','rarity_label','icon_url','passive_skill_key',
   'stat_speed','stat_power','stat_stamina',
   'effect_key','effect_value','effect_label','effect_description']);
 const newFields=new Set();
 pets.forEach(h=>[h.equipment,h.charm].forEach(x=>{
   if(x)for(const k of Object.keys(x))if(!IKNOWN.has(k))newFields.add(k);}));
 // 表示ステータス ≠ ベース+補正 なら、補正の入り方が想定と違う（要調査）
 const mismatch=pets.filter(h=>h.base_speed!=null&&
   (num(h.speed)!==num(h.base_speed)+num((h.item_bonus||{}).speed)
    ||num(h.power)!==num(h.base_power)+num((h.item_bonus||{}).power)
    ||num(h.stamina)!==num(h.base_stamina)+num((h.item_bonus||{}).stamina)));
 const horseHeader='レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額,装備,装備効果,装備効果キー,お守り,お守り効果,お守り効果キー,アイテム補正,素SPEED,素POWER,素STAMINA';
 const horseRows=pets.map(h=>[dist,surf,ground,h.displayName,h.adult_key||'',h.speed,h.power,h.stamina,h.condition_label,(h.p1?h.p1.label:''),(h.p2?h.p2.label:''),h.odds,(h.my_amount||0),iname(h.equipment),ieff(h.equipment),ikey(h.equipment),iname(h.charm),ieff(h.charm),ikey(h.charm),ibs(h),(h.base_speed==null?'':h.base_speed),(h.base_power==null?'':h.base_power),(h.base_stamina==null?'':h.base_stamina)].map(csv).join(','));
 const ownWin=pets.reduce((a,h)=>a+(Number(h.my_amount)||0),0);
 const n=pets.length, total=n*(n-1)*(n-2);
 // ---- 打ち切り条件のためにプールを先に見る ----
 // 取得済みの Σ(賭け金) が「もう見つけた金額」で、実際に賭けられた総額 BASE から
 // 引いた残りが1口(10,000rrc)未満になったら、**未取得の組には1口も入っていないと
 // 確定する**ので打ち切ってよい。判定に追加リクエストは不要。
 btn.textContent='🏇 プール確認中...';
 let pool0=0;
 try{pool0=(await(await fetch(`${B}/api/trifecta/pool?guild=${G}&schedule_id=${S}`)).json()).pool||0;}catch{}
 // 2026/08/23 アプデ: 初期プール金 20万 → 30万。あわせて「表示オッズが
 // (プール − 初期金) 基準」というバグが直った前提になった。
 //   旧: od = (P−S)/bet  → bet = (P−S)/od
 //   新: od = P/bet      → bet = P/od
 // **実際に賭けられている総額はどちらでも P−S** なので、打ち切りの目標額は BASE のまま。
 // 賭け金の見積もりだけ P 基準に変える。もし前提が外れていると seen が過大になり
 // **早く止まりすぎて「金が乗っている組」を未成立と誤判定する**ので、
 // 最後に Σ(P/od) > BASE になっていないか検算して、超えていたら赤で警告する。
 const SEED=300000, UNIT=10000, BASE=Math.max(pool0-SEED,0);
 // 賭け金は必ず1口(10,000rrc)の倍数。表示オッズは小数2桁に丸められているので
 // P/od をそのまま足すと端数が出て「残 238,806rrc」のような有り得ない表示になる。
 // 口数に丸め直すと**端数が消えるうえに打ち切りも正確になる**（誤差の積み上げが要らない）。
 // 丸めが効くかの検算: od の丸め幅 ±0.005 → 賭け金の幅は P×0.01/od²。
 //   プール90万・od1.5（＝60口）でも ±2,000rrc で、半口(5,000)よりずっと小さい。
 //   プールが数百万まで育つと大本命だけ曖昧になりうるので、その組だけ slack を積む。
 const betOf=od=>Math.round(pool0/od/UNIT)*UNIT;
 // 重みの初期値を 1 ではなく簡易スコアにする。1 で始めると、最初のヒットで並べ替えた
 // 瞬間に「ヒット馬を含まない組」が全部 weight=1 で同点になり、**簡易スコアの順序が
 // 捨てられる**（今までは Array#sort が安定なおかげで辛うじて残っていただけ）。
 // スコアを種にしておけば sc = スコア積 × 3^(ヒット回数) となって両方が効き続ける。
 // 初期ソートも sc でそのまま書けるので、別立ての SPEED 積ソートは要らない。
 const STR=new Map(pets.map(h=>[h.pet_id,strength(h)]));
 const w=new Map(pets.map(h=>[h.pet_id,STR.get(h.pet_id)]));
 const sc=c=>w.get(c[0].pet_id)*w.get(c[1].pet_id)*w.get(c[2].pet_id);
 btn.textContent=`🏇 3連単 ${total}通り 取得中...`;
 const combos=[];
 for(const a of pets)for(const b of pets){if(b.pet_id===a.pet_id)continue;
   for(const x of pets){if(x.pet_id===a.pet_id||x.pet_id===b.pet_id)continue;combos.push([a,b,x]);}}
 combos.sort((p,q)=>sc(q)-sc(p));
 // 並列数。上げると往復は半分ずつ減るが、打ち切りの粒度が粗くなって総数は増える。
 // bm_stop_test.js の実測（SPEED順が当たる場合の平均リクエスト数 / 往復回数）:
 //   PAR=5:275/55  10:278/28  20:286/14  40:307/8  80:350/4  160:477/3
 // 2026/08/21 実測（api.oasis.red は HTTP/2。60本同時に投げて全部同時に飛ぶことを確認、
 // 1オリジン6本の制限は無い）。ただし**サーバ側のスループットが上限**で、
 //   PAR=6:36req/s  20:66  60:78  120:80  200:92
 // 上の請求数と合わせた推定所要時間は PAR=5:9.2秒 / 20:4.3秒 / 40:4.3秒 / 80:4.4秒。
 // **20 がすでに最適**なので上げても下げても速くならない。触らないこと。
 const PAR=20;
 const queue=combos.slice(), results=[], failed=[]; let seenAmt=0, cut=0, regimeBad=false;
 // プール表示が 0 ＝ 誰も1口も買っていない ＝ **全組が未成立**。
 // 取りに行っても全部 null が返るだけなので、1リクエストも投げない
 // （14頭なら 2,184回、16頭なら 3,360回まるごと不要）。
 // queue を空にせず残すことで、下の rest がそのまま全組を未成立として出す。
 const noBets = !(pool0 > 0);
 if(noBets) btn.textContent=`🏇 プール0 → 全${combos.length}通り未成立。オッズ取得を省略`;
 while(!noBets && queue.length){
   const batch=queue.splice(0,PAR);
   const got=await Promise.all(batch.map(async([a,b,x])=>{
     const u=`${B}/api/trifecta/odds?guild=${G}&schedule_id=${S}&first=${a.pet_id}&second=${b.pet_id}&third=${x.pet_id}`;
     for(let t=0;t<2;t++){try{
       const d=await(await fetch(u)).json();
       return{first:a.displayName,second:b.displayName,third:x.displayName,odds:typeof d.odds==='number'?d.odds:null};
     }catch(e){await new Promise(r=>setTimeout(r,300));}}
     // 2回とも失敗。黙って落とすとCSVから消えて「未成立」と区別がつかなくなり、
     // ツールが全プール総取り(23倍)の買い目を出しかねないので数えて警告する。
     failed.push(`${a.displayName}→${b.displayName}→${x.displayName}`);
     return null;
   }));
   results.push(...got.filter(Boolean));
   cut+=batch.length;
   // 取りこぼした組は seenAmt に入らない＝残額を多めに見積もる方向なので、
   // 打ち切りが早まることはない（安全側）。
   // 口数に丸め切れない組（オッズの丸め幅が半口を超える大本命）だけ、
   // 1口ぶん残額を多めに見て取り逃しが起きない側に倒す。通常は 0 のまま。
   let slack=0;
   seenAmt=results.reduce((s,r)=>{if(!r.odds)return s;
     const u=pool0/r.odds/UNIT;
     if(Math.abs(u-Math.round(u))>0.25)slack+=UNIT;
     return s+Math.round(u)*UNIT;},0);
   // 見つけた金額が「実際に賭けられた総額」を超えたら、賭け金の見積もり式が違う
   // ＝バグが直っていない（od は今も (P−S) 基準）。このまま続けると早く止まりすぎて
   // 金の乗った組を未成立と誤判定するので、印を立てて**打ち切りをやめる**。
   if(BASE>0&&seenAmt>BASE*1.02)regimeBad=true;
   if(!regimeBad&&BASE>0&&BASE-seenAmt+slack<UNIT)break;
   // 当たった組の馬を重くして残りを並べ替える
   let hit=false;
   got.forEach((r,k)=>{if(r&&r.odds!==null){hit=true;
     for(const h of batch[k])w.set(h.pet_id,w.get(h.pet_id)*3);}});
   if(hit)queue.sort((p,q)=>sc(q)-sc(p));
   // 残額は必ず1口の倍数になるので口数も出す（端数が出たら丸めがおかしい合図）
   const left=Math.max(BASE-seenAmt,0);
   btn.textContent=`🏇 3連単 ${cut}/${combos.length} 取得中`
     +(BASE>0?`（残 ${left.toLocaleString()}rrc = ${left/UNIT}口）`:'');
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
 // 所持金。ケリー計算の資金として使うので取れるときだけ出す。
 let bal=null;
 if(T){try{const d=await(await fetch(
   `${B}/api/balance?user=${U}&guild=${G}&race=${S}&token=${encodeURIComponent(T)}`)).json();
   if(typeof d.balance==='number')bal=d.balance;}catch{}}
 const clip=[`guild=${G}`,`schedule_id=${S}`,`pool=${poolAmt}`,
   ...(bal==null?[]:[`balance=${bal}`]),
   ...(wp?[`win_pool=${Math.round(wp.pool)}`,`win_pool_before=${Math.round(wp.before)}`,
           `win_pool_delta=${wp.delta}`,`win_pool_n=${wp.n}`,
           `win_pool_err=${wp.err.toFixed(4)}`,`win_pool_exact=${wp.exact?1:0}`]:[]),
   // 単勝は【1レース合計100口】が上限。試し買いの有無に関わらず、いま自分が
   // 何口入れているかを必ず出す（予想側で残り枠を計算するのに要る）。
   `win_own=${Math.round(ownWin)}`,
   ...(failed.length?[`取得失敗=${failed.length}`]:[]),'',
   '=== 出走馬一覧 ===',horseHeader,...horseRows,'',
   '=== パッシブ効果 ===','パッシブ,コード,説明',...effRows,'',
   ...(itemRows.length?['=== 装備効果 ===','枠,名前,テンプレ,レアリティ,ステータス補正,効果名,説明,効果キー,効果値',
     ...itemRows,'']:[]),
   '=== 3連単オッズ ===','順位,1着,2着,3着,オッズ',
   ...withOdds.map((r,i)=>`${i+1},${csv(r.first)},${csv(r.second)},${csv(r.third)},${r.odds}`),
   ...noOdds.map(r=>`未成立,${csv(r.first)},${csv(r.second)},${csv(r.third)},未成立`)].join('\n');
 // iOS Safari は await をまたぐとタップの権限が切れて writeText が拒否される。
 // その場合は消えないボタンにして、**新しいタップの中で**コピーし直す。
 let copied=true;
 try{await navigator.clipboard.writeText(clip);}catch(e){copied=false;}
 let warn=unknown.size?` ⚠未知コード:${[...unknown].join('/')}`:'';
 if(failed.length){warn+=` ⚠取得失敗${failed.length}件 → 貼らずに再実行してください`;
   console.warn('Oasis: オッズ取得に失敗した組',failed);}
 if(geared.length){warn+=` ⚠装備/お守り${geared.length}頭`;
   console.warn('Oasis: 装備・お守りが付いている馬',geared.map(h=>
     ({name:h.displayName,equipment:h.equipment,charm:h.charm,item_bonus:h.item_bonus})));}
 if(regimeBad){warn+=' ⚠オッズの基準が想定と違います（初期プール金の扱い）→ 全組取得しました。'
   +'oasis_core.py の TRIFECTA_SEED_BUG_ACTIVE を True に戻してください';
   console.warn('Oasis: Σ(P/od) が「実際に賭けられた総額」を超えました。'
     +'od は (プール−初期金) 基準のままです。',{pool:pool0,SEED:SEED,BASE:BASE,seen:seenAmt});}
 if(newFields.size){warn+=` ⚠アイテムに未知の項目:${[...newFields].join('/')}`;
   console.warn('Oasis: アイテムに知らないフィールドが増えています',[...newFields],
     pets.map(h=>h.equipment||h.charm).filter(Boolean)[0]);}
 if(mismatch.length){warn+=` ⚠ステータスがベース+補正と不一致${mismatch.length}頭`;
   console.warn('Oasis: SPEED≠base+item_bonus の馬',mismatch.map(h=>
     ({name:h.displayName,speed:h.speed,base:h.base_speed,bonus:h.item_bonus})));}
 if(unknown.size)console.warn('Oasis: 辞書に無いパッシブコード:',[...unknown]);
 const n2=pets.filter(h=>h.p1&&h.p2).length;
 const stat=`v${BM_VER} | ${n}頭(2枠${n2}) | スキル${effRows.length}種 | 3連単${withOdds.length}件`
   +(noBets?`(プール0のため取得省略)`:`(${cut}/${total}点取得)`)
   +` | プール${poolAmt.toLocaleString()}rrc`
   +(wp?` | 単勝プール${Math.round(wp.pool).toLocaleString()}rrc(試買${wp.delta.toLocaleString()})`:'')
   +(ownWin?` | 単勝購入済${Math.round(ownWin/1000)}口/100`:'')
   +(bal==null?'':` | 残高${bal.toLocaleString()}rrc`);
 const bad=unknown.size||failed.length||geared.length||mismatch.length||newFields.size||regimeBad;
 btn.style.background=(failed.length||regimeBad)?'#b71c1c':(unknown.size?'#e65100':'#1b5e20');btn.style.color='#fff';
 btn.style.borderColor=failed.length?'#ef5350':(unknown.size?'#ff9800':'#4caf50');
 const done=()=>{btn.textContent=`✅ ${stat} | コピー完了${warn}`;
   setTimeout(()=>btn.remove(),bad?12000:7000);};
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
