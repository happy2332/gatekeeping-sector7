// Click-to-sort for any <table class="data sortable">.
// Honours data-sort-key on each <th> for the comparator type:
//   "num"    — numeric
//   "date"   — combined date + time, expects data-sort-value on each cell
//   "text"   — default; case-insensitive string compare
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

  function makeSortable(table) {
    const ths = table.tHead ? table.tHead.querySelectorAll("th") : [];
    ths.forEach((th, idx) => {
      if (th.dataset.sortable === "false") return;
      th.classList.add("sortable-th");
      th.addEventListener("click", () => {
        const type = th.dataset.sortKey || "text";
        const current = th.getAttribute("aria-sort");
        const dir = current === "ascending" ? "descending" : "ascending";
        ths.forEach(o => o.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", dir);

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
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("table.data.sortable").forEach(makeSortable);
  });
})();
