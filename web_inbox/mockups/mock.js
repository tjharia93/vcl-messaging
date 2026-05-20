/* VCL Inbox mockup — shared interactivity + right-panel context.
   Static design comp; sample data baked in. */
(function () {
  "use strict";

  var CTX = {
    m1: { pri:"HIGH", cat:"payment", catLabel:"payment",
      read:"Goshrani Printers cleared cheque 004521 for KES 250,000 against INV-2026-0312, banked today.",
      payment:{ amount:"KES 250,000", instrument:"Cheque 004521", settles:"INV-2026-0312" },
      customers:[{ name:"Goshrani Printers", matched:true }],
      actions:["Confirm receipt against INV-2026-0312 in the ledger"],
      meta:{ group:"VCL Accounts", sender:"Raheel", when:"19 May · 15:48", type:"text" } },
    m2: { pri:"HIGH", cat:"payment", catLabel:"payment",
      read:"M-Pesa SGH4TR8K9P: KES 18,500 received from East Africa Cable Ltd for bond paper.",
      payment:{ amount:"KES 18,500", instrument:"M-Pesa SGH4TR8K9P", settles:"bond paper order" },
      customers:[{ name:"East Africa Cable Ltd", matched:true }],
      actions:["Match M-Pesa code SGH4TR8K9P to the open invoice"],
      meta:{ group:"VCL Accounts", sender:"Neema", when:"19 May · 15:49", type:"text" } },
    m3: { pri:"HIGH", cat:"payment", catLabel:"payment",
      read:"RTGS payment of KES 1,240,000 to Korab International for newsprint shipment; slip pending.",
      payment:{ amount:"KES 1,240,000", instrument:"RTGS", settles:"newsprint shipment" },
      customers:[{ name:"Korab International", matched:false }],
      actions:["Attach the RTGS slip when shared","Korab is a supplier — log against the import PO"],
      meta:{ group:"VCL Accounts", sender:"Jeetu", when:"19 May · 15:50", type:"text" } },
    m4: { pri:"LOW", cat:"personal", catLabel:"personal",
      read:"Morning greeting with well-wishes to the team.",
      customers:[], actions:[],
      meta:{ group:"VCL Accounts", sender:"Simon", when:"19 May · 15:51", type:"text" } },
    m5: { pri:"HIGH", cat:"order", catLabel:"order",
      read:"Malindi Books confirmed order: 20 cartons computer paper 2-part payslip W/Y, delivery next week.",
      customers:[{ name:"Malindi Books", matched:true }],
      actions:["Raise a Sales Order — 20 ctn 2-part payslip W/Y","Schedule delivery for next week"],
      meta:{ group:"VCL Accounts", sender:"Vimal", when:"19 May · 15:52", type:"text" } },
    m6: { pri:"MED", cat:"image", catLabel:"label_artwork",
      read:"Avery Dennison self-adhesive label roll specification — 80gsm, 100mm width, quantity 5 rolls.",
      customers:[], actions:["File against the label job spec"],
      meta:{ group:"Cartons", sender:"Nyaata Keenda", when:"19 May · 09:48", type:"image" } },
    m7: { pri:"MED", cat:"job", catLabel:"job update",
      read:"Instruction to allocate Mombasa kraft material for the next carton job.",
      customers:[], actions:["Tell the board allocator to use Mombasa kraft"],
      meta:{ group:"Cartons", sender:"vimal", when:"19 May · 09:48", type:"text" } },
    m8: { pri:"MED", cat:"sales", catLabel:"sales status",
      read:"Weekly sales pipeline post — 30 live inquiries across 9 reps (open, follow-up, lost).",
      customers:[{ name:"Nation Media", matched:true },{ name:"East Africa Cable", matched:true },{ name:"J&B Printers", matched:false }],
      actions:["Extract per-customer rows into the pipeline board (Tier 2)"],
      meta:{ group:"VCL · Sales Management", sender:"vimal", when:"19 May · 10:19", type:"text" } },
    m9: { pri:"HIGH", cat:"order", catLabel:"order",
      read:"Live order — Smart Printer FBB sheeting, 82x54cm from 82cm reels, 3 whole reels at KES 138+VAT/kg. LPO PO-VIMIT-2026-3529.",
      customers:[{ name:"Smart Printer", matched:true }],
      actions:["Raise Sales Order + Job Card","Route LPO PO-VIMIT-2026-3529 to LPO Intake","Urgent — sheeting starts tomorrow"],
      meta:{ group:"VCL · Sales Management", sender:"vimal", when:"19 May · 15:53", type:"document" } },
  };

  function esc(s){ return (s==null?"":String(s))
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

  function renderCtx(id){
    var c = CTX[id];
    var body = document.getElementById("ix-ctx-body");
    if(!c || !body) return;
    var h = "";

    h += '<div class="ix-c-card ix-c-read">'
       + '<div class="ix-c-label">Claude read</div>'
       + '<div class="ix-c-readtext">'+esc(c.read)+'</div>'
       + '<div class="ix-c-flags">'
       +   '<span class="ix-pri p-'+c.pri+'">'+c.pri+'</span>'
       +   '<span class="ix-cat c-'+c.cat+'">'+esc(c.catLabel)+'</span>'
       + '</div></div>';

    if(c.payment){
      h += '<div class="ix-c-card ix-c-pay"><div class="ix-c-label">Payment detected</div>'
         + '<div class="ix-c-paygrid">'
         +   payRow("Amount", c.payment.amount)
         +   payRow("Instrument", c.payment.instrument)
         +   payRow("Settles", c.payment.settles)
         + '</div></div>';
    }

    h += '<div class="ix-c-card"><div class="ix-c-label">Matched to ERPNext</div>';
    if(c.customers && c.customers.length){
      h += '<div class="ix-c-matches">';
      c.customers.forEach(function(m){
        h += '<span class="ix-match '+(m.matched?"hit":"miss")+'">'
           + '<span class="ix-match-dot"></span>'+esc(m.name)
           + '<span class="ix-match-tag">'+(m.matched?"Customer":"not in master")+'</span></span>';
      });
      h += '</div>';
    } else {
      h += '<div class="ix-c-empty">No company names detected.</div>';
    }
    h += '</div>';

    if(c.actions && c.actions.length){
      h += '<div class="ix-c-card"><div class="ix-c-label">Suggested actions</div><ul class="ix-c-actions">';
      c.actions.forEach(function(a){ h += '<li>'+esc(a)+'</li>'; });
      h += '</ul></div>';
    }

    h += '<div class="ix-c-card ix-c-meta"><div class="ix-c-label">Source</div>'
       + metaRow("Group", c.meta.group)
       + metaRow("Sender", c.meta.sender)
       + metaRow("Received", c.meta.when)
       + metaRow("Type", c.meta.type)
       + metaRow("Channel", "WhatsApp PA")
       + '</div>';

    body.innerHTML = h;
  }
  function payRow(k,v){ return '<div class="ix-pf"><span class="ix-pf-k">'+esc(k)
    +'</span><span class="ix-pf-v">'+esc(v)+'</span></div>'; }
  function metaRow(k,v){ return '<div class="ix-mr"><span class="ix-mr-k">'+esc(k)
    +'</span><span class="ix-mr-v">'+esc(v)+'</span></div>'; }

  function ready(fn){
    if(document.readyState!=="loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function(){
    var root = document.querySelector(".ix-app");
    if(!root) return;

    /* conversation switching */
    root.querySelectorAll(".ix-conv").forEach(function(btn){
      btn.addEventListener("click", function(){
        var cv = btn.getAttribute("data-conv");
        root.querySelectorAll(".ix-conv").forEach(function(b){ b.classList.toggle("on", b===btn); });
        root.querySelectorAll(".ix-pane").forEach(function(p){
          p.hidden = (p.getAttribute("data-conv")!==cv);
        });
        var pane = root.querySelector('.ix-pane[data-conv="'+cv+'"]');
        var first = pane && pane.querySelector(".ix-msg");
        if(first) selectMsg(pane, first);
      });
    });

    /* message selection */
    function selectMsg(pane, msg){
      pane.querySelectorAll(".ix-msg").forEach(function(m){ m.classList.toggle("on", m===msg); });
      renderCtx(msg.getAttribute("data-msg"));
    }
    root.querySelectorAll(".ix-pane").forEach(function(pane){
      pane.querySelectorAll(".ix-msg").forEach(function(msg){
        msg.addEventListener("click", function(){ selectMsg(pane, msg); });
      });
    });

    /* filter tabs */
    root.querySelectorAll(".ix-tab").forEach(function(tab){
      tab.addEventListener("click", function(){
        var f = tab.getAttribute("data-filter");
        root.querySelectorAll(".ix-tab").forEach(function(t){ t.classList.toggle("on", t===tab); });
        root.querySelectorAll(".ix-conv").forEach(function(cv){
          var tags = (cv.getAttribute("data-tags")||"").split(" ");
          cv.style.display = tags.indexOf(f)>=0 ? "" : "none";
        });
      });
    });

    /* initial context */
    renderCtx("m1");
  });
})();
