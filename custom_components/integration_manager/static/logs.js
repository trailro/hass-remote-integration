let groups=[], gOn=new Set(), lastId=0, shown=0;
function prefixes(){ return groups.filter(g=>gOn.has(g.name)).flatMap(g=>g.loggers); }
function line(r){ return `<span class="l ${esc(r.level)}"><span class="ts">${esc(r.ts.slice(11))}</span> ${esc(r.level.padEnd(7))} <span class="lg">${esc(r.logger)}</span> ${esc(r.message)}${r.exc?`<span class="exc">${esc(r.exc)}</span>`:''}</span>`; }
async function fetchLogs(reset){
 const p=new URLSearchParams({level:$('#level').value,q:$('#q').value,limit:reset?500:200,since_id:reset?0:lastId});
 prefixes().forEach(x=>p.append('prefix',x));
 const r=await (await fetch('/api/logs?'+p)).json();
 $('#cap').textContent=r.capacity; $('#path').textContent=r.path||''; $('#ts').textContent=new Date().toLocaleTimeString();
 const out=$('#out'); if(reset){out.innerHTML='';shown=0;}
 const atBottom=out.scrollTop+out.clientHeight>=out.scrollHeight-20;
 if(r.records.length){ out.insertAdjacentHTML('beforeend',r.records.map(line).join('')); shown+=r.records.length; lastId=Math.max(lastId,...r.records.map(x=>x.id)); }
 if(r.truncated&&!reset) out.insertAdjacentHTML('beforeend','<span class="l WARNING">… more new lines than fit in one read; continuing at the next</span>');
 if(reset||atBottom) out.scrollTop=out.scrollHeight;
 $('#n').textContent=`${shown} shown`;
}
async function loadGroups(){
 groups=await (await fetch('/api/logs/loggers')).json();
 $('#groups').innerHTML=groups.map(g=>`<span class="tag ${gOn.has(g.name)?'on':''}" data-g="${esc(g.name)}" title="${esc(g.loggers.join(', '))}">${esc(g.name)} ${g.count}</span>`).join('');
 document.querySelectorAll('#groups .tag').forEach(t=>t.onclick=()=>{const g=t.dataset.g;gOn.has(g)?gOn.delete(g):gOn.add(g);loadGroups();fetchLogs(true)});
 const t=$('#loggers'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
 for(const g of groups) for(const lg of g.loggers){
  const lv=g.levels[lg]||'—', tr=document.createElement('tr');
  tr.innerHTML=`<td>${esc(g.name)}</td><td class="id">${esc(lg)}</td><td>${esc(lv)}</td><td class="mut">${g.counts[lg]||0}</td>
   <td><select data-lg="${esc(lg)}">${['(inherited)','DEBUG','INFO','WARNING','ERROR'].map(l=>`<option ${l===lv?'selected':''}>${l}</option>`).join('')}</select></td>`;
  tr.querySelector('select').onchange=async e=>{await fetch('/api/logs/level',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({logger:lg,level:e.target.value==='(inherited)'?null:e.target.value})}); loadGroups();};
  t.appendChild(tr);
 }
}
['#level','#q'].forEach(s=>$(s).addEventListener('input',()=>fetchLogs(true)));
$('#clear').onclick=()=>{$('#out').innerHTML='';shown=0;$('#n').textContent='0 shown'};
setInterval(()=>{ if($('#follow').checked) fetchLogs(false).catch(()=>{}); },3000);
setInterval(loadGroups,30000);
loadGroups().then(()=>fetchLogs(true));
