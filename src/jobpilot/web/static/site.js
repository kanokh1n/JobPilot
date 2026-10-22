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
        if (![...sel.options].some((o) => o.value === "__auto__")) sel.add(new Option("↺ вернуть автомат", "__auto__"));
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
  if (fresh && last >= 0 && location.pathname !== "/login") {
    setInterval(async () => {
      try {
        const r = await fetch("/api/last-event");
        if (!r.ok) return;
        const { id } = await r.json();
        if (id > last) { fresh.hidden = false; }
      } catch (e) { /* offline for a moment */ }
    }, 30000);
    document.addEventListener("jp-edited", async () => {
      const r = await fetch("/api/last-event"); if (r.ok) last = (await r.json()).id;
    });
  }
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

// v6: settings forms (Telegram, settings page), one-click actions and post verdicts, all as JSON.
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
        if (btn.dataset.done) { btn.textContent = btn.dataset.done; btn.disabled = true; } else location.reload();
      } catch (err) { showError(btn.closest(".panel"), err); }
    });
  });

  document.querySelectorAll(".post [data-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const post = btn.closest(".post");
      const value = btn.classList.contains("on") ? null : Number(btn.dataset.verdict);  // a second click clears it
      try {
        await send(`/api/telegram/posts/${post.dataset.id}/verdict`, { verdict: value });
        post.querySelectorAll("[data-verdict]").forEach((b) => b.classList.toggle("on", value !== null && Number(b.dataset.verdict) === value));
      } catch (err) { btn.classList.add("failed"); }
    });
  });
})();
