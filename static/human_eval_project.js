(function () {
  const shell = document.querySelector("[data-eval-analytics-api]");
  if (!shell) {
    return;
  }

  const analyticsUrl = shell.dataset.evalAnalyticsApi;
  const updatedAtNode = document.querySelector("#evalAnalyticsUpdatedAt");

  function setUpdatedLabel(value) {
    if (!updatedAtNode) {
      return;
    }
    const parsed = Date.parse(value || "");
    if (!Number.isNaN(parsed)) {
      updatedAtNode.textContent = `updated: ${new Date(parsed).toLocaleTimeString()}`;
      return;
    }
    updatedAtNode.textContent = "updated: now";
  }

  function updateRows(rows) {
    for (const row of rows || []) {
      const rowNode = document.querySelector(`[data-model-id="${row.model_id}"]`);
      if (!rowNode) {
        continue;
      }
      const avgNode = rowNode.querySelector('[data-col="avg"]');
      const avgCiNode = rowNode.querySelector('[data-col="avg-ci"]');
      const firstNode = rowNode.querySelector('[data-col="first"]');
      const firstCiNode = rowNode.querySelector('[data-col="first-ci"]');
      const distNode = rowNode.querySelector('[data-col="dist"]');
      const okNode = rowNode.querySelector('[data-col="ok"]');
      const countNode = rowNode.querySelector('[data-col="count"]');
      if (avgNode) avgNode.textContent = row.avg_rank_display;
      if (avgCiNode) avgCiNode.textContent = row.avg_rank_ci_display || "--";
      if (firstNode) firstNode.textContent = row.first_place_rate_display;
      if (distNode) distNode.textContent = row.rank_distribution_display || "--";
      if (okNode) okNode.textContent = row.uncommented_word_rate_display || "--";
      if (countNode) countNode.textContent = row.ranking_count;

      const isSignificant = !!row.significantly_better_than_next;
      rowNode.dataset.significantNext = isSignificant ? "1" : "0";
      rowNode.classList.toggle("eval-row-significant", isSignificant);
      if (row.next_pair_summary) {
        rowNode.title = row.next_pair_summary;
      } else {
        rowNode.removeAttribute("title");
      }

      const distBarNode = rowNode.querySelector('[data-col="dist-bar"]');
      if (distBarNode) renderRankSegments(distBarNode, row.rank_distribution, row.rank_distribution_display);

      const commentBox = document.querySelector(`#eval-comment-${row.model_id}`);
      if (commentBox) {
        commentBox.value = row.comments_blob || "";
      }
    }
  }

  function renderRankSegments(container, distribution, ariaLabel) {
    if (!container) {
      return;
    }
    const buckets = Array.isArray(distribution) ? distribution : [];
    container.innerHTML = "";
    if (ariaLabel) {
      container.setAttribute("aria-label", ariaLabel);
    }

    let renderedSegments = 0;
    for (const bucket of buckets) {
      const width = Number((bucket && bucket.rate) || 0);
      if (!(width > 0)) {
        continue;
      }
      const segment = document.createElement("span");
      segment.className = "eval-rank-dist-seg";
      segment.style.width = `${width.toFixed(4)}%`;
      if (bucket && bucket.color) {
        segment.style.background = bucket.color;
      }
      const rank = Number((bucket && bucket.rank) || 0);
      const count = Number((bucket && bucket.count) || 0);
      segment.title = rank > 0 ? `Rank ${rank}: ${count}` : `${count}`;
      container.appendChild(segment);
      renderedSegments += 1;
    }

    if (!renderedSegments) {
      const empty = document.createElement("span");
      empty.className = "eval-rank-dist-empty";
      container.appendChild(empty);
    }
  }

  function updatePairwiseHeatmap(pairwiseRows) {
    const shellNode = document.querySelector("#evalPairwiseHeatmap");
    if (!shellNode) {
      return;
    }
    shellNode.innerHTML = "";

    const rows = Array.isArray(pairwiseRows) ? pairwiseRows : [];
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Heatmap appears after evaluations are submitted.";
      shellNode.appendChild(empty);
      return;
    }

    const table = document.createElement("table");
    table.className = "eval-heatmap-table";

    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    const corner = document.createElement("th");
    corner.textContent = "Model";
    headRow.appendChild(corner);
    for (const column of rows) {
      const node = document.createElement("th");
      node.textContent = column.model_name || `Model ${column.model_id}`;
      headRow.appendChild(node);
    }
    head.appendChild(headRow);
    table.appendChild(head);

    const body = document.createElement("tbody");
    for (const row of rows) {
      const tableRow = document.createElement("tr");
      const label = document.createElement("th");
      label.textContent = row.model_name || `Model ${row.model_id}`;
      tableRow.appendChild(label);

      for (const cell of row.cells || []) {
        const cellNode = document.createElement("td");
        if (cell && cell.is_diagonal) {
          cellNode.classList.add("diag");
        }
        if (cell && cell.background) {
          cellNode.style.background = cell.background;
        }
        const comparisons = Number((cell && cell.comparisons) || 0);
        if (cell && cell.is_diagonal) {
          cellNode.title = "Same model";
        } else if (comparisons > 0) {
          cellNode.title = `${comparisons} paired evaluations`;
        } else {
          cellNode.title = "No overlapping evaluations";
        }
        cellNode.textContent = (cell && cell.display) || "--";
        tableRow.appendChild(cellNode);
      }
      body.appendChild(tableRow);
    }
    table.appendChild(body);
    shellNode.appendChild(table);
  }

  async function refreshAnalytics() {
    try {
      const response = await fetch(analyticsUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      if (payload.status !== "ok") {
        return;
      }
      const rows = payload.rows || [];
      updateRows(rows);
      updatePairwiseHeatmap(payload.pairwise || []);
      setUpdatedLabel(payload.updated_at);
    } catch (_error) {
      // Keep the latest successful snapshot.
    }
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-target]");
    if (!button) {
      return;
    }
    const targetId = button.dataset.copyTarget;
    if (!targetId) {
      return;
    }
    const input = document.getElementById(targetId);
    if (!input) {
      return;
    }

    const text = input.value || "";
    try {
      await navigator.clipboard.writeText(text);
      const original = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => {
        button.textContent = original;
      }, 1000);
    } catch (_error) {
      input.focus();
      input.select();
    }
  });

  refreshAnalytics();
})();
