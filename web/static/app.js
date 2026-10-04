// Ballast demo site
const $ = (id) => document.getElementById(id);
const els = {
  heard: $("heard"), caption: $("caption"), rows: $("rows"), total: $("total"), totalNote: $("total-note"),
  horizon: $("horizon"), tilt: $("tilt"), horizonLabel: $("horizon-label"),
  timeline: $("timeline"), runtime: $("runtime"), form: $("composer"), text: $("text"),
  send: $("send"), mic: $("mic"), voiceOut: $("voice-out"), chips: $("chips"),
  sheet: $("sheet"), backdrop: $("sheet-backdrop"), trades: $("sheet-trades"),
  sheetTitle: $("sheet-title"), approve: $("approve"), decline: $("decline"), reset: $("reset"),
};

const LABELS = {
  us_stocks: "US stocks", intl_stocks: "International stocks", emerging_markets: "Emerging markets",
  bonds: "Bonds", tips: "Inflation-protected bonds", real_estate: "Real estate",
  commodities: "Commodities", cash: "Cash",
};
const label = (c) => LABELS[c] || c.replace(/_/g, " ");
const num = (s) => parseFloat(String(s).replace(/[^0-9.\-]/g, "")) || 0;
const dollars = (s) => "$" + Math.round(num(s)).toLocaleString();

let busy = false;
let speaking = false;
let controller = null;   // lets Stop cancel the request that's waiting for an answer
let openConfirm = null;   // id of the confirmation the user is looking at
let speakReplies = true;

function setState(s) { document.body.dataset.state = s; }

function setCaption(text, kind = "") {
  els.caption.textContent = text;
  els.caption.className = "caption" + (text.length > 90 ? " long" : "") + (kind ? " " + kind : "");
}

// ---------- portfolio + horizon ----------
async function loadPortfolio() {
  try {
    const r = await fetch("/api/portfolio");
    const p = await r.json();
    renderPortfolio(p);
  } catch {
    els.horizonLabel.textContent = "Can't reach the Ballast server.";
  }
}

function renderPortfolio(p) {
  els.runtime.textContent = `${p.model === "offline-rules" ? "Offline model" : p.model} on ${p.broker === "alpaca" ? "Alpaca paper" : "demo portfolio"}`;
  if (p.status !== "ok") {
    els.total.textContent = "—";
    els.horizonLabel.textContent = p.message || "No portfolio data yet.";
    return;
  }
  els.total.textContent = dollars(p.total_value);
  els.totalNote.textContent = p.market_open ? "paper portfolio, market open" : "paper portfolio, market closed";

  const drift = (p.drift || []).slice().sort((a, b) => num(b.target) - num(a.target));
  els.rows.innerHTML = "";
  for (const r of drift) {
    const current = num(r.current), target = num(r.target), pts = num(r.drift_points);
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="row-head">
        <span class="row-name">${label(r.category)}</span>
        <span class="row-nums"><b>${Math.round(current)}%</b> of ${Math.round(target)}%
          <span class="row-drift ${r.over_limit ? "over" : ""}">${pts > 0 ? "+" : ""}${pts.toFixed(1)}</span></span>
      </div>
      <div class="bar" aria-hidden="true">
        <div class="fill ${r.over_limit ? "over" : ""}" style="width:${Math.min(current, 100)}%"></div>
        <div class="target" style="left:${Math.min(target, 100)}%"></div>
      </div>`;
    els.rows.appendChild(li);
  }

  // The horizon tilts with the biggest drift. Level means every category is inside your limit.
  const worst = drift.reduce((a, b) => (Math.abs(num(b.drift_points)) > Math.abs(num(a?.drift_points ?? 0)) ? b : a), null);
  const pts = worst ? num(worst.drift_points) : 0;
  const angle = Math.max(-24, Math.min(24, pts * 2.6));
  els.tilt.style.transform = `rotate(${angle}deg)`;
  els.horizon.classList.toggle("off", !!p.needs_rebalance);
  els.horizon.classList.toggle("level", !p.needs_rebalance);
  els.horizonLabel.textContent = p.needs_rebalance
    ? `Off level by ${Math.abs(pts).toFixed(1)} points`
    : "Level, within your limits";
  els.horizon.setAttribute("aria-label", els.horizonLabel.textContent);
}

// ---------- under the hood ----------
function summarize(step) {
  const o = step.output || {};
  switch (step.tool) {
    case "get_portfolio":
      return o.status === "ok" ? `Worth ${dollars(o.total_value)}, ${o.needs_rebalance ? "over your drift limit" : "within limits"}` : o.status;
    case "plan_rebalance":
      if (o.status === "awaiting_approval")
        return `Proposal ${o.proposal_id}: ` + o.trades.map((t) => `${t.side} ${dollars(t.amount)} ${t.symbol}`).join(", ");
      return o.status === "blocked" ? `Blocked: ${(o.reasons || [])[0] || ""}` : (o.message || o.status);
    case "execute_rebalance":
      return o.status === "executed" ? `Orders filled: ${(o.orders || []).length}` : (o.status || o.message || "error");
    case "get_activity":
      return `${(o.events || []).length} logged events`;
    default:
      return o.status || (step.error ? "error" : "done");
  }
}

function clearEmpty() { const e = els.timeline.querySelector(".empty"); if (e) e.remove(); }

function addTurn(text) {
  clearEmpty();
  const li = document.createElement("li");
  li.className = "turn";
  li.textContent = `“${text}”`;
  els.timeline.appendChild(li);
}

function addStep(step) {
  clearEmpty();
  const li = document.createElement("li");
  li.className = "step" + (step.error ? " err" : "");
  const name = document.createElement("div");
  name.className = "step-name";
  name.textContent = step.tool;
  const sum = document.createElement("div");
  sum.className = "step-sum";
  sum.textContent = summarize(step);
  const det = document.createElement("details");
  det.innerHTML = "<summary>Show request and result</summary>";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify({ input: step.input, output: step.output }, null, 2);
  det.appendChild(pre);
  li.append(name, sum, det);
  els.timeline.appendChild(li);
  li.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function addConfirmation(approved) {
  const li = document.createElement("li");
  li.className = "step you" + (approved ? "" : " no");
  li.innerHTML = `<div class="step-name">${approved ? "You approved" : "You declined"}</div>
    <div class="step-sum">Answered on the confirm card, not by the AI.</div>`;
  els.timeline.appendChild(li);
}

// ---------- confirm sheet ----------
function showSheet(ev) {
  openConfirm = ev.id;
  const pid = (ev.message.match(/BL-[0-9A-F]+/) || [""])[0];
  els.sheetTitle.textContent = pid ? `Confirm paper rebalance ${pid}` : "Confirm paper rebalance";
  els.trades.innerHTML = "";
  for (const t of ev.trades) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="side">${t.side === "sell" ? "Sell" : "Buy"}</span>
      <span class="sym">${t.symbol}</span><span class="amt">${dollars(t.amount)}</span>`;
    els.trades.appendChild(li);
  }
  if (!ev.trades.length) {
    const li = document.createElement("li");
    li.textContent = ev.message;
    els.trades.appendChild(li);
  }
  setCaption("Check the card to place the paper orders, or tell me no.", "waiting");
  els.sheet.hidden = false;
  els.backdrop.hidden = false;
  setState("confirming");
  speak(`Please confirm on screen.`);
  els.approve.focus();
}

function hideSheet() {
  els.sheet.hidden = true;
  els.backdrop.hidden = true;
  openConfirm = null;
}

async function answer(approve) {
  if (!openConfirm) return;
  const id = openConfirm;
  hideSheet();
  setState("thinking");
  await fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ id, approve }) });
}

els.approve.addEventListener("click", () => answer(true));
els.decline.addEventListener("click", () => answer(false));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (openConfirm || busy || speaking) stopEverything();
});

// ---------- a turn ----------
async function send(text) {
  text = text.trim();
  if (!text || busy) return;
  if (openConfirm) {           // talking while the card is open answers the card
    if (/\b(yes|yeah|sure|go ahead|do it|confirm|place)\b/i.test(text)) return answer(true);
    if (/\b(no|cancel|don't|stop)\b/i.test(text)) return answer(false);
  }
  busy = true;
  controller = new AbortController();
  updateSendButton();
  els.text.value = "";
  els.heard.classList.remove("placeholder");
  els.heard.textContent = text;
  setCaption("…");
  setState("thinking");
  addTurn(text);

  try {
    let r;
    for (let attempt = 0; ; attempt++) {   // after a Stop, the last turn may still be wrapping up
      r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ text }), signal: controller.signal });
      if (r.status !== 409 || attempt >= 20) break;
      await new Promise((ok) => setTimeout(ok, 400));
    }
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `Server error ${r.status}`);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let i;
      while ((i = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, i); buffer = buffer.slice(i + 2);
        if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      setCaption("Stopped. Nothing new was traded.");
    } else {
      setCaption(err.message, "error");
    }
    setState("idle");
  } finally {
    busy = false;
    controller = null;
    updateSendButton();
  }
}

// ---------- stop ----------
function updateSendButton() {
  const stopping = busy || speaking;
  els.send.textContent = stopping ? "Stop" : "Send";
  els.send.classList.toggle("stop", stopping);
  els.send.setAttribute("aria-label", stopping ? "Stop Ballast" : "Send");
}

function stopEverything() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  speaking = false;
  if (openConfirm) answer(false);           // an open card counts as "no"
  if (controller) controller.abort();       // stop waiting for the answer
  setState("idle");
  updateSendButton();
}

function handle(ev) {
  if (ev.type === "tool") {
    addStep(ev);
    if (["execute_rebalance", "save_target_allocation"].includes(ev.tool)) loadPortfolio();
  } else if (ev.type === "confirm") {
    showSheet(ev);
  } else if (ev.type === "confirmed") {
    if (openConfirm === ev.id) hideSheet();
    addConfirmation(ev.approve);
    setState("thinking");
  } else if (ev.type === "reply") {
    setCaption(ev.text, ev.error ? "error" : "");
    loadPortfolio();
    speak(ev.text);
  }
}

// ---------- voice ----------
// Feminine English voices, best first. "Natural" voices (Edge on Windows) sound the most human.
const PREFERRED_VOICES = [
  /aria.*natural/i, /jenny.*natural/i, /ava.*natural/i, /emma.*natural/i, /michelle.*natural/i,
  /google us english/i, /samantha/i, /\bava\b/i, /allison/i, /zira/i, /aria/i, /jenny/i, /female/i,
];
const voicePick = $("voice-pick"), speedPick = $("speed-pick");
let voice = null;

function loadVoices() {
  if (!("speechSynthesis" in window)) { voicePick.innerHTML = "<option>Not supported</option>"; return; }
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.toLowerCase().startsWith("en"));
  if (!voices.length) return;                       // Chrome fills the list a moment later
  const saved = localStorage.getItem("ballast.voice");
  voice = voices.find((v) => v.name === saved)
    || PREFERRED_VOICES.map((re) => voices.find((v) => re.test(v.name))).find(Boolean)
    || voices[0];
  voicePick.innerHTML = "";
  for (const v of voices) {
    const o = document.createElement("option");
    o.value = v.name;
    o.textContent = v.name.replace(/^Microsoft /, "").replace(/ Online \(Natural\)/, " (natural)");
    o.selected = v === voice;
    voicePick.appendChild(o);
  }
}
if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", loadVoices);
loadVoices();
setTimeout(() => { if (!voice) voicePick.innerHTML = "<option>Browser default</option>"; }, 2500);

voicePick.addEventListener("change", () => {
  voice = speechSynthesis.getVoices().find((v) => v.name === voicePick.value) || voice;
  try { localStorage.setItem("ballast.voice", voicePick.value); } catch {}
  speak("Hi, I'm Ballast. This is how I'll sound.");
});
try { speedPick.value = localStorage.getItem("ballast.speed") || speedPick.value; } catch {}
speedPick.addEventListener("change", () => {
  try { localStorage.setItem("ballast.speed", speedPick.value); } catch {}
  speak("This is my new speed.");
});

function speak(text) {
  if (!speakReplies || !("speechSynthesis" in window)) { setState("idle"); return; }
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  if (voice) { u.voice = voice; u.lang = voice.lang; }
  u.rate = parseFloat(speedPick.value) || 1.3;
  u.pitch = 1.05;
  u.onstart = () => { speaking = true; updateSendButton(); setState(openConfirm ? "confirming" : "speaking"); };
  u.onend = u.onerror = () => {
    speaking = false;
    updateSendButton();
    setState(openConfirm ? "confirming" : busy ? "thinking" : "idle");
  };
  speechSynthesis.speak(u);
}

els.voiceOut.addEventListener("click", () => {
  speakReplies = !speakReplies;
  els.voiceOut.setAttribute("aria-pressed", String(speakReplies));
  if (!speakReplies && "speechSynthesis" in window) speechSynthesis.cancel();
});

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Recognition) {
  const rec = new Recognition();
  rec.lang = "en-US";
  rec.interimResults = true;
  let finalText = "";
  rec.onstart = () => { finalText = ""; setState("listening"); els.heard.classList.remove("placeholder"); els.heard.textContent = "Listening…"; };
  rec.onresult = (e) => {
    let interim = "";
    for (const r of e.results) (r.isFinal ? (finalText = r[0].transcript) : (interim += r[0].transcript));
    els.heard.textContent = finalText || interim;
  };
  rec.onerror = () => setState("idle");
  rec.onend = () => { if (finalText) send(finalText); else setState(openConfirm ? "confirming" : "idle"); };
  els.mic.addEventListener("click", () => { if (!busy || openConfirm) { speechSynthesis?.cancel(); rec.start(); } });
} else {
  els.mic.disabled = true;
  els.mic.title = "Voice input needs Chrome or Edge. You can type instead.";
}

// ---------- wiring ----------
els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (busy || speaking) stopEverything();
  else send(els.text.value);
});
els.chips.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) send(b.dataset.say); });
els.reset.addEventListener("click", async () => {
  const r = await fetch("/api/reset", { method: "POST" });
  if (!r.ok) { const e = await r.json(); setCaption(e.error, "error"); return; }
  els.timeline.innerHTML = '<li class="empty">Nothing yet. Ask a question and each tool call will appear here.</li>';
  els.heard.classList.add("placeholder");
  els.heard.textContent = "Demo portfolio reset.";
  setCaption("Back to the starting portfolio. Ask how it's doing.");
  loadPortfolio();
});

els.heard.classList.add("placeholder");
loadPortfolio();
