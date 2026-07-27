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
      const firstNode = rowNode.querySelector('[data-col="first"]');
      const countNode = rowNode.querySelector('[data-col="count"]');
      if (avgNode) avgNode.textContent = row.avg_rank_display;
      if (firstNode) firstNode.textContent = row.first_place_rate_display;
      if (countNode) countNode.textContent = row.ranking_count;

      const commentBox = document.querySelector(`#eval-comment-${row.model_id}`);
      if (commentBox) {
        commentBox.value = row.comments_blob || "";
      }
    }
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
      updateRows(payload.rows || []);
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
  setInterval(refreshAnalytics, 15000);
})();
