(function () {
  const sourceRoot = document.getElementById("evalSourceAnnotator");
  const candidateCards = Array.from(document.querySelectorAll("[data-candidate-model]"));
  const rankContainer = document.querySelector("[data-rank-container]");
  const feedbackNode = document.getElementById("evalInteractionFeedback");
  const referenceNode = document.getElementById("evalReferenceText");
  if (!sourceRoot || !candidateCards.length) {
    return;
  }

  const stateByModel = new Map();
  const inputByModel = new Map();
  const sourceListByModel = new Map();
  const styleListByModel = new Map();
  const targetByModel = new Map();
  const selectByModel = new Map();

  let activeModelId = String(candidateCards[0].dataset.candidateModel || "");
  let pendingMarkState = null;
  let draggedRankCard = null;

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

  function modelCard(modelId) {
    return rankContainer?.querySelector(`[data-candidate-model="${modelId}"]`) || null;
  }

  function modelLabel(modelId) {
    const card = modelCard(modelId);
    const title = card?.querySelector(".eval-card-title")?.textContent || "Translation";
    return normalizeSnippet(title, 40);
  }

  function compareWithTie(leftValue, rightValue, epsilon = 1e-6) {
    if (Math.abs(leftValue - rightValue) <= epsilon) {
      return 0;
    }
    return leftValue < rightValue ? -1 : 1;
  }

  function applyAutoRankSuggestion() {
    try {
      const sourceText = String(sourceRoot.dataset.rawText || sourceRoot.textContent || "");
      const referenceText = String(referenceNode?.textContent || "");
      const modelIds = Array.from(selectByModel.keys());
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
          sourceMarkCount,
          styleMarkCount,
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

      if (rankContainer) {
        sorted.forEach((item) => {
          const card = modelCard(item.modelId);
          if (card) {
            rankContainer.appendChild(card);
          }
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
        const select = selectByModel.get(item.modelId);
        if (select) {
          select.value = String(currentRank);
        }
      });

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
    if (!raw) {
      return { source_marks: [], style_marks: [] };
    }
    try {
      const parsed = JSON.parse(raw);
      return {
        source_marks: Array.isArray(parsed?.source_marks) ? parsed.source_marks : [],
        style_marks: Array.isArray(parsed?.style_marks) ? parsed.style_marks : [],
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
      container.innerHTML = '<span class="muted">No marks yet.</span>';
      return;
    }

    marks.forEach((mark, index) => {
      const row = document.createElement("div");
      row.className = "eval-mark-item";

      const snippet = document.createElement("code");
      snippet.textContent = normalizeSnippet(mark.text || "", 160) || "[empty]";
      row.appendChild(snippet);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "Remove";
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
    candidateCards.forEach((card) => {
      const current = String(card.dataset.candidateModel || "");
      card.dataset.activeSource = current === activeModelId ? "1" : "0";
    });
    renderHighlights();
    if (changed && options.announce !== false) {
      setFeedback(`Source-side marking active for ${modelLabel(activeModelId)} (last clicked card).`);
    }
  }

  function tokenizeText(root) {
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
          ? "Added source-side translation-error mark."
          : "Added target style-issue mark.",
      );
    } else {
      setFeedback("Mark already exists or was empty.");
    }
  }

  function applyRanksFromOrder() {
    if (!rankContainer) {
      return;
    }
    const cards = Array.from(rankContainer.querySelectorAll("[data-rank-card]"));
    cards.forEach((card, index) => {
      const modelId = String(card.dataset.candidateModel || "");
      const select = selectByModel.get(modelId);
      if (!select) {
        return;
      }
      select.value = String(index + 1);
    });
    refreshRankOrderBadges();
  }

  function refreshRankOrderBadges() {
    if (!rankContainer) {
      return;
    }
    const cards = Array.from(rankContainer.querySelectorAll("[data-rank-card]"));
    cards.forEach((card, index) => {
      const modelId = String(card.dataset.candidateModel || "");
      const badge = card.querySelector(`[data-order-badge="${modelId}"]`);
      if (badge) {
        badge.textContent = `Position: ${index + 1}`;
      }
    });
  }

  function enableRankDragAndDrop() {
    if (!rankContainer) {
      return;
    }

    rankContainer.addEventListener("dragstart", (event) => {
      const card = event.target.closest("[data-rank-card]");
      if (!card) {
        return;
      }
      draggedRankCard = card;
      card.classList.add("eval-card-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.candidateModel || "");
    });

    rankContainer.addEventListener("dragover", (event) => {
      if (!draggedRankCard) {
        return;
      }
      event.preventDefault();
      const target = event.target.closest("[data-rank-card]");
      if (!target || target === draggedRankCard) {
        return;
      }
      const rect = target.getBoundingClientRect();
      const insertBefore = event.clientY < rect.top + rect.height / 2;
      rankContainer.insertBefore(draggedRankCard, insertBefore ? target : target.nextSibling);
    });

    rankContainer.addEventListener("drop", (event) => {
      if (!draggedRankCard) {
        return;
      }
      event.preventDefault();
      applyRanksFromOrder();
      setFeedback("Updated rank order from drag-and-drop (left-to-right, top-to-bottom). You can still set ties manually.");
    });

    rankContainer.addEventListener("dragend", () => {
      if (draggedRankCard) {
        draggedRankCard.classList.remove("eval-card-dragging");
      }
      draggedRankCard = null;
    });
  }

  candidateCards.forEach((card) => {
    const modelId = String(card.dataset.candidateModel || "");
    const input = card.querySelector(`[data-error-json="${modelId}"]`);
    const sourceList = card.querySelector(`[data-source-mark-list="${modelId}"]`);
    const styleList = card.querySelector(`[data-style-mark-list="${modelId}"]`);
    const target = card.querySelector(`[data-target-annotator="${modelId}"]`);
    const rankSelect = card.querySelector(`[data-rank-select="${modelId}"]`);

    inputByModel.set(modelId, input);
    sourceListByModel.set(modelId, sourceList);
    styleListByModel.set(modelId, styleList);
    targetByModel.set(modelId, target);
    selectByModel.set(modelId, rankSelect);
    stateByModel.set(modelId, parseState(input?.value || ""));

    card.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) {
        return;
      }
      setActiveModel(modelId, { announce: false });
    });

    card.addEventListener("focusin", () => {
      setActiveModel(modelId, { announce: false });
    });

    tokenizeText(target);

    target.addEventListener("click", (event) => {
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

    target.addEventListener("mouseover", (event) => {
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

    rankSelect?.addEventListener("change", () => {
      setFeedback("Rank updated manually. Equal ranks are allowed.");
    });

    renderModel(modelId);
  });

  tokenizeText(sourceRoot);
  setActiveModel(activeModelId);
  enableRankDragAndDrop();
  applyRanksFromOrder();
  refreshRankOrderBadges();

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

  document.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-model-id]");
    if (removeButton) {
      const modelId = String(removeButton.dataset.removeModelId || "");
      const markType = String(removeButton.dataset.removeType || "");
      const index = Number(removeButton.dataset.removeIndex || -1);
      removeMark(modelId, markType, index);
      return;
    }

    const actionButton = event.target.closest("[data-action]");
    if (!actionButton) {
      return;
    }
    const action = actionButton.dataset.action;
    if (action === "auto-rank") {
      applyAutoRankSuggestion();
      return;
    }

  });
})();
