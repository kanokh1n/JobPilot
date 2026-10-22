// Inline stage and note edits, and a hint when the workers wrote something new.
(function () {
  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); }
    if (!r.ok) throw new Error(await r.text());
    document.dispatchEvent(new Event("jp-edited"));
    return r.json();
  }
  function flash(el, ok) {
    el.classList.remove("saved", "failed");
    el.classList.add(ok ? "saved" : "failed");
    if (ok) setTimeout(() => el.classList.remove("saved"), 1200);
  }
  function rowId(el) { return el.closest("[data-id]").dataset.id; }

  document.querySelectorAll("select.stage").forEach((sel) => {
    sel.addEventListener("change", async () => {
      let body;
      if (sel.value === "__auto__") body = { auto: true };
      else if (sel.value === "__custom__") {
        const text = (prompt("Свой этап:") || "").trim();
        if (!text) { sel.value = sel.dataset.current; return; }
        body = { stage: text };
      } else body = { stage: sel.value };
      try {
        const row = await post(`/api/applications/${rowId(sel)}/stage`, body);
        if (body.auto || sel.value === "__custom__") { location.reload(); return; }
        sel.dataset.current = row.stage;
        sel.className = "stage s-" + row.stage;
        for (const o of sel.options) o.textContent = o.textContent.replace(" ✎", "");
        sel.selectedOptions[0].textContent += " ✎";
        if (!("byHand" in sel.dataset) && ![...sel.options].some((o) => o.value === "__auto__")) sel.add(new Option("↺ вернуть автомат", "__auto__"));
        flash(sel, true);
      } catch (e) { sel.value = sel.dataset.current; flash(sel, false); }
    });
  });

  document.querySelectorAll(".note").forEach((input) => {
    let saved = input.value;
    async function save() {
      if (input.value === saved) return;
      try { await post(`/api/applications/${rowId(input)}/note`, { note: input.value }); saved = input.value; flash(input, true); }
      catch (e) { flash(input, false); }
    }
    input.addEventListener("blur", save);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && input.tagName === "INPUT") { e.preventDefault(); input.blur(); } });
  });

  // Our own edits also add events, so the baseline moves with them.
  const fresh = document.getElementById("fresh");
  let last = Number(document.body.dataset.lastEvent || 0);
  // Chats change in real time (v8): that page looks more often and reloads itself unless a reply is being typed.
  const onChats = location.pathname.startsWith("/chats");
  if (fresh && last >= 0 && location.pathname !== "/login") {
    setInterval(async () => {
      try {
        const r = await fetch("/api/last-event");
        if (!r.ok) return;
        const { id } = await r.json();
        if (id <= last) return;
        const box = document.querySelector("#reply textarea");
        if (onChats && (!box || (!box.value.trim() && document.activeElement !== box))) location.reload();
        else fresh.hidden = false;
      } catch (e) { /* offline for a moment */ }
    }, onChats ? 8000 : 30000);
    document.addEventListener("jp-edited", async () => {
      const r = await fetch("/api/last-event"); if (r.ok) last = (await r.json()).id;
    });
  }
})();

// Reply to the employer: queued on the site, the hh worker types it into the chat.
(function () {
  const form = document.getElementById("reply");
  if (!form) return;
  const box = form.querySelector("textarea"), error = form.querySelector(".error"), button = form.querySelector("button");
  async function submit() {
    const text = box.value.trim();
    if (!text) return;
    button.disabled = true; error.hidden = true;
    try {
      const r = await fetch(`/api/chats/${form.dataset.chat}/reply`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
      });
      if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); return; }
      if (!r.ok) throw new Error(await r.text());
      box.value = "";
      location.reload();
    } catch (e) { error.textContent = e.message || "Не отправилось"; error.hidden = false; button.disabled = false; }
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  box.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } });
})();

// An open chat starts at its latest message.
document.querySelectorAll(".chat-open .thread, .panel .thread").forEach((t) => { t.scrollTop = t.scrollHeight; });

// Chart hover: the day and its numbers next to the pointer.
(function () {
  const tip = document.getElementById("tip");
  if (!tip) return;
  document.querySelectorAll(".chart .day").forEach((g) => {
    g.addEventListener("pointermove", (e) => {
      tip.textContent = g.dataset.tip; tip.hidden = false;
      const x = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = x + "px"; tip.style.top = (e.clientY - 34) + "px";
    });
    g.addEventListener("pointerleave", () => { tip.hidden = true; });
  });
})();

// Interview form: saved as JSON like the other edits.
(function () {
  const form = document.getElementById("interview");
  if (!form) return;
  const error = form.querySelector(".error");
  async function send(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); return; }
    if (!r.ok) throw new Error(await r.text());
    location.href = form.dataset.back;
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(form).entries());
    const id = form.dataset.id;
    try { await send(id ? `/api/interviews/${id}` : "/api/interviews", body); }
    catch (err) { error.textContent = err.message || "Не сохранилось"; error.hidden = false; }
  });
  const del = form.querySelector("[data-delete]");
  if (del) del.addEventListener("click", async () => {
    if (!confirm("Удалить собеседование из календаря?")) return;
    try { await send(`/api/interviews/${form.dataset.id}/delete`, {}); }
    catch (err) { error.textContent = err.message; error.hidden = false; }
  });
})();

// Settings forms, one-click actions and verdicts, all as JSON.
(function () {
  async function send(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); throw new Error("login"); }
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }
  function showError(scope, err) {
    const box = scope && scope.querySelector(".error");
    if (box) { box.textContent = err.message || "Не сохранилось"; box.hidden = false; } else alert(err.message);
  }

  document.querySelectorAll("form[data-api]").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {};
      for (const el of form.elements) {
        if (!el.name) continue;
        body[el.name] = el.type === "checkbox" ? el.checked : el.value;
      }
      const button = form.querySelector("button:not([type=button])");
      try {
        await send(form.dataset.api, body);
        if (button) button.textContent = "Сохранено ✓";
        setTimeout(() => location.reload(), 400);
      } catch (err) { showError(form, err); }
    });
  });

  document.querySelectorAll("[data-post]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
      try {
        await send(btn.dataset.post, btn.dataset.body ? JSON.parse(btn.dataset.body) : {});
        if (btn.dataset.done) { btn.textContent = btn.dataset.done; btn.disabled = true; }
        else if (btn.dataset.go) location.href = btn.dataset.go;
        else location.reload();
      } catch (err) { showError(btn.closest(".panel"), err); }
    });
  });

  // 👍 / 👎 on a Telegram post or a feed item in the vacancy list; a second click clears it.
  const VERDICT = { 1: ["liked", "интересная"], 0: ["skip", "мимо"], null: ["new", "новая"] };
  document.querySelectorAll("[data-verdict-url] [data-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("[data-verdict-url]");
      const value = btn.classList.contains("on") ? null : Number(btn.dataset.verdict);
      try {
        await send(row.dataset.verdictUrl, { verdict: value });
        row.querySelectorAll("[data-verdict]").forEach((b) => b.classList.toggle("on", value !== null && Number(b.dataset.verdict) === value));
        const st = row.querySelector(".st");
        if (st) { st.className = "st b-" + VERDICT[value][0]; st.textContent = VERDICT[value][1]; }
      } catch (err) { btn.classList.add("failed"); }
    });
  });
})();
