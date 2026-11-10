(function(){
'use strict';
var CFG=window.NAMES_CFG, DATA=JSON.parse(document.getElementById('names-data').textContent);
var KEY='names-draft:'+DATA.signature, PAGE=100;
var LBL={trainer:'المرشحون',reviewer:'المقيّمون'};
var S={tab:'trainer',filter:'pending',q:'',shown:PAGE};
var M={trainer:[],reviewer:[]}, draft={trainer:{},reviewer:{}};

['trainer','reviewer'].forEach(function(t){
  DATA.groups[t].forEach(function(g){g.members.forEach(function(m){
    M[t].push({name:m.name,n:m.n,aliased:m.aliased,base:m.base,gstatus:g.status,reasons:g.reasons});});});
});
try{var saved=JSON.parse(localStorage.getItem(KEY)||'null');if(saved&&saved.trainer&&saved.reviewer)draft=saved;}catch(e){}
function save(){try{if(changes().total)localStorage.setItem(KEY,JSON.stringify(draft));else localStorage.removeItem(KEY);}catch(e){}}

function h(tag,attrs){var el=document.createElement(tag);
  if(attrs)Object.keys(attrs).forEach(function(k){var v=attrs[k];if(v==null||v===false)return;
    if(k==='class')el.className=v;else if(k==='text')el.textContent=v;else if(k.slice(0,2)==='on')el.addEventListener(k.slice(2),v);else el.setAttribute(k,v===true?'':v);});
  for(var i=2;i<arguments.length;i++){var c=arguments[i];if(c==null)continue;el.appendChild(typeof c==='string'?document.createTextNode(c):c);}
  return el;}
function norm(s){return (s||'').replace(/[ً-ٰٟـ]/g,'').replace(/[أإآٱ]/g,'ا').replace(/ى/g,'ي').replace(/ة/g,'ه').replace(/\s+/g,' ').trim().toLowerCase();}
function $(id){return document.getElementById(id)}

/* ---------- حالة العضو والمجموعات المشتقة ---------- */
function cur(t,m){var d=draft[t][m.name];return d?{c:d.c,f:d.f}:{c:m.base,f:false};}
function mstate(t,m){var d=draft[t][m.name];
  if(d&&(d.c!==m.base||(d.f&&!m.aliased)))return 'dirty';
  return m.aliased?'ok':'pending';}
function groupsOf(t){
  var by={},order=[];
  M[t].forEach(function(m){var c=cur(t,m).c;if(!by[c]){by[c]={canonical:c,members:[]};order.push(c);}by[c].members.push(m);});
  var rank={amber:0,green:1,committed:2,single:3};
  return order.map(function(c){var g=by[c];
    var st=g.members.map(function(m){return mstate(t,m)});
    var pend=st.indexOf('pending')>=0, dirty=st.indexOf('dirty')>=0;
    var worst='single';g.members.forEach(function(m){if(rank[m.gstatus]<rank[worst])worst=m.gstatus;});
    g.reasons=[];g.members.forEach(function(m){(m.reasons||[]).forEach(function(r){if(g.reasons.indexOf(r)<0)g.reasons.push(r);});});
    if(pend)g.status=(g.members.length>1?(worst==='single'?'green':worst):'single');
    else g.status=dirty?'ready':(g.members.length>1||g.members[0].base!==g.members[0].name?'committed':'single');
    g.total=g.members.reduce(function(a,m){return a+m.n;},0);
    g.members.sort(function(a,b){return b.n-a.n;});return g;})
   .sort(function(a,b){var o={amber:0,green:1,ready:2,committed:3,single:4};return o[a.status]-o[b.status]||a.canonical.localeCompare(b.canonical,'ar');});
}
function changes(){var items=[],groups=0;
  ['trainer','reviewer'].forEach(function(t){
    var seen={};
    M[t].forEach(function(m){if(mstate(t,m)==='dirty'){var c=cur(t,m).c;items.push({entity_type:t,original:m.name,canonical:c});seen[c]=1;}});
    groups+=Object.keys(seen).length;});
  return {items:items,total:items.length,groups:groups};}

/* ---------- العرض ---------- */
var STATUS={amber:['warn','تحتاج مراجعتك'],green:['info','مطابقة مقترحة'],ready:['ok','جاهزة للاعتماد'],committed:['ok','معتمدة'],single:['gray','اسم فريد']};
function render(){
  var t=S.tab, all=groupsOf(t), q=norm(S.q);
  var cnt={};all.forEach(function(g){var k=(g.status==='amber'||g.status==='green')?'pending':g.status;cnt[k]=(cnt[k]||0)+1;});
  var chips=[['pending','تحتاج قراراً'],['ready','جاهزة'],['clean','الجدول النظيف'],['committed','معتمدة'],['single','فريدة'],['all','الكل']];
  var cw=$('chips');cw.textContent='';
  chips.forEach(function(c){var n=(c[0]==='all'||c[0]==='clean')?all.length:(cnt[c[0]]||0);
    cw.appendChild(h('button',{type:'button',class:'chip','aria-pressed':String(S.filter===c[0]),text:c[1]+' '+n,onclick:function(){S.filter=c[0];S.shown=PAGE;render();}}));});
  if(S.filter==='clean'){cleanTable(all,q);chrome(all);return;}
  var list=all.filter(function(g){
    var k=(g.status==='amber'||g.status==='green')?'pending':g.status;
    if(S.filter!=='all'&&k!==S.filter)return false;
    if(q){return norm(g.canonical).indexOf(q)>=0||g.members.some(function(m){return norm(m.name).indexOf(q)>=0;});}
    return true;});
  var box=$('list');box.textContent='';
  if(!list.length){box.appendChild(h('div',{class:'panel empty',style:'margin-top:14px'},h('b',{text:q?'لا نتائج مطابقة للبحث':(S.filter==='pending'?'لا توجد مجموعات تحتاج قراراً':'لا توجد عناصر هنا')}),
    document.createTextNode(S.filter==='pending'&&!q?'أحسنت. يمكنك المتابعة إلى الخطوة التالية بعد اعتماد أي تغييرات.':'جرّب تغيير التصفية.')));}
  list.slice(0,S.shown).forEach(function(g){box.appendChild(card(t,g));});
  if(list.length>S.shown)box.appendChild(h('div',{style:'text-align:center;margin-top:16px'},h('button',{type:'button',class:'btn ghost',text:'عرض المزيد ('+(list.length-S.shown)+')',onclick:function(){S.shown+=PAGE;render();}})));
  chrome(all);
}
function cleanTable(all,q){
  var box=$('list');box.textContent='';
  var rows=all.filter(function(g){return !q||norm(g.canonical).indexOf(q)>=0||g.members.some(function(m){return norm(m.name).indexOf(q)>=0;});});
  var pend=all.filter(function(g){return g.status==='amber'||g.status==='green';}).length;
  if(pend||changes().total)box.appendChild(h('div',{class:'flash warning',style:'margin-top:14px',text:'هذا الجدول يعكس الحالة الحالية وبه '+(pend||0)+' مجموعة لم تُحسم'+(changes().total?' وتعديلات غير معتمدة':'')+'. الجدول النهائي يثبت بعد الاعتماد.'}));
  var tb=h('tbody');
  rows.forEach(function(g,i){tb.appendChild(h('tr',null,h('td',{class:'num',text:String(i+1)}),h('td',null,h('b',{text:g.canonical})),
    h('td',{text:g.members.length>1?g.members.map(function(m){return m.name}).join('، '):'—'}),h('td',{class:'num',text:String(g.total)})));});
  box.appendChild(h('section',{class:'panel flush',style:'margin-top:14px'},h('div',{class:'tablewrap'},
    h('table',{class:'grid'},h('thead',null,h('tr',null,h('th',{text:'م'}),h('th',{text:LBL[S.tab]+' (الاسم المعتمد)'}),h('th',{text:'الصيغ الأصلية المدمجة'}),h('th',{text:'عدد الاستجابات'}))),tb))));
}
function card(t,g){
  var st=STATUS[g.status], multi=g.members.length>1, c=g.status;
  var el=h('section',{class:'grp '+c});
  var inp=h('input',{type:'text',value:g.canonical,'aria-label':'الاسم المعتمد',maxlength:'200',
    onchange:function(){var v=this.value.replace(/\s+/g,' ').trim();if(!v||v===g.canonical){this.value=g.canonical;return;}
      g.members.forEach(function(m){draft[t][m.name]={c:v,f:true};});save();render();},
    onkeydown:function(e){if(e.key==='Enter'){e.preventDefault();this.blur();}}});
  var head=h('div',{class:'grp-head'},h('div',{class:'name'},inp),
    h('span',{class:'badge '+st[0]},h('i',{class:'dia'}),st[1]),
    h('span',{class:'muted small',text:g.total+' استجابة'}));
  if(c==='amber'||c==='green'){head.appendChild(h('button',{type:'button',class:'btn gold sm',text:'تأكيد المجموعة',
    onclick:function(){g.members.forEach(function(m){draft[t][m.name]={c:g.canonical,f:true};});save();render();}}));}
  if(multi&&c!=='committed'){head.appendChild(h('button',{type:'button',class:'btn danger sm',text:'ليسوا نفس الشخص',
    onclick:function(){g.members.forEach(function(m){draft[t][m.name]={c:m.name,f:true};});save();render();}}));}
  if(c==='ready'||(c==='committed'&&false)){head.appendChild(h('button',{type:'button',class:'btn ghost sm',text:'إلغاء التأكيد',onclick:function(){g.members.forEach(function(m){delete draft[t][m.name];});save();render();}}));}
  el.appendChild(head);
  if(g.reasons.length&&(c==='amber'))el.appendChild(h('div',{class:'grp-reasons',text:'سبب التنبيه: '+g.reasons.join('، ')+'. تأكد أنهم شخص واحد قبل التأكيد.'}));
  if(multi||c==='committed'){g.members.forEach(function(m){
    var row=h('div',{class:'mem'+(m.name===g.canonical?' canon':'')},h('span',{class:'nm',text:m.name}),h('span',{class:'times',text:'×'+m.n}));
    if(multi){
      var same=m.name===g.canonical;
      row.appendChild(h('button',{type:'button',class:'btn ghost sm',text:'فصل',disabled:same,title:same?'هذا هو الاسم المعتمد للمجموعة؛ غيّر اسم المجموعة أولاً لفصله':'اجعله اسماً مستقلاً',
        onclick:function(){draft[t][m.name]={c:m.name,f:true};save();render();}}));
      row.appendChild(h('button',{type:'button',class:'btn ghost sm',text:'نقل…',onclick:function(){openMove(t,m);}}));}
    el.appendChild(row);});}
  return el;
}
function chrome(all){
  var amberPending=0,multiTotal=0,multiDone=0;
  ['trainer','reviewer'].forEach(function(t){groupsOf(t).forEach(function(g){
    if(g.status==='amber')amberPending++;
    if(g.members.length>1||g.status==='committed'||g.status==='ready'){multiTotal++;if(g.status==='committed'||g.status==='ready')multiDone++;}});});
  ['trainer','reviewer'].forEach(function(t){var n=groupsOf(t).filter(function(g){return g.status==='amber'||g.status==='green';}).length;
    var b=document.querySelector('[data-badge='+t+']');b.textContent=n;b.style.display=n?'':'none';});
  document.querySelectorAll('.tabs button').forEach(function(b){b.setAttribute('aria-selected',String(b.dataset.tab===S.tab));});
  $('bar').style.width=(multiTotal?Math.round(multiDone/multiTotal*100):100)+'%';
  var ch=changes(), cb=$('cbar');
  cb.classList.toggle('on',ch.total>0);
  $('c-sum').textContent=ch.total+' اسماً مُعدَّلاً في '+ch.groups+' مجموعة';
  $('c-sub').textContent='مسودة غير محفوظة. اضغط «مراجعة واعتماد» لحفظها دفعة واحدة.';
  var greens=all.filter(function(g){return g.status==='green';}).length;
  $('confirm-green').style.display=greens?'':'none';$('confirm-green').textContent='تأكيد كل المطابقات المطمئنة ('+greens+')';
  var ok=ch.total===0&&amberPending===0;
  $('cont').disabled=!ok;
  $('cont-t').textContent=ok?'الأسماء جاهزة':(ch.total?'اعتمد التغييرات أولاً':'بقيت '+amberPending+' مجموعة تحتاج مراجعتك');
  $('cont-s').textContent=ok?'انتقل إلى تحديد نطاق المعالجة.':(ch.total?'لا تُفتح الخطوة التالية قبل حفظ المسودة.':'أكّد المجموعة أو عدّلها أو افصلها ثم اعتمد التغييرات.');
}

/* ---------- نقل ---------- */
var moving=null;
function openMove(t,m){moving={t:t,m:m};$('m-sub').textContent='«'+m.name+'»';var dl=$('m-names');dl.textContent='';
  groupsOf(t).forEach(function(g){if(g.canonical!==cur(t,m).c)dl.appendChild(h('option',{value:g.canonical}));});
  $('m-in').value='';$('d-move').showModal();}
$('m-cancel').onclick=function(){$('d-move').close();};
$('m-ok').onclick=function(){var v=$('m-in').value.replace(/\s+/g,' ').trim();if(!v)return;
  draft[moving.t][moving.m.name]={c:v,f:true};save();$('d-move').close();render();};

/* ---------- اعتماد دفعة واحدة ---------- */
$('c-review').onclick=function(){
  var ch=changes(), box=$('d-list');box.textContent='';
  ['trainer','reviewer'].forEach(function(t){var by={};
    ch.items.filter(function(i){return i.entity_type===t;}).forEach(function(i){(by[i.canonical]=by[i.canonical]||[]).push(i.original);});
    Object.keys(by).forEach(function(c){box.appendChild(h('div',{class:'chg'},h('b',{text:c}),h('small',{text:LBL[t]+' ← '+by[c].join('، ')})));});});
  $('d-sub').textContent=ch.total+' اسماً في '+ch.groups+' مجموعة. ما لم يظهر هنا يبقى كما هو.';
  $('d-ok').disabled=false;$('d-ok').textContent='اعتماد كل التغييرات';$('d-commit').showModal();};
$('d-cancel').onclick=function(){$('d-commit').close();};
$('d-ok').onclick=function(){var btn=this;btn.disabled=true;btn.textContent='جارٍ الحفظ…';
  post(CFG.commit,{changes:changes().items}).then(function(r){
    if(r.ok){try{localStorage.removeItem(KEY);}catch(e){}draft={trainer:{},reviewer:{}};window.__saved=true;location.reload();}
    else{alert(r.error||'تعذر الحفظ');btn.disabled=false;btn.textContent='اعتماد كل التغييرات';}
  }).catch(function(){alert('تعذر الاتصال بالخادم');btn.disabled=false;btn.textContent='اعتماد كل التغييرات';});};
$('c-discard').onclick=function(){if(confirm('تجاهل كل التعديلات غير المعتمدة؟')){draft={trainer:{},reviewer:{}};save();render();}};
function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':CFG.csrf},body:JSON.stringify(body||{})}).then(function(r){return r.json();});}

var ub=$('undo-btn');
if(ub){ub.onclick=function(){$('d-undo').showModal();};
  $('u-cancel').onclick=function(){$('d-undo').close();};
  $('u-ok').onclick=function(){this.disabled=true;post(CFG.undo).then(function(r){if(r.ok){window.__saved=true;location.reload();}else{alert(r.error||'تعذر التراجع');location.reload();}});};}

/* ---------- أحداث عامة ---------- */
document.querySelectorAll('.tabs button').forEach(function(b){b.onclick=function(){S.tab=b.dataset.tab;S.shown=PAGE;render();};});
$('q').addEventListener('input',function(){S.q=this.value;S.shown=PAGE;render();});
$('confirm-green').onclick=function(){groupsOf(S.tab).forEach(function(g){if(g.status==='green')g.members.forEach(function(m){draft[S.tab][m.name]={c:g.canonical,f:true};});});save();render();};
window.addEventListener('beforeunload',function(e){if(!window.__saved&&changes().total){e.preventDefault();e.returnValue='';}});
(function(){var p=['trainer','reviewer'].some(function(t){return groupsOf(t).some(function(g){return g.status==='amber'||g.status==='green';});});if(!p)S.filter='clean';})();
render();
})();
