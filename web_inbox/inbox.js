/* =============================================================
   VCL Inbox — Option A · Daylight — live, responsive, triage
   Runs as the `javascript` field of the /vcl-inbox Web Page.
   3-pane respond.io-style inbox over VCL Message / Conversation,
   with Follow-ups (tag a message -> it moves to Tracked).
   ============================================================= */
frappe.ready(function () {
  "use strict";

  var ROOT = document.getElementById("vcl-inbox-root");
  if (!ROOT) return;

  var MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var FUP_API = "vcl_messaging.vcl_messaging.followups_api.";
  // Follow-up pipeline — section order in the Tracked view.
  var FU_STATUSES = ["Pending","Cheque Pending Collection","Pending Review",
    "Escalated","Completed","Cancelled"];
  var ACTIVE_FU = ["Pending","Cheque Pending Collection","Pending Review","Escalated"];
  var FU_TYPES = ["Payment Entry","Purchase Order","Sales Order","General"];
  var CAT_FUTYPE = { payment:"Payment Entry", purchase_order:"Purchase Order",
                     sales_order:"Sales Order" };
  function futypeForCat(c){ return CAT_FUTYPE[c] || "General"; }

  var S = { convs:[], convList:[], msgs:[], byConv:{}, custs:[], custNorm:[],
            followups:[], fuByMsg:{},
            activeConv:null, activeMsg:null, view:"open", mview:"list",
            ctxOpen:false, tier1Missing:false, fuForm:null, peCands:null,
            payForm:null, threadAll:false, scrollThreadBottom:false, busy:"" };

  /* ---------- helpers ---------- */
  function esc(s){ return (s==null?"":String(s))
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function pad(n){ return (n<10?"0":"")+n; }
  function statusCls(s){ return (s||"").replace(/ /g,""); }

  function fmtTime(s){
    if(!s) return "";
    var d=new Date(String(s).replace(" ","T"));
    if(isNaN(d.getTime())) return esc(s);
    return d.getDate()+" "+MONTHS[d.getMonth()]+" "+pad(d.getHours())+":"+pad(d.getMinutes());
  }
  function fmtDate(s){
    if(!s) return "";
    var d=new Date(String(s).replace(" ","T"));
    if(isNaN(d.getTime())) return esc(s);
    return d.getDate()+" "+MONTHS[d.getMonth()];
  }
  function nextFriday(){
    var d=new Date(), add=(5-d.getDay()+7)%7; if(add===0) add=7;
    d.setDate(d.getDate()+add);
    return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate());
  }
  function todayStr(){ var d=new Date();
    return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate()); }

  function parseArr(s){
    if(!s) return [];
    if(Array.isArray(s)) return s;
    try{ var v=JSON.parse(s); return Array.isArray(v)?v:[]; }catch(e){ return []; }
  }
  function normName(s){
    return (s||"").toLowerCase().replace(/[^a-z0-9 ]/g," ")
      .replace(/\b(ltd|limited|pvt|company|co|enterprises|enterprise|kenya|the|and)\b/g," ")
      .replace(/\s+/g," ").trim();
  }
  function matchCustomer(mention){
    var m=normName(mention); if(!m||m.length<3) return null;
    var i,cn;
    for(i=0;i<S.custNorm.length;i++){ if(S.custNorm[i]===m) return S.custs[i]; }
    for(i=0;i<S.custNorm.length;i++){
      cn=S.custNorm[i];
      if(cn&&cn.length>=4&&(cn.indexOf(m)>=0||m.indexOf(cn)>=0)) return S.custs[i];
    }
    var mt=m.split(" ").filter(function(t){return t.length>=4;});
    if(mt.length){ for(i=0;i<S.custNorm.length;i++){
      cn=S.custNorm[i]; if(!cn) continue;
      var ct=cn.split(" ");
      if(mt.some(function(t){return ct.indexOf(t)>=0;})) return S.custs[i];
    }}
    return null;
  }
  function extractPayment(text){
    var o={}; if(!text) return o;
    var am=text.match(/(?:KES|KSh|Ksh|Kshs|Sh)\s*\.?\s*([\d,]+(?:\.\d+)?)/)
        || text.match(/\b([\d,]{4,}(?:\.\d+)?)\s*(?:\/=|\/-)/);
    if(am) o.amount=am[1].replace(/,/g,"");
    var chq=text.match(/che?que\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{3,})/i);
    if(chq) o.instrument="Cheque "+chq[1];
    if(!o.instrument){ var mp=text.match(/\b([A-Z0-9]{10})\b/);
      if(mp&&/[A-Z]/.test(mp[1])&&/[0-9]/.test(mp[1])) o.instrument="M-Pesa "+mp[1]; }
    if(!o.instrument&&/\brtgs\b/i.test(text)) o.instrument="RTGS";
    if(!o.instrument&&/pesalink/i.test(text)) o.instrument="Pesalink";
    var ref=text.match(/\b(INV[-\/ ]?[A-Za-z0-9\-]+|PO[-\/ ]?[A-Za-z0-9\-]+)\b/i);
    if(ref) o.ref=ref[1];
    return o;
  }

  function custOptions(){
    var h="";
    S.custs.forEach(function(c){ h+='<option value="'+esc(c.customer_name||c.name)+'">'; });
    return h;
  }
  function custDisplay(name){
    var c=S.custs.filter(function(x){return x.name===name;})[0];
    return c?(c.customer_name||c.name):name;
  }
  function custHint(v){
    v=(v||"").trim(); if(!v) return "";
    var c=matchCustomer(v);
    return c
      ? '<span class="ok">&#10003; links to Customer: '+esc(c.customer_name||c.name)+'</span>'
      : '<span class="warn">not in the Customer master — will be saved as text</span>';
  }

  function call(method, args){
    return new Promise(function(res,rej){
      frappe.call({ method:method, args:args||{},
        callback:function(r){ res(r&&r.message); }, error:function(e){ rej(e); } });
    });
  }
  function getList(doctype, fields, opts){
    opts=opts||{};
    return new Promise(function(res,rej){
      frappe.call({ method:"frappe.client.get_list",
        args:{ doctype:doctype, fields:fields, limit_page_length:opts.limit||0,
               order_by:opts.order_by||"creation desc", filters:opts.filters||[] },
        callback:function(r){ res((r&&r.message)||[]); }, error:function(e){ rej(e); } });
    });
  }

  /* ---------- load ---------- */
  function loadAll(){
    ROOT.innerHTML='<div class="ix-boot">Loading VCL Inbox…</div>';
    var convP=getList("VCL Conversation",
      ["name","whatsapp_group_name","whatsapp_group_id","channel"],{limit:0});
    var custP=getList("Customer",["name","customer_name"],
      {limit:0,order_by:"customer_name asc"}).catch(function(){return [];});
    // Wildcard fields — returns whatever columns exist on the live site, so
    // a field added to the app but not yet deployed never breaks the inbox.
    var msgP=getList("VCL Message",["*"],{order_by:"creation asc"});
    var fupP=call(FUP_API+"get_followups").catch(function(){ return []; });

    Promise.all([convP,custP,msgP,fupP]).then(function(r){
      var convs=r[0],custs=r[1],msgs=r[2],fups=r[3]||[];
      S.custs=custs;
      S.custNorm=custs.map(function(c){ return normName(c.customer_name||c.name); });
      S.convs=convs;
      S.msgs=msgs;
      S.byConv={};
      msgs.forEach(function(m){ (S.byConv[m.conversation]=S.byConv[m.conversation]||[]).push(m); });
      S.followups=fups;
      S.fuByMsg={};
      fups.forEach(function(f){ if(!S.fuByMsg[f.message]) S.fuByMsg[f.message]=f; });
      deriveConvs();
      if(!S.activeConv && S.convList.length) S.activeConv=S.convList[0].conv.name;
      pickActiveMsg();
      render();
    }).catch(function(e){
      ROOT.innerHTML='<div class="ix-empty"><b>Could not load the inbox.</b>'
        +esc((e&&e.message)||"Are you logged in to ERPNext?")+'</div>';
    });
  }

  function actionWorthy(m){
    return m.ai_priority==="HIGH"||m.ai_priority==="CRIT"||m.ai_category==="payment"
      ||m.ai_mentions_tanuj;
  }
  function deriveConvs(){
    var PR={CRIT:3,HIGH:2,MED:1,LOW:0};
    S.convList=S.convs.map(function(c){
      var ms=S.byConv[c.name]||[];
      var last=ms[ms.length-1], rank=0, hasPay=false, untriaged=0;
      ms.forEach(function(m){
        var p=PR[m.ai_priority]||0; if(p>rank) rank=p;
        if(m.ai_category==="payment") hasPay=true;
        if(actionWorthy(m) && !S.fuByMsg[m.name] && !m.inbox_ignored) untriaged++;
      });
      return { conv:c, msgs:ms, last:last, rank:rank, hasPay:hasPay,
               untriaged:untriaged,
               lastTime:(last&&(last.sent_at||last.creation))||"" };
    }).filter(function(x){ return x.msgs.length>0; });
    S.convList.sort(function(a,b){ return b.lastTime<a.lastTime?-1:b.lastTime>a.lastTime?1:0; });
  }
  function pickActiveMsg(){
    var ms=S.byConv[S.activeConv]||[];
    if(!ms.length){ S.activeMsg=null; return; }
    if(S.activeMsg && ms.some(function(m){return m.name===S.activeMsg;})) return;
    S.activeMsg=ms[ms.length-1].name;
  }
  function rankName(r){ return r>=3?"CRIT":r>=2?"HIGH":r>=1?"MED":"LOW"; }

  /* ---------- render ---------- */
  function render(){
    var t={
      open: railCount("open"),
      pending: railCount("pending"),
      closed: railCount("closed"),
      pay: S.msgs.filter(function(m){ return m.ai_category==="payment"; }).length,
    };

    var h='<header class="ix-top">'
      +'<div class="ix-brand">VCL<span>INBOX</span></div>'
      +'<div class="ix-stats">'
      + stat(t.open,"Open Items","keep-xs")
      + stat(t.pending,"Pending","warn")
      + stat(t.closed,"Closed","hide-sm")
      + stat(t.pay,"Payments","hide-sm")
      +'</div>'
      +'<button class="ix-refresh" id="ix-refresh">refresh</button></header>';

    h+='<div class="ix-main">'+renderRail()+renderThread()+renderCtx()+'</div>';

    // Snapshot scroll positions — a full re-render must not jump to the top.
    var snap={};
    var elC=ROOT.querySelector(".ix-convs"); if(elC) snap.convs=elC.scrollTop;
    var elM=ROOT.querySelector(".ix-msgs"); if(elM) snap.msgs=elM.scrollTop;
    var elX=document.getElementById("ix-ctx-body"); if(elX) snap.ctx=elX.scrollTop;

    ROOT.className="ix-app"+(S.ctxOpen?" ctx-open":"");
    ROOT.setAttribute("data-view",S.mview);
    ROOT.innerHTML=h;
    wire();

    // Restore scroll. Thread lands at the latest message when a conversation
    // was just opened; otherwise it stays exactly where it was.
    var nC=ROOT.querySelector(".ix-convs");
    if(nC && snap.convs!=null) nC.scrollTop=snap.convs;
    var nX=document.getElementById("ix-ctx-body");
    if(nX && snap.ctx!=null) nX.scrollTop=snap.ctx;
    var nM=ROOT.querySelector(".ix-msgs");
    if(nM){
      if(S.scrollThreadBottom){
        var onM=nM.querySelector(".ix-msg.on");
        if(onM && onM.scrollIntoView){ onM.scrollIntoView({block:"center"}); }
        else { nM.scrollTop=nM.scrollHeight; }
        S.scrollThreadBottom=false;
      }
      else if(snap.msgs!=null){ nM.scrollTop=snap.msgs; }
    }
  }
  function stat(n,label,cls){
    return '<div class="ix-stat '+cls+'"><b>'+n+'</b><i>'+esc(label)+'</i></div>';
  }

  var PRANK={CRIT:3,HIGH:2,MED:1,LOW:0};

  function renderRail(){
    var h='<aside class="ix-rail"><div class="ix-rail-hd"><div class="ix-views">'
      + vbtn("open","Open Items") + vbtn("pending","Pending")
      + vbtn("closed","Closed") + vbtn("all","All")
      +'</div></div><div class="ix-convs">';
    var items=railItems();
    if(!items.length){
      h+='<div class="ix-empty">'+railEmpty()+'</div>';
    } else {
      items.forEach(function(it){ h+= it.fu ? fuRow(it.fu) : msgRow(it.msg); });
    }
    return h+'</div></aside>';
  }
  function vbtn(v,label){
    var n=railCount(v);
    return '<button class="ix-view'+(S.view===v?" on":"")+'" data-view="'+v+'">'
      +esc(label)+(n!=null?' <span class="ix-view-ct">'+n+'</span>':'')+'</button>';
  }
  function railEmpty(){
    if(S.view==="open") return '<b>Nothing to triage.</b>Open Items is clear.';
    if(S.view==="pending") return '<b>Nothing pending.</b>No active follow-ups.';
    if(S.view==="closed") return 'Nothing closed yet.';
    return 'No messages.';
  }
  function railCount(v){
    if(v==="open") return S.msgs.filter(function(m){
      return !S.fuByMsg[m.name] && !m.inbox_ignored; }).length;
    if(v==="pending") return S.followups.filter(function(f){
      return ACTIVE_FU.indexOf(f.status)>=0; }).length;
    if(v==="closed") return S.followups.filter(function(f){
      return f.status==="Completed"||f.status==="Cancelled"; }).length;
    return null;  // "All" carries no count
  }
  function fuSort(a,b){
    var o={Escalated:0,Pending:1,"Cheque Pending Collection":2,"Pending Review":3};
    return (o[a.status]||9)-(o[b.status]||9) || (a.due_date<b.due_date?-1:1);
  }
  function railItems(){
    if(S.view==="pending"){
      return S.followups.filter(function(f){ return ACTIVE_FU.indexOf(f.status)>=0; })
        .sort(fuSort).map(function(f){ return {fu:f}; });
    }
    if(S.view==="closed"){
      return S.followups.filter(function(f){
          return f.status==="Completed"||f.status==="Cancelled"; })
        .sort(function(a,b){ return (b.name<a.name?-1:1); })
        .map(function(f){ return {fu:f}; });
    }
    var list=S.msgs.slice();
    if(S.view==="open"){
      list=list.filter(function(m){ return !S.fuByMsg[m.name] && !m.inbox_ignored; });
    }
    list.sort(function(a,b){
      var d=(PRANK[b.ai_priority]||0)-(PRANK[a.ai_priority]||0);
      return d || (b.creation<a.creation?-1:1);
    });
    return list.map(function(m){ return {msg:m}; });
  }
  function convName(cid){
    var c=S.convs.filter(function(x){ return x.name===cid; })[0];
    return c?(c.whatsapp_group_name||c.name):cid;
  }
  function msgRow(m){
    var pri=m.ai_priority||"";
    var prev=m.ai_summary||m.content||("["+(m.message_type||"media")+"]");
    return '<button class="ix-row'+(m.name===S.activeMsg?" on":"")
      +'" data-msg="'+esc(m.name)+'">'
      +'<span class="ix-row-spine p-'+(pri||"LOW")+'"></span>'
      +'<span class="ix-row-body">'
      +'<span class="ix-row-top"><span class="ix-row-who">'+esc(m.sender_name||"?")+'</span>'
      +'<span class="ix-row-grp">'+esc(convName(m.conversation))+'</span>'
      +'<span class="ix-row-time">'+fmtTime(m.sent_at||m.creation)+'</span></span>'
      +'<span class="ix-row-prev">'+esc(prev)+'</span>'
      +'<span class="ix-row-tags">'
      +(pri?'<span class="ix-tag p-'+esc(pri)+'">'+esc(pri)+'</span>':'')
      +(m.ai_category?'<span class="ix-tag cat">'+esc(m.ai_category.replace(/_/g," "))+'</span>':'')
      +(m.inbox_ignored?'<span class="ix-tag mut">ignored</span>':'')
      +'</span></span></button>';
  }
  function fuRow(fu){
    var who=fu.customer?custDisplay(fu.customer):(fu.customer_text||"—");
    var overdue=(ACTIVE_FU.indexOf(fu.status)>=0&&fu.due_date&&fu.due_date<todayStr());
    return '<button class="ix-row'+(fu.message===S.activeMsg?" on":"")
      +'" data-fu-open="'+esc(fu.name)+'">'
      +'<span class="ix-row-spine s-'+statusCls(fu.status)+'"></span>'
      +'<span class="ix-row-body">'
      +'<span class="ix-row-top"><span class="ix-row-who">'+esc(who)+'</span>'
      +(fu.expected_amount?'<span class="ix-row-amt">KES '
        +Number(fu.expected_amount).toLocaleString()+'</span>':'')
      +'<span class="ix-row-time'+(overdue?" od":"")+'">'
      +(fu.due_date?fmtDate(fu.due_date):"")+'</span></span>'
      +'<span class="ix-row-prev">'+esc(fu.action||"")+'</span>'
      +'<span class="ix-row-tags">'
      +'<span class="ix-tag s-'+statusCls(fu.status)+'">'+esc(fu.status)+'</span>'
      +'<span class="ix-tag cat">'+esc(fu.followup_type||"")+'</span>'
      +'</span></span></button>';
  }

  function renderThread(){
    var x=S.convList.filter(function(y){return y.conv.name===S.activeConv;})[0];
    var h='<section class="ix-thread"><div class="ix-pane">';
    if(!x){ return h+'<div class="ix-empty">Select a conversation.</div></div></section>'; }
    var c=x.conv;
    h+='<header class="ix-thread-hd">'
      +'<button class="ix-back" id="ix-back" title="Back">&#8592;</button>'
      +'<div><span class="ix-th-name">'+esc(c.whatsapp_group_name||c.name)+'</span>'
      +'<span class="ix-th-sub">'+x.msgs.length+' messages · '
      +x.untriaged+' need attention</span></div></header>';
    h+='<div class="ix-msgs">';
    // Window the thread to the clicked message + 4 each side — opening a
    // conversation must not dump the whole group history into the pane.
    var ms=x.msgs, WIN=4, ai=-1, ti;
    for(ti=0;ti<ms.length;ti++){ if(ms[ti].name===S.activeMsg){ ai=ti; break; } }
    var lo=0, hi=ms.length;
    if(!S.threadAll && ai>=0 && ms.length>WIN*2+1){
      lo=Math.max(0,ai-WIN); hi=Math.min(ms.length,ai+WIN+1);
    }
    if(lo>0){
      h+='<button class="ix-thread-more" id="ix-thread-earlier">&#8593; '+lo
        +' earlier message'+(lo>1?'s':'')+' — show whole conversation</button>';
    }
    ms.slice(lo,hi).forEach(function(m){ h+=msgBubble(m); });
    if(hi<ms.length){
      h+='<button class="ix-thread-more" id="ix-thread-later">&#8595; '+(ms.length-hi)
        +' more message'+((ms.length-hi)>1?'s':'')+' — show whole conversation</button>';
    }
    h+='</div>';
    h+='<footer class="ix-composer">'
      +'<input class="ix-compose-in" placeholder="Reply to '+esc(c.whatsapp_group_name||"")+'" disabled>'
      +'<button class="ix-send" disabled>Send</button>'
      +'<span class="ix-compose-note">Reply path ships next</span></footer>';
    return h+'</div></section>';
  }

  function msgBubble(m){
    var isText=(m.message_type||"text")==="text";
    var fu=S.fuByMsg[m.name];
    var h='<article class="ix-msg'+(m.name===S.activeMsg?" on":"")
      +((fu||m.inbox_ignored)?" is-tracked":"")+'" data-msg="'+esc(m.name)+'">';
    h+='<div class="ix-msg-meta"><span class="ix-from">'+esc(m.sender_name||"?")
      +'</span><span class="ix-time">'+fmtTime(m.sent_at||m.creation)+'</span></div>';
    if(!isText){
      if(m.message_type==="image"&&m.media_url&&/^\/(private|files)/.test(m.media_url)){
        h+='<div class="ix-thumb"><img src="'+esc(m.media_url)+'" alt="image" loading="lazy"></div>';
      } else {
        h+='<div class="ix-media"><span class="tag">'+esc((m.message_type||"media").toUpperCase())
          +'</span><span>'+esc(m.content||m.media_mime_type||"")+'</span></div>';
      }
    }
    if(m.content&&isText){
      var long=m.content.length>600;
      h+='<div class="ix-bubble'+(long?" clip":"")+'">'+esc(m.content)+'</div>';
    } else if(!m.content&&isText){
      h+='<div class="ix-bubble" style="color:var(--muted);font-style:italic">(empty)</div>';
    }
    var chips='';
    if(m.ai_priority) chips+='<span class="ix-pri p-'+esc(m.ai_priority)+'">'+esc(m.ai_priority)+'</span>';
    if(m.ai_category) chips+='<span class="ix-cat c-'+esc(m.ai_category)+'">'
      +esc(m.ai_category.replace(/_/g," "))+'</span>';
    else if(isText) chips+='<span class="ix-cat c-pending">unclassified</span>';
    if(!isText&&m.ai_kind) chips+='<span class="ix-cat">'+esc(m.ai_kind)+'</span>';
    if(m.ai_mentions_tanuj) chips+='<span class="ix-pri p-CRIT">@ TANUJ</span>';
    if(fu) chips+='<span class="ix-trk s-'+statusCls(fu.status)+'">&#10003; '+esc(fu.status)+'</span>';
    else if(m.inbox_ignored) chips+='<span class="ix-trk s-Cancelled">ignored</span>';
    if(m.ai_summary) chips+='<span class="ix-ai-sum">'+esc(m.ai_summary)+'</span>';
    if(chips) h+='<div class="ix-ai">'+chips+'</div>';
    return h+'</article>';
  }

  function renderCtx(){
    var h='<aside class="ix-ctx"><div class="ix-ctx-hd"><span>Message intelligence</span>'
      +'<button class="ix-ctx-close" id="ix-ctx-close" title="Close">&#10005;</button></div>'
      +'<div id="ix-ctx-body">';
    var m=S.msgs.filter(function(x){return x.name===S.activeMsg;})[0];
    h+= m ? ctxBody(m) : '<div class="ix-empty">Select a message.</div>';
    return h+'</div></aside>';
  }

  function ctxBody(m){
    var h='', isText=(m.message_type||"text")==="text";

    /* claude read */
    h+='<div class="ix-c-card ix-c-read"><div class="ix-c-label">Claude read</div>';
    h+= m.ai_summary
      ? '<div class="ix-c-readtext">'+esc(m.ai_summary)+'</div>'
      : '<div class="ix-c-readtext pending">Awaiting classification.</div>';
    var fl='';
    if(m.ai_priority) fl+='<span class="ix-pri p-'+esc(m.ai_priority)+'">'+esc(m.ai_priority)+'</span>';
    if(m.ai_category) fl+='<span class="ix-cat c-'+esc(m.ai_category)+'">'
      +esc(m.ai_category.replace(/_/g," "))+'</span>';
    if(!isText&&m.ai_kind) fl+='<span class="ix-cat">'+esc(m.ai_kind)+'</span>';
    if(fl) h+='<div class="ix-c-flags">'+fl+'</div>';
    h+='</div>';

    /* follow-up block */
    h+=ctxFollowup(m);

    /* original message */
    h+='<div class="ix-c-card"><div class="ix-c-label">Original message</div>';
    if(m.content) h+='<div class="ix-c-orig">'+esc(m.content)+'</div>';
    else if(!isText) h+='<div class="ix-c-orig" style="color:var(--muted)">['
      +esc(m.message_type)+(m.media_mime_type?" · "+esc(m.media_mime_type):"")+']</div>';
    else h+='<div class="ix-c-orig" style="color:var(--muted)">(empty)</div>';
    h+='</div>';

    /* customer match */
    var mentions=parseArr(m.ai_customer_mentions);
    h+='<div class="ix-c-card"><div class="ix-c-label">Matched to ERPNext</div>';
    if(mentions.length){
      h+='<div class="ix-c-matches">';
      mentions.forEach(function(nm){
        var c=matchCustomer(nm);
        h+= c
          ? '<a class="ix-match hit" target="_blank" href="/app/customer/'
            +encodeURIComponent(c.name)+'"><span class="ix-match-dot"></span>'
            +'<span class="ix-match-nm">'+esc(c.customer_name||c.name)+'</span>'
            +'<span class="ix-match-tag">Customer</span></a>'
          : '<span class="ix-match miss"><span class="ix-match-dot"></span>'
            +'<span class="ix-match-nm">'+esc(nm)+'</span>'
            +'<span class="ix-match-tag">not in master</span></span>';
      });
      h+='</div>';
    } else h+='<div class="ix-c-empty">No company names detected.</div>';
    h+='</div>';

    /* source */
    var conv=S.convs.filter(function(c){return c.name===m.conversation;})[0]||{};
    h+='<div class="ix-c-card"><div class="ix-c-label">Source</div>'
      +mr("Group",conv.whatsapp_group_name||m.conversation)
      +mr("Sender",m.sender_name||"?")
      +mr("Received",fmtTime(m.sent_at||m.creation))
      +mr("Type",m.message_type||"text")
      +mr("Record",m.name)+'</div>';
    return h;
  }

  function ctxFollowup(m){
    var fu=S.fuByMsg[m.name];
    /* create form open for this message? */
    if(S.fuForm && S.fuForm.message===m.name){
      return fuFormHtml(m);
    }
    if(!fu){
      if(m.inbox_ignored){
        return '<div class="ix-c-card ix-c-fu s-Cancelled"><div class="ix-c-label">Triage</div>'
          +'<div class="ix-fu-row"><span class="ix-fu-status s-Cancelled">IGNORED</span></div>'
          +'<div class="ix-c-empty">Marked no-action — hidden from the Inbox view.</div>'
          +'<div class="ix-fu-btns">'
          +'<button class="ix-btn" id="ix-unignore">Un-ignore</button>'
          +'<button class="ix-btn primary" id="ix-fu-add">+ Add follow-up</button>'
          +'</div></div>';
      }
      return '<div class="ix-c-card ix-c-fu"><div class="ix-c-label">Triage</div>'
        +'<div class="ix-c-empty">Not tracked. Add a follow-up to action this, or '
        +'ignore it — either way it leaves the Inbox.</div>'
        +'<div class="ix-fu-btns">'
        +'<button class="ix-btn primary" id="ix-fu-add">+ Add follow-up</button>'
        +'<button class="ix-btn ghost" id="ix-ignore">Ignore</button>'
        +'</div></div>';
    }
    /* existing follow-up */
    var custHtml = fu.customer
      ? '<a class="ix-fu-cust" target="_blank" href="/app/customer/'
        +encodeURIComponent(fu.customer)+'">'+esc(custDisplay(fu.customer))+'</a>'
      : '<span class="ix-fu-cust-txt">'+esc(fu.customer_text||"—")+' · not in master</span>';
    var active = ACTIVE_FU.indexOf(fu.status)>=0;
    var h='<div class="ix-c-card ix-c-fu s-'+statusCls(fu.status)+'">'
      +'<div class="ix-c-label">Follow-up</div>'
      +'<div class="ix-fu-row"><span class="ix-fu-status s-'+statusCls(fu.status)+'">'
      +esc(fu.status)+'</span>'
      +'<span class="ix-fu-due">'+(fu.due_date?"check by "+fmtDate(fu.due_date):"")+'</span></div>'
      +'<div class="ix-fu-retag"><span>Type</span><select data-fu-retag="'+esc(fu.name)+'">'
      + FU_TYPES.map(function(t){
          return '<option'+(t===fu.followup_type?" selected":"")+'>'+esc(t)+'</option>'; }).join("")
      +'</select></div>'
      +'<div class="ix-fu-act">'+esc(fu.action||"")+'</div>'
      +'<div class="ix-fu-meta">'+custHtml
      +(fu.expected_amount?' · KES '+Number(fu.expected_amount).toLocaleString():'')+'</div>';

    /* payment detail — name · account · amount · date */
    if(fu.linked_payment_entry||fu.payment_account||fu.payment_ref||fu.payment_date){
      h+='<div class="ix-fu-pay">';
      if(fu.linked_payment_entry)
        h+='<a class="ix-fu-pe" target="_blank" href="/app/payment-entry/'
          +encodeURIComponent(fu.linked_payment_entry)+'">Payment Entry · '
          +esc(fu.linked_payment_entry)+'</a>';
      if(fu.payment_account) h+=mr("Account",fu.payment_account);
      if(fu.payment_ref) h+=mr("Ref",fu.payment_ref);
      if(fu.payment_date) h+=mr("Date",fmtDate(fu.payment_date));
      h+='</div>';
    }

    if(active && S.payForm===fu.name){
      h+=payFormHtml(fu);
    } else if(active){
      h+='<div class="ix-fu-btns">';
      if((fu.followup_type||"")==="Payment Entry"){
        h+='<button class="ix-btn" data-fu-find="'+esc(fu.name)+'">'
          +(S.busy==="find:"+fu.name?"Searching…":"Find Payment Entry")+'</button>'
          +'<button class="ix-btn" data-fu-manual="'+esc(fu.name)+'">Enter manually</button>';
      }
      h+='<button class="ix-btn ok" data-fu-complete="'+esc(fu.name)+'">Mark Completed</button>'
        +'<button class="ix-btn ghost" data-fu-cancel="'+esc(fu.name)+'">Cancel</button>'
        +'</div>';
      /* PE candidates — name · account · amount · date · ref · mode */
      if(S.peCands && S.peCands.fu===fu.name){
        if(!S.peCands.list.length){
          h+='<div class="ix-c-empty">No matching Payment Entry — use '
            +'"Enter manually", or raise one in ERPNext first.</div>';
        } else {
          h+='<div class="ix-pe-list">';
          S.peCands.list.forEach(function(pe){
            var amt=Number(pe.received_amount||pe.paid_amount||0).toLocaleString();
            h+='<div class="ix-pe"><div class="ix-pe-main">'
              +'<span class="ix-pe-name">'+esc(pe.name)+'</span>'
              +'<span class="ix-pe-amt">KES '+amt+'</span></div>'
              +'<div class="ix-pe-sub">'
              +(pe.paid_to?esc(pe.paid_to)+' · ':'')
              +esc(pe.posting_date||"")
              +(pe.reference_no?' · ref '+esc(pe.reference_no):'')
              +(pe.mode_of_payment?' · '+esc(pe.mode_of_payment):'')
              +(pe.docstatus===1?' · submitted':' · draft')+'</div>'
              +'<button class="ix-btn small" data-fu-link="'+esc(fu.name)
              +'" data-pe="'+esc(pe.name)+'">Link &amp; move to Pending Review</button></div>';
          });
          h+='</div>';
        }
      }
    }
    h+='</div>';
    return h;
  }

  function payFormHtml(fu){
    return '<div class="ix-pay-form"><div class="ix-c-label">Enter payment manually</div>'
      +'<label class="ix-fl">Payment / cheque ref</label>'
      +'<input class="ix-fi" id="ix-pf-ref" value="'+esc(fu.payment_ref||"")+'">'
      +'<label class="ix-fl">Bank / cash account</label>'
      +'<input class="ix-fi" id="ix-pf-acct" value="'+esc(fu.payment_account||"")+'">'
      +'<div class="ix-fl-row">'
      +'<div><label class="ix-fl">Amount (KES)</label>'
      +'<input class="ix-fi" id="ix-pf-amt" value="'+esc(fu.expected_amount||"")+'"></div>'
      +'<div><label class="ix-fl">Date</label>'
      +'<input class="ix-fi" type="date" id="ix-pf-date" value="'+esc(fu.payment_date||"")+'"></div>'
      +'</div><div class="ix-fu-btns">'
      +'<button class="ix-btn primary" data-pf-save="'+esc(fu.name)+'">'
      +(S.busy==="resolve"?"Saving…":"Save → Pending Review")+'</button>'
      +'<button class="ix-btn ghost" id="ix-pf-cancel">Cancel</button></div></div>';
  }

  function fuFormHtml(m){
    var f=S.fuForm;
    return '<div class="ix-c-card ix-c-fu"><div class="ix-c-label">New follow-up</div>'
      +'<label class="ix-fl">Type</label>'
      +'<select class="ix-fi" id="ix-fuf-type">'
      + FU_TYPES.map(function(t){
          return '<option'+(t===f.type?' selected':'')+'>'+esc(t)+'</option>'; }).join('')
      +'</select>'
      +'<label class="ix-fl">Customer · linked to the Customer master</label>'
      +'<input class="ix-fi" id="ix-fuf-cust" list="ix-cust-list" autocomplete="off" '
      +'placeholder="Type to search the Customer master" value="'+esc(f.customer)+'">'
      +'<datalist id="ix-cust-list">'+custOptions()+'</datalist>'
      +'<div class="ix-fl-hint" id="ix-fuf-custhint">'+custHint(f.customer)+'</div>'
      +'<label class="ix-fl">Action</label>'
      +'<textarea class="ix-fi" id="ix-fuf-action" rows="3">'+esc(f.action)+'</textarea>'
      +'<div class="ix-fl-row">'
      +'<div><label class="ix-fl">Expected amount (KES)</label>'
      +'<input class="ix-fi" id="ix-fuf-amt" value="'+esc(f.amount)+'"></div>'
      +'<div><label class="ix-fl">Check by</label>'
      +'<input class="ix-fi" type="date" id="ix-fuf-due" value="'+esc(f.due)+'"></div>'
      +'</div>'
      +'<div class="ix-fu-btns">'
      +'<button class="ix-btn primary" id="ix-fuf-save">'
      +(S.busy==="create"?"Saving…":"Create follow-up")+'</button>'
      +'<button class="ix-btn ghost" id="ix-fuf-cancel">Cancel</button></div>'
      +'<div class="ix-fl-note">Owner &amp; escalation default to you (Tanuj).</div>'
      +'</div>';
  }
  function mr(k,v){ return '<div class="ix-mr"><span class="ix-mr-k">'+esc(k)
    +'</span><span class="ix-mr-v">'+esc(v)+'</span></div>'; }

  /* ---------- prefill ---------- */
  function openFuForm(m){
    var mentions=parseArr(m.ai_customer_mentions);
    var custName=mentions.length?mentions[0]:"";
    var matched=custName?matchCustomer(custName):null;
    var pay=extractPayment((m.ai_summary||"")+" "+(m.content||""));
    var ftype=futypeForCat(m.ai_category);
    var act={
      "Payment Entry":"Confirm this payment landed — raise the Payment Entry.",
      "Purchase Order":"Place the Purchase Order / LPO — "+(m.ai_summary||""),
      "Sales Order":"Raise the Sales Order — "+(m.ai_summary||""),
    }[ftype] || (m.ai_summary||"Follow up on this message.");
    S.fuForm={ message:m.name, type:ftype,
      customer:(matched?(matched.customer_name||matched.name):custName),
      action:act, amount:pay.amount||"", due:nextFriday() };
    render();
  }

  /* ---------- events ---------- */
  function wire(){
    var rf=document.getElementById("ix-refresh");
    if(rf) rf.addEventListener("click", loadAll);

    ROOT.querySelectorAll(".ix-view").forEach(function(b){
      b.addEventListener("click", function(){
        S.view=b.getAttribute("data-view"); render();
      });
    });
    ROOT.querySelectorAll(".ix-row[data-msg]").forEach(function(b){
      b.addEventListener("click", function(){
        var m=S.msgs.filter(function(x){ return x.name===b.getAttribute("data-msg"); })[0];
        if(!m) return;
        S.activeConv=m.conversation; S.activeMsg=m.name;
        S.fuForm=null; S.peCands=null; S.payForm=null; S.threadAll=false;
        S.mview="thread"; S.ctxOpen=true; S.scrollThreadBottom=true; render();
      });
    });
    ROOT.querySelectorAll(".ix-row[data-fu-open]").forEach(function(b){
      b.addEventListener("click", function(){
        var fu=S.followups.filter(function(f){
          return f.name===b.getAttribute("data-fu-open"); })[0];
        if(!fu) return;
        S.activeConv=fu.conversation; S.activeMsg=fu.message;
        S.fuForm=null; S.peCands=null; S.payForm=null; S.threadAll=false;
        S.mview="thread"; S.ctxOpen=true; S.scrollThreadBottom=true; render();
      });
    });
    ROOT.querySelectorAll(".ix-msg").forEach(function(a){
      a.addEventListener("click", function(){
        S.activeMsg=a.getAttribute("data-msg");
        S.fuForm=null; S.peCands=null; S.payForm=null; S.ctxOpen=true; render();
      });
    });
    var bk=document.getElementById("ix-back");
    if(bk) bk.addEventListener("click", function(){ S.mview="list"; render(); });
    var cc=document.getElementById("ix-ctx-close");
    if(cc) cc.addEventListener("click", function(){ S.ctxOpen=false; render(); });
    var tEarlier=document.getElementById("ix-thread-earlier");
    if(tEarlier) tEarlier.addEventListener("click", function(){ S.threadAll=true; render(); });
    var tLater=document.getElementById("ix-thread-later");
    if(tLater) tLater.addEventListener("click", function(){ S.threadAll=true; render(); });

    /* follow-up: add */
    var add=document.getElementById("ix-fu-add");
    if(add) add.addEventListener("click", function(){
      var m=S.msgs.filter(function(x){return x.name===S.activeMsg;})[0];
      if(m) openFuForm(m);
    });
    /* triage: ignore / un-ignore */
    var ign=document.getElementById("ix-ignore");
    if(ign) ign.addEventListener("click", function(){ setIgnored(S.activeMsg,1); });
    var unign=document.getElementById("ix-unignore");
    if(unign) unign.addEventListener("click", function(){ setIgnored(S.activeMsg,0); });
    /* follow-up: form */
    var save=document.getElementById("ix-fuf-save");
    if(save) save.addEventListener("click", submitFuForm);
    var fcancel=document.getElementById("ix-fuf-cancel");
    if(fcancel) fcancel.addEventListener("click", function(){ S.fuForm=null; render(); });
    var custIn=document.getElementById("ix-fuf-cust");
    if(custIn) custIn.addEventListener("input", function(){
      var hint=document.getElementById("ix-fuf-custhint");
      if(hint) hint.innerHTML=custHint(custIn.value);
    });
    /* follow-up: resolve */
    ROOT.querySelectorAll("[data-fu-find]").forEach(function(b){
      b.addEventListener("click", function(){ findPE(b.getAttribute("data-fu-find")); });
    });
    ROOT.querySelectorAll("[data-fu-manual]").forEach(function(b){
      b.addEventListener("click", function(){
        S.payForm=b.getAttribute("data-fu-manual"); S.peCands=null; render();
      });
    });
    ROOT.querySelectorAll("[data-fu-complete]").forEach(function(b){
      b.addEventListener("click", function(){
        resolveFu(b.getAttribute("data-fu-complete"),"Completed");
      });
    });
    ROOT.querySelectorAll("[data-fu-cancel]").forEach(function(b){
      b.addEventListener("click", function(){ resolveFu(b.getAttribute("data-fu-cancel"),"Cancelled"); });
    });
    ROOT.querySelectorAll("[data-fu-link]").forEach(function(b){
      b.addEventListener("click", function(){
        var fuName=b.getAttribute("data-fu-link"), peName=b.getAttribute("data-pe");
        var pe=((S.peCands&&S.peCands.list)||[]).filter(function(x){return x.name===peName;})[0]||{};
        resolveFu(fuName,"Pending Review",{ payment_entry:peName,
          payment_account:pe.paid_to, payment_ref:pe.reference_no,
          payment_date:pe.posting_date });
      });
    });
    ROOT.querySelectorAll("[data-fu-retag]").forEach(function(s){
      s.addEventListener("change", function(){
        retagFu(s.getAttribute("data-fu-retag"), s.value);
      });
    });
    var pfc=document.getElementById("ix-pf-cancel");
    if(pfc) pfc.addEventListener("click", function(){ S.payForm=null; render(); });
    ROOT.querySelectorAll("[data-pf-save]").forEach(function(b){
      b.addEventListener("click", function(){
        var fuName=b.getAttribute("data-pf-save");
        var ref=(document.getElementById("ix-pf-ref")||{}).value||"";
        var acct=(document.getElementById("ix-pf-acct")||{}).value||"";
        var amt=(document.getElementById("ix-pf-amt")||{}).value||"";
        var dt=(document.getElementById("ix-pf-date")||{}).value||"";
        resolveFu(fuName,"Pending Review",{ payment_ref:ref, payment_account:acct,
          expected_amount:(amt.replace(/[^0-9.]/g,"")||""), payment_date:dt });
      });
    });
  }

  function submitFuForm(){
    if(S.busy) return;
    var cust=(document.getElementById("ix-fuf-cust")||{}).value||"";
    var action=(document.getElementById("ix-fuf-action")||{}).value||"";
    var amt=(document.getElementById("ix-fuf-amt")||{}).value||"";
    var due=(document.getElementById("ix-fuf-due")||{}).value||"";
    if(!action.trim()||!due){ frappe.msgprint("Action and a date are required."); return; }
    var matched=matchCustomer(cust);
    var ftype=(document.getElementById("ix-fuf-type")||{}).value||"Payment Entry";
    var args={ message:S.fuForm.message, action:action, due_date:due,
      followup_type:ftype,
      expected_amount:(amt.replace(/[^0-9.]/g,"")||0) };
    if(matched) args.customer=matched.name; else args.customer_text=cust;
    S.busy="create"; render();
    call(FUP_API+"create_followup", args).then(function(){
      S.busy=""; S.fuForm=null; reloadFollowups();
    }).catch(function(e){
      S.busy=""; render();
      frappe.msgprint("Could not create the follow-up: "+esc((e&&e.message)||""));
    });
  }
  function findPE(fuName){
    if(S.busy) return;
    S.busy="find:"+fuName; render();
    call(FUP_API+"match_payment_entries",{followup:fuName}).then(function(list){
      S.busy=""; S.peCands={ fu:fuName, list:list||[] }; render();
    }).catch(function(e){
      S.busy=""; render();
      frappe.msgprint("Match failed: "+esc((e&&e.message)||""));
    });
  }
  function resolveFu(fuName,status,extra){
    if(S.busy) return;
    S.busy="resolve"; render();
    var args={ followup:fuName, status:status };
    if(extra){ for(var k in extra){
      if(extra[k]!=null && extra[k]!=="") args[k]=extra[k];
    }}
    call(FUP_API+"resolve_followup",args).then(function(){
      S.busy=""; S.peCands=null; S.payForm=null; reloadFollowups();
    }).catch(function(e){
      S.busy=""; render();
      frappe.msgprint("Could not update the follow-up: "+esc((e&&e.message)||""));
    });
  }
  function setIgnored(msgName, val){
    if(!msgName || S.busy) return;
    S.busy="ignore"; render();
    frappe.call({
      method:"frappe.client.set_value",
      args:{ doctype:"VCL Message", name:msgName,
             fieldname:"inbox_ignored", value:val?1:0 },
      callback:function(){
        S.busy="";
        var m=S.msgs.filter(function(x){return x.name===msgName;})[0];
        if(m) m.inbox_ignored=val?1:0;
        deriveConvs(); render();
      },
      error:function(e){
        S.busy=""; render();
        frappe.msgprint("Could not update: "+esc((e&&e.message)||""));
      }
    });
  }

  function retagFu(fuName, newType){
    if(S.busy) return;
    S.busy="retag"; render();
    frappe.call({
      method:"frappe.client.set_value",
      args:{ doctype:"VCL Followup", name:fuName,
             fieldname:"followup_type", value:newType },
      callback:function(){ S.busy=""; reloadFollowups(); },
      error:function(e){
        S.busy=""; render();
        frappe.msgprint("Could not change the type: "+esc((e&&e.message)||""));
      }
    });
  }

  function reloadFollowups(){
    call(FUP_API+"get_followups").then(function(fups){
      S.followups=fups||[];
      S.fuByMsg={};
      S.followups.forEach(function(f){ if(!S.fuByMsg[f.message]) S.fuByMsg[f.message]=f; });
      deriveConvs();
      render();
    }).catch(function(){ render(); });
  }

  /* ---------- guard + boot ---------- */
  if(frappe.session && frappe.session.user==="Guest"){
    ROOT.innerHTML='<div class="ix-empty"><b>Please log in to ERPNext.</b>'
      +'<a href="/login?redirect-to=/vcl-inbox">Log in</a></div>';
    return;
  }
  loadAll();
});
