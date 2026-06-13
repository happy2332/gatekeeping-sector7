// Click-to-sort for any <table class="data sortable">. On phones the table
// is stacked into cards (headers hidden), so we also generate a small
// "Sort by…" dropdown above each sortable table to give mobile users the
// same control.
//
// Each <th> can carry data-sort-key for the comparator type:
//   "num"    — numeric
//   "date"   — combined date + time, expects data-sort-value on each cell
//   "text"   — default; case-insensitive string compare
// data-sortable="false" excludes the column from the sort UI.
(function () {
  function getCellValue(row, idx, type) {
    const td = row.cells[idx];
    if (!td) return type === "num" ? 0 : "";
    const raw = td.dataset.sortValue ?? td.textContent.trim();
    if (type === "num") {
      const n = parseFloat(raw.replace(/[^\d.\-]/g, ""));
      return Number.isFinite(n) ? n : 0;
    }
    return raw.toLowerCase();
  }

  function applySort(table, idx, type, dir) {
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const rows = Array.from(tbody.rows);
    rows.sort((a, b) => {
      const va = getCellValue(a, idx, type);
      const vb = getCellValue(b, idx, type);
      if (va < vb) return dir === "ascending" ? -1 : 1;
      if (va > vb) return dir === "ascending" ? 1 : -1;
      return 0;
    });
    rows.forEach(r => tbody.appendChild(r));
    // Sync the visual indicators on the headers so the desktop UI agrees.
    const ths = table.tHead ? table.tHead.querySelectorAll("th") : [];
    ths.forEach((o, i) => {
      o.removeAttribute("aria-sort");
      if (i === idx) o.setAttribute("aria-sort", dir);
    });
  }

  function makeSortable(table) {
    const ths = table.tHead ? table.tHead.querySelectorAll("th") : [];
    const sortable = [];  // [{idx, label, type}]
    ths.forEach((th, idx) => {
      if (th.dataset.sortable === "false") return;
      const label = th.textContent.trim();
      if (!label) return;
      const type = th.dataset.sortKey || "text";
      sortable.push({ idx, label, type });

      // Desktop: header click toggles asc/desc.
      th.classList.add("sortable-th");
      th.addEventListener("click", () => {
        const current = th.getAttribute("aria-sort");
        const dir = current === "ascending" ? "descending" : "ascending";
        applySort(table, idx, type, dir);
      });
    });

    // Mobile: render a "Sort by…" dropdown just above the table wrapper
    // (or the table itself if there's no wrapper). Hidden on desktop via CSS.
    if (sortable.length > 0) {
      const host = document.createElement("div");
      host.className = "mobile-sort";
      host.innerHTML =
        '<label><span>Sort by</span>' +
        '<select aria-label="Sort by">' +
          sortable.map(s =>
            `<option value="${s.idx}|asc">${s.label} ↑</option>` +
            `<option value="${s.idx}|desc">${s.label} ↓</option>`
          ).join('') +
        '</select></label>';
      const select = host.querySelector("select");
      select.addEventListener("change", () => {
        const [idxStr, dirShort] = select.value.split("|");
        const idx = parseInt(idxStr, 10);
        const meta = sortable.find(s => s.idx === idx);
        if (!meta) return;
        applySort(table, idx, meta.type, dirShort === "asc" ? "ascending" : "descending");
      });
      // Insert just before the table (or its wrapper if present).
      const insertBefore = table.parentElement.classList.contains("table-wrap")
        ? table.parentElement
        : table;
      insertBefore.parentElement.insertBefore(host, insertBefore);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("table.data.sortable").forEach(makeSortable);
  });
})();
