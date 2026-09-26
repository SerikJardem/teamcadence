const gridEl = document.getElementById("grid");
const grandEl = document.getElementById("grand");
const csvPathEl = document.getElementById("csvPath");
const toastEl = document.getElementById("toast");

function showToast(text) {
  toastEl.hidden = false;
  toastEl.textContent = text;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { toastEl.hidden = true; }, 1600);
}

function render(state) {
  grandEl.textContent = String(state.grand_total ?? 0);
  if (state.csv) csvPathEl.textContent = state.csv;
  gridEl.innerHTML = "";
  for (const row of state.items || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "item";
    btn.dataset.item = row.item;
    btn.innerHTML = `<span>${row.item}</span><span class="count">${row.total}</span>`;
    btn.addEventListener("click", () => sell(row.item, btn));
    gridEl.appendChild(btn);
  }
}

async function sell(item, btn) {
  btn.disabled = true;
  try {
    const res = await fetch(`/api/sale/${encodeURIComponent(item)}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "sale failed");
    btn.classList.add("pulse");
    setTimeout(() => btn.classList.remove("pulse"), 220);
    render(data);
    showToast(`✅ ${data.item} · всего ${data.total}`);
  } catch (err) {
    showToast(String(err.message || err));
  } finally {
    btn.disabled = false;
  }
}

async function refresh() {
  const res = await fetch("/api/state");
  const data = await res.json();
  render(data);
}

refresh();
