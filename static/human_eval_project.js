(function () {
  const shell = document.querySelector("[data-eval-analytics-api]");
  if (!shell) {
    return;
  }

  const analyticsUrl = shell.dataset.evalAnalyticsApi;
  const updatedAtNode = document.querySelector("#evalAnalyticsUpdatedAt");
  const latexModal = document.querySelector("#evalLatexModal");
  const latexTitleNode = document.querySelector("#evalLatexTitle");
  const latexTextNode = document.querySelector("#evalLatexText");
  let latestPairwiseRows = [];
  let latestAnalyticsRows = [];
  let latestRankLevels = [];

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
    latestAnalyticsRows = Array.isArray(rows) ? rows : [];
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
    latestPairwiseRows = rows;
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

  function escapeLatex(value) {
    return String(value || "")
      .replace(/\\/g, "\\textbackslash{}")
      .replace(/([{}%&#_$])/g, "\\$1")
      .replace(/~/g, "\\textasciitilde{}")
      .replace(/\^/g, "\\textasciicircum{}");
  }

  function parsePercentRate(value) {
    const match = String(value || "").match(/(-?\d+(?:\.\d+)?)\s*%/);
    if (!match) {
      return null;
    }
    const parsed = Number(match[1]);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function parseRankCountsFromDisplay(displayText) {
    const counts = new Map();
    const pattern = /(\d+)\s*:\s*(\d+)/g;
    const source = String(displayText || "");
    let match = pattern.exec(source);
    while (match) {
      const rank = Number(match[1]);
      const count = Number(match[2]);
      if (Number.isFinite(rank) && rank > 0 && Number.isFinite(count) && count >= 0) {
        counts.set(rank, Math.round(count));
      }
      match = pattern.exec(source);
    }
    return counts;
  }

  function inferRankLevelsFromRows(rows) {
    const levels = new Set();
    for (const row of rows || []) {
      for (const bucket of (row && row.rank_distribution) || []) {
        const rank = Number((bucket && bucket.rank) || 0);
        if (Number.isFinite(rank) && rank > 0) {
          levels.add(rank);
        }
      }
      const parsedCounts = parseRankCountsFromDisplay(row && row.rank_distribution_display);
      for (const rank of parsedCounts.keys()) {
        levels.add(rank);
      }
    }
    return Array.from(levels).sort((left, right) => left - right);
  }

  function normalizeRankLevels(rankLevels, rows) {
    const provided = Array.isArray(rankLevels)
      ? rankLevels
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value > 0)
      : [];
    if (provided.length) {
      return Array.from(new Set(provided)).sort((left, right) => left - right);
    }
    return inferRankLevelsFromRows(rows);
  }

  function parseCiBounds(ciDisplay) {
    const text = String(ciDisplay || "");
    const ciPattern = /CI\s*(-?\d+(?:\.\d+)?)\s*[–-]\s*(-?\d+(?:\.\d+)?)/i;
    const match = text.match(ciPattern);
    if (!match) {
      return { low: null, high: null };
    }
    const low = Number(match[1]);
    const high = Number(match[2]);
    if (!Number.isFinite(low) || !Number.isFinite(high)) {
      return { low: null, high: null };
    }
    return { low, high };
  }

  function extractRankCountMap(row) {
    const counts = new Map();
    for (const bucket of (row && row.rank_distribution) || []) {
      const rank = Number((bucket && bucket.rank) || 0);
      const count = Number((bucket && bucket.count) || 0);
      if (Number.isFinite(rank) && rank > 0 && Number.isFinite(count) && count >= 0) {
        counts.set(rank, Math.round(count));
      }
    }
    if (!counts.size) {
      return parseRankCountsFromDisplay(row && row.rank_distribution_display);
    }
    return counts;
  }

  function extractRankingRowsFromTable() {
    const rowNodes = Array.from(document.querySelectorAll("#evalAnalyticsBody .text-row"));
    if (!rowNodes.length) {
      return [];
    }
    const rows = [];
    for (const rowNode of rowNodes) {
      const modelName = (rowNode.querySelector("strong") && rowNode.querySelector("strong").textContent
        ? rowNode.querySelector("strong").textContent
        : "").trim();
      const avgDisplay = (rowNode.querySelector('[data-col="avg"]') && rowNode.querySelector('[data-col="avg"]').textContent
        ? rowNode.querySelector('[data-col="avg"]').textContent
        : "--").trim();
      const avgCiDisplay = (rowNode.querySelector('[data-col="avg-ci"]') && rowNode.querySelector('[data-col="avg-ci"]').textContent
        ? rowNode.querySelector('[data-col="avg-ci"]').textContent
        : "--").trim();
      const rankDistributionDisplay = (rowNode.querySelector('[data-col="dist"]') && rowNode.querySelector('[data-col="dist"]').textContent
        ? rowNode.querySelector('[data-col="dist"]').textContent
        : "").trim();
      const rankingCount = Number((rowNode.querySelector('[data-col="count"]') && rowNode.querySelector('[data-col="count"]').textContent
        ? rowNode.querySelector('[data-col="count"]').textContent
        : "0").trim());

      rows.push({
        model_name: modelName,
        avg_rank: Number(avgDisplay),
        avg_rank_display: avgDisplay,
        avg_rank_ci_display: avgCiDisplay,
        rank_distribution_display: rankDistributionDisplay,
        ranking_count: Number.isFinite(rankingCount) ? rankingCount : 0,
      });
    }
    return rows;
  }

  function pairwiseColorForLatex(rate) {
    const safeRate = Number(rate);
    if (!Number.isFinite(safeRate)) {
      return "winMid";
    }
    if (safeRate >= 80) {
      return "winHigh";
    }
    if (safeRate >= 60) {
      return "winMidHigh";
    }
    if (safeRate >= 30) {
      return "winMid";
    }
    if (safeRate >= 10) {
      return "winMidLow";
    }
    return "winLow";
  }

  function formatModelNameForLatex(modelName) {
    const label = String(modelName || "").trim() || "Model";
    return `\\textsc{${escapeLatex(label)}}`;
  }

  function extractPairwiseRowsFromTable() {
    const table = document.querySelector("#evalPairwiseHeatmap table.eval-heatmap-table");
    if (!table) {
      return [];
    }
    const headerCells = Array.from(table.querySelectorAll("thead th"));
    const headerNames = headerCells.slice(1).map((node) => (node.textContent || "").trim());
    if (!headerNames.length) {
      return [];
    }

    const rows = [];
    const bodyRows = Array.from(table.querySelectorAll("tbody tr"));
    for (let rowIndex = 0; rowIndex < bodyRows.length; rowIndex += 1) {
      const rowNode = bodyRows[rowIndex];
      const rowHeader = rowNode.querySelector("th");
      const modelName = (rowHeader && rowHeader.textContent ? rowHeader.textContent : "").trim() || headerNames[rowIndex] || `Model ${rowIndex + 1}`;
      const cells = [];
      const valueCells = Array.from(rowNode.querySelectorAll("td"));
      for (let columnIndex = 0; columnIndex < headerNames.length; columnIndex += 1) {
        const cellNode = valueCells[columnIndex];
        const isDiagonal = rowIndex === columnIndex || (cellNode && cellNode.classList.contains("diag"));
        const display = (cellNode && cellNode.textContent ? cellNode.textContent : "").trim() || "--";
        const rate = isDiagonal ? null : parsePercentRate(display);
        cells.push({
          source_model_id: rowIndex + 1,
          target_model_id: columnIndex + 1,
          is_diagonal: !!isDiagonal,
          rate,
          display,
        });
      }
      rows.push({
        model_id: rowIndex + 1,
        model_name: modelName,
        cells,
      });
    }
    return rows;
  }

  function buildPairwiseLatex(pairwiseRows) {
    const rows = Array.isArray(pairwiseRows) ? pairwiseRows : [];
    if (!rows.length) {
      return "";
    }

    const modelRows = rows.map((row, index) => ({
      model_id: row && row.model_id != null ? row.model_id : index + 1,
      model_name: (row && row.model_name ? String(row.model_name) : "").trim() || `Model ${index + 1}`,
      cells: Array.isArray(row && row.cells) ? row.cells : [],
    }));
    const columnCount = modelRows.length;
    const lineBreak = "\\\\";

    const lines = [
      "% Define custom soft color palette for win rates",
      "\\definecolor{winHigh}{RGB}{186, 230, 192}   % Light Green (~80-100%)",
      "\\definecolor{winMidHigh}{RGB}{218, 240, 190} % Soft Lime Green (~60-79%)",
      "\\definecolor{winMid}{RGB}{254, 240, 190}     % Light Yellow (~30-59%)",
      "\\definecolor{winMidLow}{RGB}{253, 215, 185}  % Light Orange (~10-29%)",
      "\\definecolor{winLow}{RGB}{248, 190, 190}     % Light Red (~0-9%)",
      "",
      "\\newcommand{\\diag}{\\cellcolor{gray!15}--}",
      "\\begin{table}[htbp]",
      "\\centering",
      "\\small % Slightly smaller text size for clean matrix layout",
      "\\setlength{\\tabcolsep}{8pt} % Better horizontal spacing",
      "\\renewcommand{\\arraystretch}{1.2} % Better vertical line height",
      "",
      "\\caption{Pairwise Win-Rate Heatmap (Cell $A \\to B$ shows how often model $A$ was ranked better than model $B$)}",
      "\\label{tab:pairwise_heatmap}",
      "",
      `\\begin{tabular}{l *{${columnCount}}{c}}`,
      "\\toprule",
      `\\textbf{Model ($A \\downarrow / B \\rightarrow$)} & ${modelRows.map((row) => formatModelNameForLatex(row.model_name)).join(" & ")} ${lineBreak}`,
      "\\midrule",
    ];

    const orderedModelIds = modelRows.map((row) => String(row.model_id));
    for (const sourceRow of modelRows) {
      const cellsByTargetId = new Map(
        sourceRow.cells.map((cell) => [String(cell && cell.target_model_id), cell])
      );
      const latexCells = [];
      for (const targetModelId of orderedModelIds) {
        const cell = cellsByTargetId.get(targetModelId);
        const isDiagonal =
          targetModelId === String(sourceRow.model_id) ||
          !!(cell && cell.is_diagonal);
        if (isDiagonal) {
          latexCells.push("\\diag");
          continue;
        }
        const numericRate = cell && Number.isFinite(Number(cell.rate)) ? Number(cell.rate) : parsePercentRate(cell && cell.display);
        if (!Number.isFinite(numericRate)) {
          latexCells.push("--");
          continue;
        }
        const colorName = pairwiseColorForLatex(numericRate);
        latexCells.push(`\\cellcolor{${colorName}}${numericRate.toFixed(1)}\\%`);
      }
      lines.push(`${formatModelNameForLatex(sourceRow.model_name)} & ${latexCells.join(" & ")} ${lineBreak}`);
    }

    lines.push("\\bottomrule");
    lines.push("\\end{tabular}");
    lines.push("\\end{table}");
    return lines.join("\n");
  }

  function rankingLatexCaptionContext(rows) {
    const counts = rows
      .map((row) => Number((row && row.ranking_count) || 0))
      .filter((value) => Number.isFinite(value) && value > 0);

    let evalText = "evaluations per model";
    if (counts.length) {
      const minCount = Math.min(...counts);
      const maxCount = Math.max(...counts);
      if (minCount === maxCount) {
        evalText = `$N=${minCount}$ evaluations per model`;
      } else {
        evalText = `evaluations per model vary ($N\\in[${minCount}, ${maxCount}]$)`;
      }
    }

    const heading = (document.querySelector("h1") && document.querySelector("h1").textContent
      ? document.querySelector("h1").textContent
      : "").trim();
    const title = heading.includes("·") ? heading.split("·")[0].trim() : heading;
    if (!title) {
      return `Model Ranking and Rank Counts (${evalText})`;
    }
    return `Model Ranking and Rank Counts (${evalText}) for ${escapeLatex(title)}`;
  }

  function formatRankingAvgCell(row) {
    const avgRank = Number((row && row.avg_rank));
    const avgDisplay = Number.isFinite(avgRank)
      ? avgRank.toFixed(2)
      : String((row && row.avg_rank_display) || "--").trim() || "--";

    let low = Number((row && row.avg_rank_ci_low));
    let high = Number((row && row.avg_rank_ci_high));
    if (!Number.isFinite(low) || !Number.isFinite(high)) {
      const parsed = parseCiBounds(row && row.avg_rank_ci_display);
      low = parsed.low;
      high = parsed.high;
    }

    if (Number.isFinite(low) && Number.isFinite(high)) {
      return `${avgDisplay} [${low.toFixed(2)}, ${high.toFixed(2)}]`;
    }
    return avgDisplay;
  }

  function buildRankingLatex(rankingRows, rankLevels) {
    const rows = Array.isArray(rankingRows) ? rankingRows : [];
    const levels = normalizeRankLevels(rankLevels, rows);
    if (!rows.length || !levels.length) {
      return "";
    }

    const lineBreak = "\\\\";
    const rankHeaders = levels.map((rank) => `\\textbf{Rank ${rank}}`).join(" & ");
    const caption = rankingLatexCaptionContext(rows);

    const lines = [
      "\\begin{table}[htbp]",
      "\\centering",
      `\\caption{${caption}}`,
      "\\label{tab:model_rankings}",
      `\\begin{tabular}{l c *{${levels.length}}{c}}`,
      "\\toprule",
      ` & & \\multicolumn{${levels.length}}{c}{\\textbf{Rank Counts ($n$)}} ${lineBreak}`,
      `\\cmidrule(lr){3-${levels.length + 2}}`,
      `\\textbf{Model} & \\textbf{Avg. Rank (95\\% CI)} & ${rankHeaders} ${lineBreak}`,
      "\\midrule",
    ];

    for (const row of rows) {
      const modelName = String((row && row.model_name) || "").trim() || "Model";
      const avgCell = formatRankingAvgCell(row);
      const countByRank = extractRankCountMap(row);
      const countCells = levels.map((rank) => String(countByRank.get(rank) || 0));
      lines.push(`\\textbf{${escapeLatex(modelName)}} & ${avgCell} & ${countCells.join(" & ")} ${lineBreak}`);
    }

    lines.push("\\bottomrule");
    lines.push("\\end{tabular}");
    lines.push("\\end{table}");
    return lines.join("\n");
  }

  function flashExportButton(buttonNode, message) {
    const original = buttonNode.textContent;
    buttonNode.textContent = message;
    setTimeout(() => {
      buttonNode.textContent = original;
    }, 1000);
  }

  function showLatexModal(title, latexText) {
    if (!latexModal || !latexTextNode || !latexTitleNode) {
      return false;
    }
    latexTitleNode.textContent = title;
    latexTextNode.value = latexText;
    if (typeof latexModal.showModal === "function") {
      latexModal.showModal();
    } else {
      latexModal.setAttribute("open", "open");
    }
    latexTextNode.focus();
    latexTextNode.select();
    return true;
  }

  function updateSentenceRankings(sentenceRows, rankLevels) {
    const shellNode = document.querySelector("#evalSentenceRankings");
    if (!shellNode) {
      return;
    }
    shellNode.innerHTML = "";

    const rows = Array.isArray(sentenceRows) ? sentenceRows : [];
    const levels = Array.isArray(rankLevels) ? rankLevels : [];
    if (!rows.length || !levels.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Matrix appears after evaluations are submitted.";
      shellNode.appendChild(empty);
      return;
    }

    const table = document.createElement("table");
    table.className = "eval-sentence-rank-table";

    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    const sentenceHead = document.createElement("th");
    sentenceHead.textContent = "Sentence";
    headRow.appendChild(sentenceHead);
    const evaluatorHead = document.createElement("th");
    evaluatorHead.textContent = "Evaluator";
    headRow.appendChild(evaluatorHead);
    for (const rank of levels) {
      const rankHead = document.createElement("th");
      rankHead.textContent = `Rank ${rank}`;
      headRow.appendChild(rankHead);
    }
    head.appendChild(headRow);
    table.appendChild(head);

    const body = document.createElement("tbody");
    for (const row of rows) {
      const tableRow = document.createElement("tr");
      if (row && row.missing_rank_one) {
        tableRow.classList.add("missing-rank-one");
      }

      const sentenceCell = document.createElement("th");
      const ordinal = Number((row && row.item_ordinal) || 0);
      const sourcePreview = (row && row.source_text_preview) || "";
      sentenceCell.textContent = ordinal > 0 ? `#${ordinal} · ${sourcePreview}` : sourcePreview;
      const sourceText = (row && row.source_text) || "";
      if (sourceText) {
        sentenceCell.title = sourceText;
      }
      tableRow.appendChild(sentenceCell);

      const evaluatorCell = document.createElement("td");
      evaluatorCell.textContent = ((row && row.evaluator_name) || "").trim() || "evaluator";
      tableRow.appendChild(evaluatorCell);

      const cellsByRank = new Map();
      for (const cell of (row && row.rank_cells) || []) {
        const rank = Number((cell && cell.rank) || 0);
        if (rank > 0) {
          cellsByRank.set(rank, cell);
        }
      }

      for (const rank of levels) {
        const rankValue = Number(rank || 0);
        const cell = cellsByRank.get(rankValue);
        const modelCell = document.createElement("td");
        modelCell.textContent = (cell && cell.display) || "--";
        const models = Array.isArray(cell && cell.models) ? cell.models : [];
        modelCell.title = models.length ? models.join(", ") : "No model assigned";
        tableRow.appendChild(modelCell);
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
      latestRankLevels = normalizeRankLevels(payload.rank_levels || [], rows);
      updatePairwiseHeatmap(payload.pairwise || []);
      updateSentenceRankings(payload.sentence_rankings || [], payload.rank_levels || []);
      setUpdatedLabel(payload.updated_at);
    } catch (_error) {
      // Keep the latest successful snapshot.
    }
  }

  document.addEventListener("click", async (event) => {
    const rankingExportButton = event.target.closest('[data-action="show-ranking-latex"]');
    if (rankingExportButton) {
      const rankingRowsForExport = (Array.isArray(latestAnalyticsRows) && latestAnalyticsRows.length)
        ? latestAnalyticsRows
        : extractRankingRowsFromTable();
      const rankLevelsForExport = (Array.isArray(latestRankLevels) && latestRankLevels.length)
        ? latestRankLevels
        : normalizeRankLevels([], rankingRowsForExport);
      const latex = buildRankingLatex(rankingRowsForExport, rankLevelsForExport);
      if (!latex) {
        flashExportButton(rankingExportButton, "No Data");
        return;
      }

      if (!showLatexModal("Model Ranking LaTeX", latex)) {
        flashExportButton(rankingExportButton, "Unavailable");
        return;
      }
      return;
    }

    const exportButton = event.target.closest('[data-action="show-pairwise-latex"]');
    if (exportButton) {
      const rowsForExport = (Array.isArray(latestPairwiseRows) && latestPairwiseRows.length)
        ? latestPairwiseRows
        : extractPairwiseRowsFromTable();
      const latex = buildPairwiseLatex(rowsForExport);
      if (!latex) {
        flashExportButton(exportButton, "No Data");
        return;
      }

      if (!showLatexModal("Pairwise Heatmap LaTeX", latex)) {
        flashExportButton(exportButton, "Unavailable");
        return;
      }
      return;
    }

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
