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
