(() => {
  const ids=['cpu','memory','uptime','players'];
  const colors={cpu:'#ef49d5',memory:'#17d8f0',uptime:'#18cba1',players:'#9d8cff'};
  const titles={cpu:'CPU',memory:'Memory',uptime:'Server status',players:'Players online'};
  const range=document.getElementById('monitor-range');
  if(!range) return;
  const fmtTime=ts=>new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const valueFor=(id,s)=>id==='cpu'?s.cpu:id==='memory'?(s.rss==null?null:s.rss/1073741824):id==='uptime'?s.online:s.players;
  const formatValue=(id,v)=>id==='cpu'?v.toFixed(1)+'%':id==='memory'?v.toFixed(2)+' GB':id==='uptime'?(v?'Online':'Offline'):Math.round(v)+' player(s)';
  const hoverState={}; let latest=null;
  function chart(id,samples,events){
    const c=document.getElementById('chart-'+id),rect=c.getBoundingClientRect();
    if(!rect.width)return;
    const dpr=window.devicePixelRatio||1,w=rect.width,h=180,l=47,r=12,t=16,b=29;
    c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);
    const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
    const vals=samples.map(s=>valueFor(id,s));
    const valid=vals.filter(v=>v!=null&&Number.isFinite(v));
    const max=id==='uptime'?1:Math.max(1,...valid)*(id==='players'?1.15:1.12);
    const plotH=h-t-b,plotW=w-l-r;
    ctx.font='11px system-ui';ctx.textBaseline='middle';ctx.lineWidth=1;
    for(let i=0;i<=4;i++){
      const gy=t+plotH*i/4;ctx.beginPath();ctx.strokeStyle='rgba(180,185,210,.15)';ctx.moveTo(l,gy);ctx.lineTo(w-r,gy);ctx.stroke();
      ctx.fillStyle='#999ab3';ctx.fillText(id==='uptime'?(i===0?'Online':i===4?'Offline':''):id==='players'?String(Math.round(max*(1-i/4))):(max*(1-i/4)).toFixed(1),3,gy);
    }
    if(!samples.length){ctx.fillStyle='#999ab3';ctx.fillText('Waiting for monitoring samples…',l+12,h/2);hoverState[id]=null;return;}
    const start=samples[0].ts,end=samples[samples.length-1].ts,span=Math.max(1,end-start);
    const x=ts=>l+plotW*(ts-start)/span,y=v=>t+plotH*(1-v/max);
    const points=samples.map((s,i)=>vals[i]==null||!Number.isFinite(vals[i])?null:{x:x(s.ts),y:y(vals[i]),ts:s.ts,value:vals[i]});
    // Smooth cubic Bezier interpolation, split at missing values. Monotone tangents prevent exaggerated spikes.
    ctx.save();ctx.beginPath();ctx.rect(l,t,plotW,plotH);ctx.clip();
    ctx.strokeStyle=colors[id];ctx.lineWidth=2.5;ctx.lineCap='round';ctx.lineJoin='round';ctx.beginPath();
    let segments=[],segment=[];
    points.forEach(p=>{if(p)segment.push(p);else if(segment.length){segments.push(segment);segment=[];}});
    if(segment.length)segments.push(segment);
    for(const seg of segments){ctx.moveTo(seg[0].x,seg[0].y);
      for(let i=0;i<seg.length-1;i++){
        const a=seg[i],b=seg[i+1],prev=seg[i-1]||a,next=seg[i+2]||b,dx=b.x-a.x;
        if(id==='uptime'){ctx.lineTo(b.x,a.y);ctx.lineTo(b.x,b.y);continue;}
        if(dx<0.01){ctx.lineTo(b.x,b.y);continue;}
        const d=(b.y-a.y)/dx,dl=(b.y-prev.y)/Math.max(.001,b.x-prev.x),dr=(next.y-a.y)/Math.max(.001,next.x-a.x);
        const tangent=(slope)=>slope*d<=0?0:Math.sign(d)*Math.min(Math.abs(slope),3*Math.abs(d));
        const m1=tangent(dl),m2=tangent(dr);
        ctx.bezierCurveTo(a.x+dx/3,a.y+m1*dx/3,b.x-dx/3,b.y-m2*dx/3,b.x,b.y);
      }
    }
    ctx.stroke();
    const kinds=id==='uptime'?['schedule','server']:id==='players'?['join','leave']:[];
    const markers=events.filter(e=>kinds.includes(e.kind)&&e.ts>=start&&e.ts<=end).map(e=>({...e,x:x(e.ts)}));
    markers.forEach(e=>{const color=e.kind==='schedule'?'#f8bd55':e.kind==='join'?'#1cdaa5':e.kind==='leave'?'#fa8296':'#f271e9';ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(e.x,t);ctx.lineTo(e.x,h-b);ctx.stroke();ctx.beginPath();ctx.arc(e.x,t+5,3.5,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();});
    const active=hoverState[id];if(active){const p=active.type==='event'?markers.find(e=>e.ts===active.ts&&e.kind===active.kind):points.find(p=>p&&p.ts===active.ts);if(p){ctx.beginPath();ctx.arc(p.x,p.y??t+5,5,0,Math.PI*2);ctx.fillStyle='#fff';ctx.fill();ctx.beginPath();ctx.arc(p.x,p.y??t+5,3,0,Math.PI*2);ctx.fillStyle=colors[id];ctx.fill();}}
    ctx.restore();ctx.fillStyle='#999ab3';ctx.textBaseline='alphabetic';ctx.fillText(new Date(start*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}),l,h-7);ctx.textAlign='right';ctx.fillText(new Date(end*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}),w-r,h-7);ctx.textAlign='left';
    c._plot={points:points.filter(Boolean),markers};
  }
  function showHover(id,ev){
    const c=document.getElementById('chart-'+id),plot=c._plot,tooltip=c.parentElement.querySelector('.monitor-tooltip');if(!plot)return;
    const bounds=c.getBoundingClientRect(),mx=ev.clientX-bounds.left,my=ev.clientY-bounds.top;
    const nearestEvent=plot.markers.reduce((a,e)=>{const d=Math.abs(e.x-mx);return !a||d<a.d?{item:e,d}:a;},null);
    const nearestPoint=plot.points.reduce((a,p)=>{const d=Math.abs(p.x-mx);return !a||d<a.d?{item:p,d}:a;},null);
    let text='',item=null;
    if(nearestEvent&&nearestEvent.d<=10&&my<155){const e=nearestEvent.item;item={type:'event',ts:e.ts,kind:e.kind};text=`${fmtTime(e.ts)}\n${e.kind.toUpperCase()}: ${e.label||'Event'}`;}
    else if(nearestPoint&&nearestPoint.d<=Math.max(12,bounds.width/Math.max(2,plot.points.length))){const p=nearestPoint.item;item={type:'point',ts:p.ts};text=`${fmtTime(p.ts)}\n${titles[id]}: ${formatValue(id,p.value)}`;}
    if(!item){tooltip.hidden=true;if(hoverState[id]){hoverState[id]=null;chart(id,latest.samples,latest.events);}return;}
    const previous=hoverState[id];hoverState[id]=item;tooltip.textContent=text;tooltip.hidden=false;
    tooltip.style.left=Math.max(8,Math.min(mx+14,bounds.width-205))+'px';tooltip.style.top=Math.max(0,my-54)+'px';
    if(!previous||previous.ts!==item.ts||previous.type!==item.type)chart(id,latest.samples,latest.events);
  }
  ids.forEach(id=>{const c=document.getElementById('chart-'+id);c.addEventListener('pointermove',e=>showHover(id,e));c.addEventListener('pointerleave',()=>{c.parentElement.querySelector('.monitor-tooltip').hidden=true;hoverState[id]=null;if(latest)chart(id,latest.samples,latest.events);});});
  async function refresh(){try{
    const [res,stat]=await Promise.all([fetch('/api/monitor/history?hours='+range.value),fetch('/api/stats')]);
    if(!res.ok||!stat.ok)throw Error('HTTP '+res.status);
    latest=await res.json();const s=await stat.json();
    document.getElementById('cpu').textContent=s.jvm_found?s.cpu_percent.toFixed(1)+'%':'—';
    document.getElementById('mem').textContent=s.jvm_found?(s.mem_used/1073741824).toFixed(2)+' GB':'—';
    document.getElementById('player-count').textContent=latest.samples.length?latest.samples[latest.samples.length-1].players:'—';
    const pill=document.querySelector('#server-status .status-pill');pill.textContent=s.server_running?'Online':'Offline';pill.classList.toggle('online',s.server_running);pill.classList.toggle('offline',!s.server_running);
    ids.forEach(id=>chart(id,latest.samples,latest.events));
    const box=document.getElementById('monitor-events');box.replaceChildren();
    latest.events.slice(-25).reverse().forEach(e=>{const el=document.createElement('div');el.className='monitor-event';el.textContent=fmtTime(e.ts)+' · '+e.kind.toUpperCase()+' · '+e.label;box.append(el);});
    if(!latest.events.length)box.textContent='No recorded events in this range yet.';
  }catch(e){console.error('Monitoring:',e);}}
  range.addEventListener('change',refresh);
  window.addEventListener('resize',()=>{if(latest)ids.forEach(id=>chart(id,latest.samples,latest.events));});
  document.querySelectorAll('.monitor-graph').forEach(d=>d.addEventListener('toggle',()=>{if(d.open&&latest)requestAnimationFrame(()=>ids.forEach(id=>chart(id,latest.samples,latest.events)));}));
  document.getElementById('btn-start').addEventListener('click',async()=>{await fetch('/api/server/start',{method:'POST'});setTimeout(refresh,1200);});
  document.getElementById('btn-stop').addEventListener('click',async()=>{await fetch('/api/server/stop',{method:'POST'});setTimeout(refresh,1200);});
  refresh();setInterval(refresh,5000);
})();
