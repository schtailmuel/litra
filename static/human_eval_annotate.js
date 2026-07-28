(function () {
  const sourceRoot = document.getElementById("evalSourceAnnotator");
  const rankContainer = document.querySelector("[data-rank-container]");
  const rankRows = Array.from(rankContainer?.querySelectorAll("[data-rank-row][data-candidate-model]") || []);
  const feedbackNode = document.getElementById("evalInteractionFeedback");
  const referenceNode = document.getElementById("evalReferenceText");
  const commentModal = document.getElementById("evalCommentModal");
  const commentModalField = document.getElementById("evalCommentModalField");
  const commentModalTitle = document.getElementById("evalCommentModalTitle");
  const markNoteModal = document.getElementById("evalMarkNoteModal");
  const markNoteModalField = document.getElementById("evalMarkNoteModalField");
  const markNoteModalTitle = document.getElementById("evalMarkNoteModalTitle");
  const markNoteModalSnippet = document.getElementById("evalMarkNoteModalSnippet");

  if (!sourceRoot || !rankContainer || !rankRows.length) {
    return;
  }

  const spectralPalette = [
    "#3288bd",
    "#66c2a5",
    "#abdda4",
    "#e6f598",
    "#ffffbf",
    "#fee08b",
    "#fdae61",
    "#f46d43",
    "#d53e4f",
    "#9e0142",
  ];

  const stateByModel = new Map();
  const inputByModel = new Map();
  const sourceListByModel = new Map();
  const styleListByModel = new Map();
  const targetByModel = new Map();
  const rankInputByModel = new Map();
  const rankControlsByModel = new Map();
  const commentInputByModel = new Map();
  const commentButtonByModel = new Map();
  const commentPreviewByModel = new Map();

  let activeModelId = String(rankRows[0].dataset.candidateModel || "");
  let pendingMarkState = null;
  let draggedModelId = null;
  let activeCommentModelId = null;
  let activeMarkNoteContext = null;

  function setFeedback(message) {
    if (!feedbackNode) {
      return;
    }
    feedbackNode.textContent = message;
  }

  function normalizeSnippet(value, limit = 180) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, limit);
  }

  function normalizeMarkNote(value, limit = 320) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, limit);
  }

  function parseRank(value) {
    const parsed = Number.parseInt(String(value || ""), 10);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  }

  function maxRankValue() {
    return Math.max(1, rankInputByModel.size);
  }

  function clampRank(rank) {
    if (!Number.isInteger(rank)) {
      return null;
    }
    return Math.min(Math.max(rank, 1), maxRankValue());
  }

  function rowOrder() {
    return Array.from(rankContainer.querySelectorAll("[data-rank-row][data-candidate-model]"));
  }

  function modelOrder() {
    return rowOrder().map((row) => String(row.dataset.candidateModel || ""));
  }

  function modelRow(modelId) {
    return rankContainer.querySelector(`[data-rank-row][data-candidate-model="${modelId}"]`);
  }

  function modelLabel(modelId) {
    const row = modelRow(modelId);
    const letter = String(row?.dataset.candidateLabel || "").trim();
    if (letter) {
      return `Candidate ${letter}`;
    }
    return "Candidate";
  }

  function rankValue(modelId) {
    return parseRank(rankInputByModel.get(modelId)?.value);
  }

  function setRankValue(modelId, rank) {
    const input = rankInputByModel.get(modelId);
    if (!input) {
      return;
    }
    const next = clampRank(rank);
    input.value = next ? String(next) : "";
  }

  function compareWithTie(leftValue, rightValue, epsilon = 1e-6) {
    if (Math.abs(leftValue - rightValue) <= epsilon) {
      return 0;
    }
    return leftValue < rightValue ? -1 : 1;
  }

  function colorForRank(rank) {
    if (!rank) {
      return "#cbd5e1";
    }
    const max = maxRankValue();
    const ratio = max <= 1 ? 0 : (rank - 1) / (max - 1);
    const colorIndex = Math.round(ratio * (spectralPalette.length - 1));
    return spectralPalette[Math.min(Math.max(colorIndex, 0), spectralPalette.length - 1)];
  }

  function hexToRgba(hex, alpha) {
    const clean = String(hex || "").replace("#", "");
    if (clean.length !== 6) {
      return `rgba(148, 163, 184, ${alpha})`;
    }
    const red = Number.parseInt(clean.slice(0, 2), 16);
    const green = Number.parseInt(clean.slice(2, 4), 16);
    const blue = Number.parseInt(clean.slice(4, 6), 16);
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
  }

  function tieCountsByRank() {
    const counts = new Map();
    for (const modelId of rankInputByModel.keys()) {
      const rank = rankValue(modelId);
      if (!rank) {
        continue;
      }
      counts.set(rank, (counts.get(rank) || 0) + 1);
    }
    return counts;
  }

  function updateRankButtons(modelId) {
    const rank = rankValue(modelId);
    const color = colorForRank(rank);
    const groups = rankControlsByModel.get(modelId) || [];
    groups.forEach((group) => {
      group.style.setProperty("--eval-rank-color", color);
      const buttons = Array.from(group.querySelectorAll("[data-rank-value]"));
      buttons.forEach((button) => {
        const candidateRank = parseRank(button.dataset.rankValue);
        const active = rank && candidateRank === rank;
        button.classList.toggle("active", Boolean(active));
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    });
  }

  function updateCommentButton(modelId) {
    const input = commentInputByModel.get(modelId);
    const button = commentButtonByModel.get(modelId);
    const previewNode = commentPreviewByModel.get(modelId);
    if (!button && !previewNode) {
      return;
    }
    const commentText = String(input?.value || "").trim();
    const preview = normalizeSnippet(commentText, 170);
    const hasComment = Boolean(preview);
    if (button) {
      button.classList.toggle("has-comment", hasComment);
      button.title = hasComment ? `Edit comment: ${preview}` : "Edit comment";
    }
    if (previewNode) {
      previewNode.textContent = hasComment ? preview : "No comment.";
      previewNode.title = hasComment ? commentText : "";
    }
  }

  function updateRankUi() {
    const rankTieCounts = tieCountsByRank();
    for (const modelId of rankInputByModel.keys()) {
      const rank = rankValue(modelId);
      const color = colorForRank(rank);
      const fill = hexToRgba(color, 0.14);
      const tieCount = rank ? rankTieCounts.get(rank) || 1 : 0;
      const row = modelRow(modelId);
      if (row) {
        row.style.setProperty("--eval-rank-color", color);
        row.style.setProperty("--eval-rank-fill", fill);
        row.dataset.tie = rank && tieCount > 1 ? "1" : "0";
      }
      updateRankButtons(modelId);
      updateCommentButton(modelId);
    }
  }

  function ensureInitialRanks() {
    modelOrder().forEach((modelId, index) => {
      if (!rankValue(modelId)) {
        setRankValue(modelId, index + 1);
      }
    });
  }

  function rankSortedModelIds() {
    const ordered = modelOrder().map((modelId, index) => {
      const rank = rankValue(modelId);
      return {
        modelId,
        rank: rank || maxRankValue() + index + 1,
        index,
      };
    });

    ordered.sort((left, right) => {
      if (left.rank !== right.rank) {
        return left.rank - right.rank;
      }
      return left.index - right.index;
    });

    return ordered.map((item) => item.modelId);
  }

  function snapshotRects(elements) {
    const snapshot = new Map();
    elements.forEach((element) => {
      snapshot.set(element, element.getBoundingClientRect());
    });
    return snapshot;
  }

  function animateFromSnapshot(elements, snapshot) {
    elements.forEach((element) => {
      const before = snapshot.get(element);
      if (!before) {
        return;
      }
      const after = element.getBoundingClientRect();
      const dx = before.left - after.left;
      const dy = before.top - after.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) {
        return;
      }
      element.animate(
        [
          { transform: `translate(${dx}px, ${dy}px)` },
          { transform: "translate(0, 0)" },
        ],
        {
          duration: 260,
          easing: "cubic-bezier(0.2, 0, 0, 1)",
        },
      );
    });
  }

  function withReorderAnimation(mutator) {
    const rows = rowOrder();
    const snapshot = snapshotRects(rows);
    mutator();
    animateFromSnapshot(rowOrder(), snapshot);
  }

  function applyModelOrder(modelIds, options = {}) {
    const { animate = true } = options;
    const mutator = () => {
      modelIds.forEach((modelId) => {
        const row = modelRow(modelId);
        if (row) {
          rankContainer.appendChild(row);
        }
      });
    };

    if (animate) {
      withReorderAnimation(mutator);
    } else {
      mutator();
    }

    updateRankUi();
  }

  function modelIdsAfterInsert(order, draggedId, targetId) {
    const filtered = order.filter((modelId) => modelId !== draggedId);
    if (!targetId || !filtered.includes(targetId)) {
      filtered.push(draggedId);
      return filtered;
    }
    const targetIndex = filtered.indexOf(targetId);
    filtered.splice(targetIndex, 0, draggedId);
    return filtered;
  }

  function recomputeStrictRanksFromOrder() {
    modelOrder().forEach((modelId, index) => {
      setRankValue(modelId, index + 1);
    });
  }

  function reorderByRank(options = {}) {
    const { animate = true } = options;
    applyModelOrder(rankSortedModelIds(), { animate });
  }

  function tokensForBleu(text) {
    const value = String(text || "").toLowerCase();
    try {
      const unicodeWordRegex = new RegExp("[\\p{L}\\p{N}]+", "gu");
      const unicodeTokens = value.match(unicodeWordRegex);
      if (unicodeTokens?.length) {
        return unicodeTokens;
      }
    } catch (_error) {
      // Fallback for environments without Unicode property escapes.
    }
    return value.match(/[a-z0-9]+/g) || [];
  }

  function ngramCounts(tokens, n) {
    const counts = new Map();
    if (!Array.isArray(tokens) || tokens.length < n) {
      return counts;
    }
    for (let index = 0; index <= tokens.length - n; index += 1) {
      const gram = tokens.slice(index, index + n).join("\u0001");
      counts.set(gram, (counts.get(gram) || 0) + 1);
    }
    return counts;
  }

  function sentenceBleu(candidateText, referenceText) {
    const candidateTokens = tokensForBleu(candidateText);
    const referenceTokens = tokensForBleu(referenceText);
    if (!candidateTokens.length || !referenceTokens.length) {
      return 0;
    }

    const precisions = [];
    for (let n = 1; n <= 4; n += 1) {
      if (candidateTokens.length < n) {
        precisions.push(1);
        continue;
      }
      const candidateCounts = ngramCounts(candidateTokens, n);
      const referenceCounts = ngramCounts(referenceTokens, n);
      let matchCount = 0;
      let totalCount = 0;

      for (const [gram, count] of candidateCounts.entries()) {
        totalCount += count;
        const refCount = referenceCounts.get(gram) || 0;
        matchCount += Math.min(count, refCount);
      }

      precisions.push((matchCount + 1) / (totalCount + 1));
    }

    const referenceLength = referenceTokens.length;
    const candidateLength = candidateTokens.length;
    const brevityPenalty = candidateLength > referenceLength
      ? 1
      : Math.exp(1 - referenceLength / Math.max(candidateLength, 1));
    const logPrecisionMean = precisions.reduce((sum, value) => sum + Math.log(value), 0) / precisions.length;
    return brevityPenalty * Math.exp(logPrecisionMean);
  }

  function mergedIntervals(marks, maxLength) {
    const intervals = [];
    for (const mark of marks || []) {
      const start = Math.max(0, Math.min(Number(mark.start) || 0, maxLength));
      const end = Math.max(start, Math.min(Number(mark.end) || 0, maxLength));
      if (end > start) {
        intervals.push([start, end]);
      }
    }
    intervals.sort((left, right) => left[0] - right[0]);

    const merged = [];
    for (const interval of intervals) {
      const previous = merged[merged.length - 1];
      if (!previous || interval[0] > previous[1]) {
        merged.push(interval);
      } else {
        previous[1] = Math.max(previous[1], interval[1]);
      }
    }
    return merged;
  }

  function sourceCoveragePercent(sourceText, sourceMarks) {
    const raw = String(sourceText || "");
    const totalNonWhitespace = raw.replace(/\s+/g, "").length;
    if (!totalNonWhitespace) {
      return 0;
    }

    let markedNonWhitespace = 0;
    const intervals = mergedIntervals(sourceMarks, raw.length);
    for (const [start, end] of intervals) {
      markedNonWhitespace += raw.slice(start, end).replace(/\s+/g, "").length;
    }
    return (markedNonWhitespace * 100) / totalNonWhitespace;
  }

  function applyAutoRankSuggestion() {
    try {
      const sourceText = String(sourceRoot.dataset.rawText || sourceRoot.textContent || "");
      const referenceText = String(referenceNode?.textContent || "");
      const modelIds = Array.from(rankInputByModel.keys());
      if (!modelIds.length) {
        setFeedback("Auto-rank skipped: no candidates available.");
        return;
      }

      const hasAnyAnnotations = modelIds.some((modelId) => {
        const state = stateByModel.get(modelId) || { source_marks: [], style_marks: [] };
        return (state.source_marks?.length || 0) + (state.style_marks?.length || 0) > 0;
      });

      const scored = modelIds.map((modelId) => {
        const state = stateByModel.get(modelId) || { source_marks: [], style_marks: [] };
        const sourceMarkCount = state.source_marks?.length || 0;
        const styleMarkCount = state.style_marks?.length || 0;
        const totalMarks = sourceMarkCount + styleMarkCount;
        const percent = sourceCoveragePercent(sourceText, state.source_marks || []);
        const targetText = String(targetByModel.get(modelId)?.dataset.rawText || "");
        const bleu = sentenceBleu(targetText, referenceText);
        return {
          modelId,
          totalMarks,
          sourceErrorPercent: percent,
          bleu,
        };
      });

      let sorted;
      let rankMode;
      if (!hasAnyAnnotations) {
        rankMode = "bleu";
        sorted = [...scored].sort((left, right) => {
          const cmp = compareWithTie(right.bleu, left.bleu);
          if (cmp !== 0) {
            return cmp;
          }
          return String(left.modelId).localeCompare(String(right.modelId));
        });
      } else {
        rankMode = "annotations";
        sorted = [...scored].sort((left, right) => {
          const percentCmp = compareWithTie(left.sourceErrorPercent, right.sourceErrorPercent, 1e-3);
          if (percentCmp !== 0) {
            return percentCmp;
          }
          if (left.totalMarks !== right.totalMarks) {
            return left.totalMarks - right.totalMarks;
          }
          return String(left.modelId).localeCompare(String(right.modelId));
        });
      }

      let currentRank = 1;
      sorted.forEach((item, index) => {
        if (index > 0) {
          const previous = sorted[index - 1];
          const tied = rankMode === "bleu"
            ? compareWithTie(item.bleu, previous.bleu) === 0
            : compareWithTie(item.sourceErrorPercent, previous.sourceErrorPercent, 1e-3) === 0
              && item.totalMarks === previous.totalMarks;
          if (!tied) {
            currentRank += 1;
          }
        }
        setRankValue(item.modelId, currentRank);
      });

      applyModelOrder(sorted.map((item) => item.modelId), { animate: true });

      const summary = sorted
        .slice(0, 4)
        .map((item) => {
          if (rankMode === "bleu") {
            return `${modelLabel(item.modelId)} BLEU=${item.bleu.toFixed(3)}`;
          }
          return `${modelLabel(item.modelId)} err=${item.totalMarks}, src=${item.sourceErrorPercent.toFixed(1)}%`;
        })
        .join(" | ");

      if (rankMode === "bleu") {
        setFeedback(`Auto-rank suggestion used BLEU vs reference (higher is better). ${summary}`);
      } else {
        setFeedback(`Auto-rank suggestion used annotations (lower source-error% and fewer marks are better). ${summary}`);
      }
    } catch (_error) {
      setFeedback("Auto-rank suggestion failed. Please try reloading this page.");
    }
  }

  function parseState(raw) {
    function normalizeMarks(value) {
      if (!Array.isArray(value)) {
        return [];
      }
      return value
        .map((mark) => {
          if (!mark || typeof mark !== "object") {
            return null;
          }
          const start = Number(mark.start || 0);
          const end = Number(mark.end || 0);
          const text = normalizeSnippet(mark.text || "", 220);
          const note = normalizeMarkNote(mark.note || "", 320);
          if (!text || end <= start) {
            return null;
          }
          return {
            start,
            end,
            text,
            note,
          };
        })
        .filter((mark) => Boolean(mark));
    }

    if (!raw) {
      return { source_marks: [], style_marks: [] };
    }
    try {
      const parsed = JSON.parse(raw);
      return {
        source_marks: normalizeMarks(parsed?.source_marks),
        style_marks: normalizeMarks(parsed?.style_marks),
      };
    } catch (_error) {
      return { source_marks: [], style_marks: [] };
    }
  }

  function syncHiddenInput(modelId) {
    const input = inputByModel.get(modelId);
    const state = stateByModel.get(modelId);
    if (!input || !state) {
      return;
    }
    input.value = JSON.stringify({
      source_marks: state.source_marks,
      style_marks: state.style_marks,
    });
  }

  function tokenElements(root) {
    if (!root) {
      return [];
    }
    return Array.from(root.querySelectorAll(".eval-word-token"));
  }

  function clearHover(root) {
    tokenElements(root).forEach((token) => token.classList.remove("eval-word-hover"));
  }

  function tokenOverlapsMark(token, mark) {
    const tokenStart = Number(token.dataset.start || 0);
    const tokenEnd = Number(token.dataset.end || 0);
    return tokenStart < Number(mark.end) && tokenEnd > Number(mark.start);
  }

  function applyMarkedClasses(root, marks) {
    const tokens = tokenElements(root);
    for (const token of tokens) {
      const marked = marks.some((mark) => tokenOverlapsMark(token, mark));
      token.classList.toggle("eval-word-marked", marked);
    }
  }

  function renderHighlights() {
    const activeState = stateByModel.get(activeModelId) || { source_marks: [], style_marks: [] };
    applyMarkedClasses(sourceRoot, activeState.source_marks || []);

    for (const [modelId, targetRoot] of targetByModel.entries()) {
      const state = stateByModel.get(modelId) || { style_marks: [] };
      applyMarkedClasses(targetRoot, state.style_marks || []);
    }
  }

  function renderList(container, marks, modelId, markType) {
    if (!container) {
      return;
    }
    container.innerHTML = "";
    if (!marks.length) {
      container.innerHTML = '<span class="muted eval-mark-empty">—</span>';
      return;
    }

    marks.forEach((mark, index) => {
      const row = document.createElement("span");
      row.className = "eval-mark-item";

      const snippet = document.createElement("span");
      snippet.className = "eval-mark-snippet";
      const fullSnippet = normalizeSnippet(mark.text || "", 220) || "[empty]";
      const note = normalizeMarkNote(mark.note || "", 320);
      snippet.textContent = normalizeSnippet(fullSnippet, 72) || "[empty]";
      snippet.title = note ? `${fullSnippet}\nNote: ${note}` : fullSnippet;
      row.appendChild(snippet);

      const noteButton = document.createElement("button");
      noteButton.type = "button";
      noteButton.className = "secondary eval-mark-note";
      noteButton.textContent = note ? "📝" : "＋";
      noteButton.title = note ? `Edit note: ${normalizeSnippet(note, 110)}` : "Add note";
      noteButton.setAttribute("aria-label", note ? "Edit mark note" : "Add mark note");
      noteButton.dataset.action = "edit-mark-note";
      noteButton.dataset.noteModelId = modelId;
      noteButton.dataset.noteType = markType;
      noteButton.dataset.noteIndex = String(index);
      row.appendChild(noteButton);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary eval-mark-remove";
      remove.textContent = "✕";
      remove.title = "Remove mark";
      remove.setAttribute("aria-label", "Remove mark");
      remove.dataset.removeModelId = modelId;
      remove.dataset.removeType = markType;
      remove.dataset.removeIndex = String(index);
      row.appendChild(remove);

      container.appendChild(row);
    });
  }

  function renderModel(modelId) {
    const state = stateByModel.get(modelId);
    if (!state) {
      return;
    }
    renderList(sourceListByModel.get(modelId), state.source_marks, modelId, "source");
    renderList(styleListByModel.get(modelId), state.style_marks, modelId, "style");
    syncHiddenInput(modelId);
    renderHighlights();
  }

  function markExists(marks, candidate) {
    return marks.some(
      (item) => Number(item.start) === Number(candidate.start)
        && Number(item.end) === Number(candidate.end),
    );
  }

  function addMark(modelId, markType, mark) {
    const state = stateByModel.get(modelId);
    if (!state) {
      return false;
    }
    const list = markType === "source" ? state.source_marks : state.style_marks;
    const normalized = {
      start: Number(mark.start || 0),
      end: Number(mark.end || 0),
      text: normalizeSnippet(mark.text || "", 220),
      note: normalizeMarkNote(mark.note || "", 320),
    };
    if (!normalized.text || normalized.end <= normalized.start) {
      return false;
    }
    if (markExists(list, normalized)) {
      return false;
    }
    list.push(normalized);
    renderModel(modelId);
    return true;
  }

  function markListFor(state, markType) {
    return markType === "source" ? state.source_marks : state.style_marks;
  }

  function getMark(modelId, markType, index) {
    const state = stateByModel.get(modelId);
    if (!state) {
      return null;
    }
    const list = markListFor(state, markType);
    if (!Array.isArray(list) || index < 0 || index >= list.length) {
      return null;
    }
    return list[index];
  }

  function removeMark(modelId, markType, index) {
    const state = stateByModel.get(modelId);
    if (!state) {
      return;
    }
    const list = markType === "source" ? state.source_marks : state.style_marks;
    if (index < 0 || index >= list.length) {
      return;
    }
    list.splice(index, 1);
    renderModel(modelId);
    setFeedback("Removed mark.");
  }

  function setActiveModel(modelId, options = {}) {
    if (!modelId || !stateByModel.has(modelId)) {
      return;
    }
    const changed = modelId !== activeModelId;
    if (
      pendingMarkState
      && pendingMarkState.kind === "source"
      && pendingMarkState.modelId !== modelId
    ) {
      clearPendingMark();
    }
    activeModelId = modelId;
    rowOrder().forEach((row) => {
      const current = String(row.dataset.candidateModel || "");
      row.dataset.activeSource = current === activeModelId ? "1" : "0";
    });
    renderHighlights();
    if (changed && options.announce !== false) {
      setFeedback(`Source-side marking active for ${modelLabel(activeModelId)} (selected row).`);
    }
  }

  function tokenizeText(root) {
    if (!root) {
      return;
    }
    const rawText = root.textContent || "";
    root.dataset.rawText = rawText;
    root.innerHTML = "";
    let offset = 0;
    const parts = rawText.split(/(\s+)/);
    for (const part of parts) {
      if (!part) {
        continue;
      }
      if (/^\s+$/.test(part)) {
        root.appendChild(document.createTextNode(part));
        offset += part.length;
        continue;
      }

      const token = document.createElement("span");
      token.className = "eval-word-token";
      token.textContent = part;
      token.dataset.start = String(offset);
      token.dataset.end = String(offset + part.length);
      root.appendChild(token);
      offset += part.length;
    }
  }

  function markFromTokenRange(root, start, end) {
    const raw = String(root.dataset.rawText || "");
    const from = Math.max(0, Math.min(start, end));
    const to = Math.max(from, Math.max(start, end));
    return {
      start: from,
      end: to,
      text: raw.slice(from, to),
    };
  }

  function applyHoverRange(root, start, end) {
    const from = Math.min(start, end);
    const to = Math.max(start, end);
    for (const token of tokenElements(root)) {
      const tokenStart = Number(token.dataset.start || 0);
      const tokenEnd = Number(token.dataset.end || 0);
      const inRange = tokenStart >= from && tokenEnd <= to;
      token.classList.toggle("eval-word-hover", inRange);
    }
  }

  function clearPendingMark() {
    if (!pendingMarkState) {
      return;
    }
    const { root } = pendingMarkState;
    for (const token of tokenElements(root)) {
      token.classList.remove("eval-word-pending");
    }
    clearHover(root);
    pendingMarkState = null;
  }

  function setPendingMark(kind, modelId, root, token) {
    clearPendingMark();
    pendingMarkState = {
      kind,
      modelId,
      root,
      anchorStart: Number(token.dataset.start || 0),
      anchorEnd: Number(token.dataset.end || 0),
    };
    token.classList.add("eval-word-pending");
    applyHoverRange(root, pendingMarkState.anchorStart, pendingMarkState.anchorEnd);
  }

  function pendingRangeFromToken(token) {
    if (!pendingMarkState) {
      return null;
    }
    const tokenStart = Number(token.dataset.start || 0);
    const tokenEnd = Number(token.dataset.end || 0);
    const start = Math.min(pendingMarkState.anchorStart, tokenStart);
    const end = Math.max(pendingMarkState.anchorEnd, tokenEnd);
    return { start, end };
  }

  function previewPendingRange(token) {
    if (!pendingMarkState) {
      return;
    }
    const range = pendingRangeFromToken(token);
    if (!range) {
      return;
    }
    applyHoverRange(pendingMarkState.root, range.start, range.end);
  }

  function completePendingMark(token) {
    if (!pendingMarkState) {
      return;
    }
    const { kind, modelId, root } = pendingMarkState;
    const range = pendingRangeFromToken(token);
    if (!range) {
      clearPendingMark();
      return;
    }
    const markType = kind === "source" ? "source" : "style";
    const created = addMark(modelId, markType, markFromTokenRange(root, range.start, range.end));
    clearPendingMark();

    if (created) {
      setFeedback(
        markType === "source"
          ? "Added source-side translation-error mark. Use + to attach a note."
          : "Added target style-issue mark. Use + to attach a note.",
      );
    } else {
      setFeedback("Mark already exists or was empty.");
    }
  }

  function clearDropTargets() {
    rankContainer.querySelectorAll(".eval-drop-target").forEach((node) => {
      node.classList.remove("eval-drop-target");
    });
  }

  function enableRankDragAndDrop() {
    const handles = Array.from(document.querySelectorAll("[data-drag-handle]"));
    handles.forEach((handle) => {
      handle.setAttribute("draggable", "true");
    });

    document.addEventListener("dragstart", (event) => {
      const handle = event.target.closest("[data-drag-handle]");
      if (!handle) {
        return;
      }
      const modelId = String(handle.dataset.dragModel || "");
      if (!modelId) {
        return;
      }
      draggedModelId = modelId;
      const row = modelRow(modelId);
      row?.classList.add("eval-card-dragging");

      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", modelId);
      }
    });

    rankContainer.addEventListener("dragover", (event) => {
      if (!draggedModelId) {
        return;
      }
      event.preventDefault();
      clearDropTargets();
      const target = event.target.closest("[data-rank-row]");
      if (target && String(target.dataset.candidateModel || "") !== draggedModelId) {
        target.classList.add("eval-drop-target");
      }
    });

    rankContainer.addEventListener("drop", (event) => {
      if (!draggedModelId) {
        return;
      }
      event.preventDefault();
      const target = event.target.closest("[data-rank-row]");
      const targetId = target && String(target.dataset.candidateModel || "") !== draggedModelId
        ? String(target.dataset.candidateModel || "")
        : null;
      const nextOrder = modelIdsAfterInsert(modelOrder(), draggedModelId, targetId);
      applyModelOrder(nextOrder, { animate: true });
      recomputeStrictRanksFromOrder();
      updateRankUi();
      setFeedback("Inserted row before target and recomputed rankings from new order.");
      clearDropTargets();
    });

    document.addEventListener("dragend", () => {
      if (!draggedModelId) {
        return;
      }
      modelRow(draggedModelId)?.classList.remove("eval-card-dragging");
      clearDropTargets();
      draggedModelId = null;
    });
  }

  function openCommentModal(modelId) {
    const input = commentInputByModel.get(modelId);
    if (!commentModal || !commentModalField || !input) {
      return;
    }
    activeCommentModelId = modelId;
    commentModalField.value = input.value || "";
    if (commentModalTitle) {
      commentModalTitle.textContent = `Comment for ${modelLabel(modelId)}`;
    }
    if (typeof commentModal.showModal === "function") {
      commentModal.showModal();
    } else {
      commentModal.setAttribute("open", "open");
    }
    commentModalField.focus();
  }

  function closeCommentModal() {
    if (!commentModal) {
      return;
    }
    if (commentModal.open && typeof commentModal.close === "function") {
      commentModal.close();
    } else {
      commentModal.removeAttribute("open");
    }
    activeCommentModelId = null;
  }

  function saveCommentModal() {
    if (!activeCommentModelId || !commentModalField) {
      closeCommentModal();
      return;
    }
    const input = commentInputByModel.get(activeCommentModelId);
    if (input) {
      input.value = commentModalField.value || "";
      updateCommentButton(activeCommentModelId);
    }
    setFeedback(`Comment updated for ${modelLabel(activeCommentModelId)}.`);
    closeCommentModal();
  }

  function openMarkNoteModal(modelId, markType, index) {
    if (!markNoteModal || !markNoteModalField) {
      return;
    }
    const mark = getMark(modelId, markType, index);
    if (!mark) {
      return;
    }

    activeMarkNoteContext = { modelId, markType, index };
    markNoteModalField.value = normalizeMarkNote(mark.note || "", 320);

    if (markNoteModalTitle) {
      const prefix = markType === "source" ? "Source" : "Style";
      markNoteModalTitle.textContent = `${prefix} mark note for ${modelLabel(modelId)}`;
    }
    if (markNoteModalSnippet) {
      const snippet = normalizeSnippet(mark.text || "", 180) || "[empty span]";
      markNoteModalSnippet.textContent = `Span: ${snippet}`;
    }

    if (typeof markNoteModal.showModal === "function") {
      markNoteModal.showModal();
    } else {
      markNoteModal.setAttribute("open", "open");
    }
    markNoteModalField.focus();
  }

  function closeMarkNoteModal() {
    if (!markNoteModal) {
      return;
    }
    if (markNoteModal.open && typeof markNoteModal.close === "function") {
      markNoteModal.close();
    } else {
      markNoteModal.removeAttribute("open");
    }
    activeMarkNoteContext = null;
  }

  function saveMarkNoteModal() {
    if (!activeMarkNoteContext || !markNoteModalField) {
      closeMarkNoteModal();
      return;
    }

    const { modelId, markType, index } = activeMarkNoteContext;
    const mark = getMark(modelId, markType, index);
    if (!mark) {
      closeMarkNoteModal();
      return;
    }

    const note = normalizeMarkNote(markNoteModalField.value || "", 320);
    mark.note = note;
    renderModel(modelId);
    setFeedback(note ? "Mark note saved." : "Mark note cleared.");
    closeMarkNoteModal();
  }

  rankRows.forEach((row) => {
    const modelId = String(row.dataset.candidateModel || "");
    const input = row.querySelector(`[data-error-json="${modelId}"]`);
    const sourceList = row.querySelector(`[data-source-mark-list="${modelId}"]`);
    const styleList = row.querySelector(`[data-style-mark-list="${modelId}"]`);
    const target = row.querySelector(`[data-target-annotator="${modelId}"]`);
    const rankInput = row.querySelector(`[data-rank-input="${modelId}"]`);
    const commentInput = row.querySelector(`[data-comment-input="${modelId}"]`);
    const commentButton = row.querySelector(`[data-action="open-comment"][data-comment-model="${modelId}"]`);
    const commentPreview = row.querySelector(`[data-comment-preview="${modelId}"]`);

    inputByModel.set(modelId, input);
    sourceListByModel.set(modelId, sourceList);
    styleListByModel.set(modelId, styleList);
    rankInputByModel.set(modelId, rankInput);
    commentInputByModel.set(modelId, commentInput);
    commentButtonByModel.set(modelId, commentButton);
    commentPreviewByModel.set(modelId, commentPreview);
    stateByModel.set(modelId, parseState(input?.value || ""));

    if (target) {
      targetByModel.set(modelId, target);
      tokenizeText(target);
    }

    rankControlsByModel.set(
      modelId,
      Array.from(document.querySelectorAll(`[data-rank-controls="${modelId}"]`)),
    );

    row.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) {
        return;
      }
      if (event.target.closest("button, textarea")) {
        return;
      }
      setActiveModel(modelId, { announce: false });
    });

    row.addEventListener("focusin", () => {
      setActiveModel(modelId, { announce: false });
    });

    target?.addEventListener("click", (event) => {
      const token = event.target.closest(".eval-word-token");
      if (!token) {
        return;
      }

      setActiveModel(modelId, { announce: false });

      if (
        pendingMarkState
        && pendingMarkState.kind === "style"
        && pendingMarkState.modelId === modelId
        && pendingMarkState.root === target
      ) {
        completePendingMark(token);
        return;
      }

      setPendingMark("style", modelId, target, token);
      setFeedback(`Style mark started for ${modelLabel(modelId)}. Click another target word to finish this span.`);
    });

    target?.addEventListener("mouseover", (event) => {
      const token = event.target.closest(".eval-word-token");
      if (!token) {
        return;
      }
      if (
        pendingMarkState
        && pendingMarkState.kind === "style"
        && pendingMarkState.modelId === modelId
        && pendingMarkState.root === target
      ) {
        previewPendingRange(token);
      }
    });

    renderModel(modelId);
    updateCommentButton(modelId);
  });

  tokenizeText(sourceRoot);
  setActiveModel(activeModelId, { announce: false });

  ensureInitialRanks();
  reorderByRank({ animate: false });
  updateRankUi();

  enableRankDragAndDrop();

  sourceRoot.addEventListener("click", (event) => {
    const token = event.target.closest(".eval-word-token");
    if (!token) {
      return;
    }

    if (
      pendingMarkState
      && pendingMarkState.kind === "source"
      && pendingMarkState.modelId === activeModelId
      && pendingMarkState.root === sourceRoot
    ) {
      completePendingMark(token);
      return;
    }

    setPendingMark("source", activeModelId, sourceRoot, token);
    setFeedback(`Source mark started for ${modelLabel(activeModelId)}. Click another source word to finish this span.`);
  });

  sourceRoot.addEventListener("mouseover", (event) => {
    const token = event.target.closest(".eval-word-token");
    if (!token) {
      return;
    }
    if (
      pendingMarkState
      && pendingMarkState.kind === "source"
      && pendingMarkState.modelId === activeModelId
      && pendingMarkState.root === sourceRoot
    ) {
      previewPendingRange(token);
    }
  });

  commentModal?.addEventListener("cancel", () => {
    activeCommentModelId = null;
  });

  markNoteModal?.addEventListener("cancel", () => {
    activeMarkNoteContext = null;
  });

  document.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-model-id]");
    if (removeButton) {
      const modelId = String(removeButton.dataset.removeModelId || "");
      const markType = String(removeButton.dataset.removeType || "");
      const index = Number(removeButton.dataset.removeIndex || -1);
      removeMark(modelId, markType, index);
      return;
    }

    const rankButton = event.target.closest("[data-rank-value][data-rank-model]");
    if (rankButton) {
      const modelId = String(rankButton.dataset.rankModel || "");
      const nextRank = parseRank(rankButton.dataset.rankValue);
      if (!modelId || !nextRank) {
        return;
      }
      setRankValue(modelId, nextRank);
      reorderByRank({ animate: true });
      updateRankUi();
      setFeedback(`Set ${modelLabel(modelId)} to rank ${nextRank}. Order synchronized.`);
      return;
    }

    const actionButton = event.target.closest("[data-action]");
    if (!actionButton) {
      return;
    }

    const action = String(actionButton.dataset.action || "");
    if (action === "auto-rank") {
      applyAutoRankSuggestion();
      return;
    }
    if (action === "open-comment") {
      openCommentModal(String(actionButton.dataset.commentModel || ""));
      return;
    }
    if (action === "close-comment") {
      closeCommentModal();
      return;
    }
    if (action === "save-comment") {
      saveCommentModal();
      return;
    }
    if (action === "edit-mark-note") {
      const modelId = String(actionButton.dataset.noteModelId || "");
      const markType = String(actionButton.dataset.noteType || "");
      const index = Number(actionButton.dataset.noteIndex || -1);
      if (!modelId || (markType !== "source" && markType !== "style") || index < 0) {
        return;
      }
      openMarkNoteModal(modelId, markType, index);
      return;
    }
    if (action === "close-mark-note") {
      closeMarkNoteModal();
      return;
    }
    if (action === "save-mark-note") {
      saveMarkNoteModal();
      return;
    }
  });
})();
