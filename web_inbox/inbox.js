/* =============================================================
   VCL Inbox — Option A · Daylight — live data, responsive
   Runs as the `javascript` field of the /vcl-inbox Web Page.
   3-pane respond.io-style inbox over VCL Message / Conversation.
   ============================================================= */
frappe.ready(function () {
  "use strict";

  var ROOT = document.getElementById("vcl-inbox-root");
  if (!ROOT) return;

  var MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var PRANK = { CRIT:3, HIGH:2, MED:1, LOW:0 };
  var FULL = ["name","conversation","sender_name","message_type","content","sent_at",
    "creation","ai_priority","ai_category","ai_kind","ai_summary","ai_customer_mentions",
    "ai_action_items","ai_mentions_tanuj","media_url","media_mime_type","direction"];
  var SAFE = ["name","conversation","sender_name","message_type","content","sent_at",
    "creation","ai_kind","ai_summary","media_url","media_mime_type","direction"];

  var S = { convs:[], msgs:[], byConv:{}, custs:[], custNorm:[],
            activeConv:null, activeMsg:null, filter:"all",
            view:"list", ctxOpen:false, tier1Missing:false };

  /* ---------- helpers ---------- */
  function esc(s){ return (s==null?"":String(s))
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

  function fmtTime(s){
    if(!s) return "";
    var d = new Date(String(s).replace(" ","T"));
    if(isNaN(d.getTime())) return esc(s);
    var p=function(n){return(n<10?"0":"")+n;};
    return d.getDate()+" "+MONTHS[d.getMonth()]+" "+p(d.getHours())+":"+p(d.getMinutes());
  }

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
    var m = normName(mention);
    if(!m || m.length<3) return null;
    var i, cn;
    for(i=0;i<S.custNorm.length;i++){ if(S.custNorm[i]===m) return S.custs[i]; }
    for(i=0;i<S.custNorm.length;i++){
      cn=S.custNorm[i];
      if(cn && cn.length>=4 && (cn.indexOf(m)>=0||m.indexOf(cn)>=0)) return S.custs[i];
    }
    var mt=m.split(" ").filter(function(t){return t.length>=4;});
    if(mt.length){
      for(i=0;i<S.custNorm.length;i++){
        cn=S.custNorm[i]; if(!cn) continue;
        var ct=cn.split(" ");
        if(mt.some(function(t){return ct.indexOf(t)>=0;})) return S.custs[i];
      }
    }
    return null;
  }

  function extractPayment(text){
    var o={}; if(!text) return o;
    var am=text.match(/(?:KES|KSh|Ksh|Kshs|Sh)\s*\.?\s*([\d,]+(?:\.\d+)?)/)
        || text.match(/\b([\d,]{4,}(?:\.\d+)?)\s*(?:\/=|\/-)/);
    if(am) o.amount="KES "+am[1].replace(/,/g,"");
    var chq=text.match(/che?que\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{3,})/i);
    if(chq) o.instrument="Cheque "+chq[1];
    if(!o.instrument){
      var mp=text.match(/\b([A-Z0-9]{10})\b/);
      if(mp && /[A-Z]/.test(mp[1]) && /[0-9]/.test(mp[1])) o.instrument="M-Pesa "+mp[1];
    }
    if(!o.instrument && /\brtgs\b/i.test(text)) o.instrument="RTGS";
    if(!o.instrument && /pesalink/i.test(text)) o.instrument="Pesalink";
    var ref=text.match(/\b(INV[-\/ ]?[A-Za-z0-9\-]+|PO[-\/ ]?[A-Za-z0-9\-]+)\b/i);
    if(ref) o.ref=ref[1];
    return o;
  }

  function getList(doctype, fields, opts){
    opts=opts||{};
    return new Promise(function(res,rej){
      frappe.call({ method:"frappe.client.get_list",
        args:{ doctype:doctype, fields:fields, limit_page_length:opts.limit||0,
               order_by:opts.order_by||"creation desc", filters:opts.filters||[] },
        callback:function(r){ res((r&&r.message)||[]); },
        error:function(e){ rej(e); } });
    });
  }

  /* ---------- load ---------- */
  function loadAll(){
    ROOT.innerHTML = '<div class="ix-boot">Loading VCL Inbox…</div>';
    var convP = getList("VCL Conversation",
      ["name","whatsapp_group_name","whatsapp_group_id","channel"], {limit:0});
    var custP = getList("Customer", ["name","customer_name"],
      {limit:0, order_by:"customer_name asc"}).catch(function(){ return []; });
    var msgP = getList("VCL Message", FULL, {order_by:"creation asc"})
      .catch(function(){
        S.tier1Missing = true;
        return getList("VCL Message", SAFE, {order_by:"creation asc"});
      });

    Promise.all([convP,custP,msgP]).then(function(r){
      var convs=r[0], custs=r[1], msgs=r[2];
      S.custs=custs;
      S.custNorm=custs.map(function(c){ return normName(c.customer_name||c.name); });
      var cmap={}; convs.forEach(function(c){ cmap[c.name]=c; });
      S.convs=convs; S.msgs=msgs; S.byConv={};
      msgs.forEach(function(m){
        (S.byConv[m.conversation]=S.byConv[m.conversation]||[]).push(m);
      });
      // derive per-conversation summary
      S.convList = convs.map(function(c){
        var ms = S.byConv[c.name] || [];
        var last = ms[ms.length-1];
        var rank = 0, hasPay=false, attn=0;
        ms.forEach(function(m){
          var pr = PRANK[m.ai_priority]||0;
          if(pr>rank) rank=pr;
          if(m.ai_category==="payment") hasPay=true;
          if(m.ai_priority==="HIGH"||m.ai_priority==="CRIT"||m.ai_mentions_tanuj) attn++;
        });
        return { conv:c, msgs:ms, last:last, rank:rank, hasPay:hasPay, attn:attn,
                 lastTime:(last&&(last.sent_at||last.creation))||"" };
      }).filter(function(x){ return x.msgs.length>0; });
      S.convList.sort(function(a,b){ return (b.lastTime<a.lastTime?-1:b.lastTime>a.lastTime?1:0); });

      if(!S.activeConv && S.convList.length) S.activeConv = S.convList[0].conv.name;
      pickActiveMsg();
      render();
    }).catch(function(e){
      ROOT.innerHTML = '<div class="ix-empty"><b>Could not load the inbox.</b>'
        + esc((e&&e.message)||"Are you logged in to ERPNext?")+'</div>';
    });
  }

  function pickActiveMsg(){
    var ms = S.byConv[S.activeConv] || [];
    if(!ms.length){ S.activeMsg=null; return; }
    if(S.activeMsg && ms.some(function(m){return m.name===S.activeMsg;})) return;
    S.activeMsg = ms[ms.length-1].name;
  }

  /* ---------- derive ---------- */
  function priLabel(m){ return m.ai_priority || ""; }
  function catClass(c){ return c ? "c-"+c : ""; }

  function rankName(r){ return r>=3?"CRIT":r>=2?"HIGH":r>=1?"MED":"LOW"; }

  function visibleConvs(){
    return S.convList.filter(function(x){
      if(S.filter==="all") return true;
      if(S.filter==="pay") return x.hasPay;
      if(S.filter==="high") return x.attn>0;
      return true;
    });
  }

  /* ---------- render ---------- */
  function render(){
    var totals = { msgs:S.msgs.length, pay:0, attn:0, unmatched:0 };
    S.msgs.forEach(function(m){
      if(m.ai_category==="payment") totals.pay++;
      if(m.ai_priority==="HIGH"||m.ai_priority==="CRIT"||m.ai_mentions_tanuj) totals.attn++;
    });
    var seen={};
    S.msgs.forEach(function(m){
      parseArr(m.ai_customer_mentions).forEach(function(nm){
        var k=normName(nm); if(!k||seen[k]) return; seen[k]=1;
        if(!matchCustomer(nm)) totals.unmatched++;
      });
    });

    var h = '';
    h += '<header class="ix-top">'
       + '<div class="ix-brand">VCL<span>INBOX</span></div>'
       + '<div class="ix-stats">'
       +   stat(totals.msgs,"Messages","keep-xs")
       +   stat(totals.pay,"Payments","")
       +   stat(totals.attn,"Attention","warn hide-sm")
       +   stat(totals.unmatched,"Unmatched","miss hide-sm")
       + '</div>'
       + '<button class="ix-refresh" id="ix-refresh">refresh</button>'
       + '</header>';

    h += '<div class="ix-main">'
       + renderRail() + renderThread() + renderCtx() + '</div>';

    ROOT.className = "ix-app" + (S.ctxOpen?" ctx-open":"");
    ROOT.setAttribute("data-view", S.view);
    ROOT.innerHTML = h;
    wire();
  }

  function stat(n,label,cls){
    return '<div class="ix-stat '+cls+'"><b>'+n+'</b><i>'+esc(label)+'</i></div>';
  }

  function renderRail(){
    var h = '<aside class="ix-rail"><div class="ix-rail-hd">'
      + '<div class="ix-search"><input placeholder="Search conversations" disabled></div>'
      + '<div class="ix-tabs">'
      +   tab("all","All") + tab("pay","Payments") + tab("high","High")
      + '</div></div>';
    if(S.tier1Missing){
      h += '<div class="ix-banner">Tier 1 fields not deployed — showing raw '
         + 'messages. Update the <code>vcl-messaging</code> app.</div>';
    }
    h += '<div class="ix-convs">';
    var vis = visibleConvs();
    if(!vis.length){
      h += '<div class="ix-empty">No conversations match this filter.</div>';
    } else {
      vis.forEach(function(x){
        var c=x.conv, last=x.last;
        var prev = (last && (last.ai_summary || last.content)) ||
                   (last? "["+(last.message_type||"media")+"]" : "");
        var who = last? (last.sender_name||"?")+" — " : "";
        h += '<button class="ix-conv'+(c.name===S.activeConv?" on":"")+'" data-conv="'+esc(c.name)+'">'
           + '<span class="ix-conv-dot p-'+rankName(x.rank)+'"></span>'
           + '<span class="ix-conv-body">'
           +   '<span class="ix-conv-top"><span class="ix-conv-name">'
           +     esc(c.whatsapp_group_name||c.name)+'</span>'
           +     '<span class="ix-conv-time">'+fmtTime(x.lastTime)+'</span></span>'
           +   '<span class="ix-conv-prev">'+esc(who+prev)+'</span>'
           +   '<span class="ix-conv-meta"><span class="ix-wa">WhatsApp</span>'
           +     (x.hasPay?'<span class="ix-conv-pay">payments</span>':'')+'</span>'
           + '</span>'
           + '<span class="ix-conv-badge">'+x.msgs.length+'</span>'
           + '</button>';
      });
    }
    h += '</div></aside>';
    return h;
  }
  function tab(f,label){
    return '<button class="ix-tab'+(S.filter===f?" on":"")+'" data-filter="'+f+'">'
      +esc(label)+'</button>';
  }

  function renderThread(){
    var x = S.convList.filter(function(y){return y.conv.name===S.activeConv;})[0];
    var h = '<section class="ix-thread"><div class="ix-pane">';
    if(!x){
      h += '<div class="ix-empty">Select a conversation.</div></div></section>';
      return h;
    }
    var c=x.conv;
    h += '<header class="ix-thread-hd">'
       + '<button class="ix-back" id="ix-back" title="Back">&#8592;</button>'
       + '<div><span class="ix-th-name">'+esc(c.whatsapp_group_name||c.name)+'</span>'
       +   '<span class="ix-th-sub">WhatsApp group · '+x.msgs.length+' messages'
       +   (x.hasPay?' · payments':'')+'</span></div></header>';
    h += '<div class="ix-msgs">';
    x.msgs.forEach(function(m){ h += msgBubble(m); });
    h += '</div>';
    h += '<footer class="ix-composer">'
       + '<input class="ix-compose-in" placeholder="Reply to '+esc(c.whatsapp_group_name||"")+'" disabled>'
       + '<button class="ix-send" disabled>Send</button>'
       + '<span class="ix-compose-note">Reply path ships next</span></footer>';
    h += '</div></section>';
    return h;
  }

  function msgBubble(m){
    var isText=(m.message_type||"text")==="text";
    var h='<article class="ix-msg'+(m.name===S.activeMsg?" on":"")+'" data-msg="'+esc(m.name)+'">';
    h+='<div class="ix-msg-meta"><span class="ix-from">'+esc(m.sender_name||"?")
      +'</span><span class="ix-time">'+fmtTime(m.sent_at||m.creation)+'</span></div>';
    if(!isText){
      if(m.message_type==="image" && m.media_url && /^\/(private|files)/.test(m.media_url)){
        h+='<div class="ix-thumb"><img src="'+esc(m.media_url)+'" alt="image" loading="lazy"></div>';
      } else {
        h+='<div class="ix-media"><span class="tag">'+esc((m.message_type||"media").toUpperCase())
          +'</span><span>'+esc(m.content||m.media_mime_type||"")+'</span></div>';
      }
    }
    if(m.content && isText){
      var long=m.content.length>600;
      h+='<div class="ix-bubble'+(long?" clip":"")+'">'+esc(m.content)+'</div>';
    } else if(!m.content && isText){
      h+='<div class="ix-bubble" style="color:var(--muted);font-style:italic">(empty)</div>';
    }
    // AI chip row
    var chips='';
    var pri=priLabel(m);
    if(pri) chips+='<span class="ix-pri p-'+esc(pri)+'">'+esc(pri)+'</span>';
    if(m.ai_category) chips+='<span class="ix-cat '+catClass(m.ai_category)+'">'
      +esc(m.ai_category.replace(/_/g," "))+'</span>';
    else if(isText) chips+='<span class="ix-cat c-pending">unclassified</span>';
    if(!isText && m.ai_kind) chips+='<span class="ix-cat">'+esc(m.ai_kind)+'</span>';
    if(m.ai_mentions_tanuj) chips+='<span class="ix-pri p-CRIT">@ TANUJ</span>';
    if(m.ai_summary) chips+='<span class="ix-ai-sum">'+esc(m.ai_summary)+'</span>';
    if(chips) h+='<div class="ix-ai">'+chips+'</div>';
    h+='</article>';
    return h;
  }

  function renderCtx(){
    var h='<aside class="ix-ctx"><div class="ix-ctx-hd"><span>Message intelligence</span>'
      +'<button class="ix-ctx-close" id="ix-ctx-close" title="Close">&#10005;</button></div>'
      +'<div id="ix-ctx-body">';
    var m = S.msgs.filter(function(x){return x.name===S.activeMsg;})[0];
    if(!m){
      h+='<div class="ix-empty">Select a message to see its intelligence.</div>';
    } else {
      h+=ctxBody(m);
    }
    h+='</div></aside>';
    return h;
  }

  function ctxBody(m){
    var h='';
    var isText=(m.message_type||"text")==="text";
    var classified=!!(m.ai_priority||m.ai_category||m.ai_kind);

    /* claude read */
    h+='<div class="ix-c-card ix-c-read"><div class="ix-c-label">Claude read</div>';
    if(m.ai_summary){
      h+='<div class="ix-c-readtext">'+esc(m.ai_summary)+'</div>';
    } else {
      h+='<div class="ix-c-readtext pending">Awaiting classification — run the '
        +'backfill, or this message predates the classifier.</div>';
    }
    var flags='';
    if(m.ai_priority) flags+='<span class="ix-pri p-'+esc(m.ai_priority)+'">'
      +esc(m.ai_priority)+'</span>';
    if(m.ai_category) flags+='<span class="ix-cat '+catClass(m.ai_category)+'">'
      +esc(m.ai_category.replace(/_/g," "))+'</span>';
    if(!isText && m.ai_kind) flags+='<span class="ix-cat">'+esc(m.ai_kind)+'</span>';
    if(flags) h+='<div class="ix-c-flags">'+flags+'</div>';
    h+='</div>';

    /* original message */
    h+='<div class="ix-c-card"><div class="ix-c-label">Original message</div>';
    if(m.content){
      h+='<div class="ix-c-orig">'+esc(m.content)+'</div>';
    } else if(!isText){
      h+='<div class="ix-c-orig" style="color:var(--muted)">['+esc(m.message_type)
        +(m.media_mime_type?" · "+esc(m.media_mime_type):"")+']</div>';
    } else {
      h+='<div class="ix-c-orig" style="color:var(--muted)">(empty)</div>';
    }
    h+='</div>';

    /* payment */
    if(m.ai_category==="payment"){
      var pay=extractPayment((m.ai_summary||"")+" "+(m.content||""));
      var pf='';
      if(pay.amount) pf+=pfRow("Amount",pay.amount);
      if(pay.instrument) pf+=pfRow("Instrument",pay.instrument);
      if(pay.ref) pf+=pfRow("Settles",pay.ref);
      if(pf) h+='<div class="ix-c-card ix-c-pay"><div class="ix-c-label">Payment detected</div>'
        +'<div class="ix-c-paygrid">'+pf+'</div></div>';
    }

    /* customer match */
    var mentions=parseArr(m.ai_customer_mentions);
    h+='<div class="ix-c-card"><div class="ix-c-label">Matched to ERPNext</div>';
    if(mentions.length){
      h+='<div class="ix-c-matches">';
      mentions.forEach(function(nm){
        var c=matchCustomer(nm);
        if(c){
          h+='<a class="ix-match hit" target="_blank" href="/app/customer/'
            +encodeURIComponent(c.name)+'"><span class="ix-match-dot"></span>'
            +'<span class="ix-match-nm">'+esc(c.customer_name||c.name)+'</span>'
            +'<span class="ix-match-tag">Customer</span></a>';
        } else {
          h+='<span class="ix-match miss"><span class="ix-match-dot"></span>'
            +'<span class="ix-match-nm">'+esc(nm)+'</span>'
            +'<span class="ix-match-tag">not in master</span></span>';
        }
      });
      h+='</div>';
    } else {
      h+='<div class="ix-c-empty">No company names detected.</div>';
    }
    h+='</div>';

    /* action items */
    var acts=parseArr(m.ai_action_items);
    if(acts.length){
      h+='<div class="ix-c-card"><div class="ix-c-label">Suggested actions</div>'
        +'<ul class="ix-c-actions">';
      acts.forEach(function(a){ h+='<li>'+esc(a)+'</li>'; });
      h+='</ul></div>';
    }

    /* source */
    var conv=S.convs.filter(function(c){return c.name===m.conversation;})[0]||{};
    h+='<div class="ix-c-card"><div class="ix-c-label">Source</div>'
      +mrRow("Group",conv.whatsapp_group_name||m.conversation)
      +mrRow("Sender",m.sender_name||"?")
      +mrRow("Received",fmtTime(m.sent_at||m.creation))
      +mrRow("Type",m.message_type||"text")
      +mrRow("Channel","WhatsApp PA")
      +mrRow("Record",m.name)
      +'</div>';
    return h;
  }
  function pfRow(k,v){ return '<div class="ix-pf"><span class="ix-pf-k">'+esc(k)
    +'</span><span class="ix-pf-v">'+esc(v)+'</span></div>'; }
  function mrRow(k,v){ return '<div class="ix-mr"><span class="ix-mr-k">'+esc(k)
    +'</span><span class="ix-mr-v">'+esc(v)+'</span></div>'; }

  /* ---------- events ---------- */
  function wire(){
    var rf=document.getElementById("ix-refresh");
    if(rf) rf.addEventListener("click", loadAll);

    ROOT.querySelectorAll(".ix-tab").forEach(function(t){
      t.addEventListener("click", function(){
        S.filter=t.getAttribute("data-filter"); render();
      });
    });
    ROOT.querySelectorAll(".ix-conv").forEach(function(b){
      b.addEventListener("click", function(){
        S.activeConv=b.getAttribute("data-conv");
        S.activeMsg=null; pickActiveMsg();
        S.view="thread";
        render();
      });
    });
    ROOT.querySelectorAll(".ix-msg").forEach(function(a){
      a.addEventListener("click", function(){
        S.activeMsg=a.getAttribute("data-msg");
        S.ctxOpen=true;
        render();
      });
    });
    var bk=document.getElementById("ix-back");
    if(bk) bk.addEventListener("click", function(){ S.view="list"; render(); });
    var cc=document.getElementById("ix-ctx-close");
    if(cc) cc.addEventListener("click", function(){ S.ctxOpen=false; render(); });
  }

  /* ---------- guard + boot ---------- */
  if(frappe.session && frappe.session.user==="Guest"){
    ROOT.innerHTML='<div class="ix-empty"><b>Please log in to ERPNext.</b>'
      +'<a href="/login?redirect-to=/vcl-inbox">Log in</a></div>';
    return;
  }
  loadAll();
});
