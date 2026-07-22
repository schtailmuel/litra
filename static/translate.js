const shell = document.querySelector(".translator-shell");
const token = shell.dataset.token;

// Handles subpath deployments such as /litra/t/<token>.
function getBasePath() {
  const path = window.location.pathname;
  const marker = `/t/${token}`;
  const tokenIndex = path.indexOf(marker);
  if (tokenIndex >= 0) {
    return path.slice(0, tokenIndex);
  }
  const genericIndex = path.indexOf("/t/");
  return genericIndex >= 0 ? path.slice(0, genericIndex) : "";
}

const pageBasePath = getBasePath();
const apiBase = pageBasePath
  ? `${pageBasePath}/api/t/${token}`
  : shell.dataset.apiBase || `/api/t/${token}`;
const translationsBase = pageBasePath
  ? `${pageBasePath}/t/${token}/translations`
  : shell.dataset.translationsBase || `/t/${token}/translations`;

function apiUrl(path) {
  return `${apiBase}${path}`;
}

const els = {
  translatorName: document.querySelector("#translatorName"),
  assignmentCount: document.querySelector("#assignmentCount"),
  languageCount: document.querySelector("#languageCount"),
  recentSubmissions: document.querySelector("#recentSubmissions"),
  segmentTitle: document.querySelector("#segmentTitle"),
  segmentMeta: document.querySelector("#segmentMeta"),
  sourceTabs: document.querySelector("#sourceTabs"),
  sourceText: document.querySelector("#sourceText"),
  sentenceEditor: document.querySelector("#sentenceEditor"),
  instructionPanel: document.querySelector("#instructionPanel"),
  instructions: document.querySelector("#instructions"),
  workbench: document.querySelector(".translation-workbench"),
  targetTextLabel: document.querySelector("#targetTextLabel"),
  targetText: document.querySelector("#targetText"),
  increaseTextFontButton: document.querySelector("#increaseTextFontButton"),
  textFontSizeSlider: document.querySelector("#textFontSizeSlider"),
  textFontSizeValue: document.querySelector("#textFontSizeValue"),
  targetInstructions: document.querySelector("#targetInstructions"),
  targetPreview: document.querySelector("#targetPreview"),
  commentList: document.querySelector("#commentList"),
  commentBody: document.querySelector("#commentBody"),
  postCommentButton: document.querySelector("#postCommentButton"),
  saveButton: document.querySelector("#saveButton"),
  nextAfterSaveButton: document.querySelector("#nextAfterSaveButton"),
  skipButton: document.querySelector("#skipButton"),
  flagSourceButton: document.querySelector("#flagSourceButton"),
  sourceFlagPanel: document.querySelector("#sourceFlagPanel"),
  sourceFlagNote: document.querySelector("#sourceFlagNote"),
  submitSourceFlagButton: document.querySelector("#submitSourceFlagButton"),
  cancelSourceFlagButton: document.querySelector("#cancelSourceFlagButton"),
  downloadSourceButton: document.querySelector("#downloadSourceButton"),
  downloadTranslationButton: document.querySelector("#downloadTranslationButton"),
  saveState: document.querySelector("#saveState"),
  conflictPanel: document.querySelector("#conflictPanel"),
  serverText: document.querySelector("#serverText"),
  draftText: document.querySelector("#draftText"),
  useServer: document.querySelector("#useServer"),
  overwriteServer: document.querySelector("#overwriteServer"),
};

const TEXT_FONT_SIZE_STORAGE_KEY = "litra.translator.textFontSizePx";
const TEXT_FONT_SIZE_MIN = 14;
const TEXT_FONT_SIZE_MAX = 26;

const state = {
  targetLanguage: "",
  currentSegment: null,
  dirty: false,
  conflict: null,
  draftTimer: null,
  draftSnapshot: "",
  legacyComment: "",
  activeSourceLanguage: "",
};

function clampTextFontSize(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) {
    return 16;
  }
  return Math.min(TEXT_FONT_SIZE_MAX, Math.max(TEXT_FONT_SIZE_MIN, parsed));
}

function applyTextFontSize(value) {
  const size = clampTextFontSize(value);
  shell.style.setProperty("--translation-font-size", `${size}px`);
  if (els.textFontSizeSlider) {
    els.textFontSizeSlider.value = String(size);
  }
  if (els.textFontSizeValue) {
    els.textFontSizeValue.textContent = `${size}px`;
  }
  try {
    window.localStorage.setItem(TEXT_FONT_SIZE_STORAGE_KEY, String(size));
  } catch (error) {
    // Local storage can be unavailable in hardened browser modes.
  }
}

function initTextFontSizeControl() {
  let savedSize = 16;
  try {
    savedSize = window.localStorage.getItem(TEXT_FONT_SIZE_STORAGE_KEY) || savedSize;
  } catch (error) {
    savedSize = 16;
  }
  applyTextFontSize(savedSize);
}

function setSaveState(text, mode = "") {
  els.saveState.textContent = text;
  els.saveState.className = `save-state ${mode}`.trim();
}

function setEditorEnabled(enabled) {
  els.targetText.disabled = !enabled;
  els.targetInstructions.disabled = !enabled;
  els.sentenceEditor.querySelectorAll("textarea[data-target-line]").forEach((textarea) => {
    textarea.disabled = !enabled;
  });
  els.commentBody.disabled = !enabled;
  els.postCommentButton.disabled = !enabled;
  els.saveButton.disabled = !enabled;
  els.nextAfterSaveButton.disabled = !enabled;
  els.skipButton.disabled = !enabled;
  els.flagSourceButton.disabled = !enabled;
  els.submitSourceFlagButton.disabled = !enabled;
  els.downloadSourceButton.disabled = !enabled;
  els.downloadTranslationButton.disabled = !enabled;
}

function safeFilePart(value, fallback) {
  const cleaned = String(value || "")
    .trim()
    .replace(/[^\w.-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || fallback;
}

function downloadTextFile(filename, text) {
  const blob = new Blob([text || ""], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function currentSegmentFilename(kind) {
  const segment = state.currentSegment;
  const label = safeFilePart(
    segment.identifier || `segment-${segment.ordinal}`,
    `segment-${segment.id}`
  );
  return `${label}-${kind}.txt`;
}

function downloadCurrentSource() {
  if (!state.currentSegment) {
    return;
  }
  const source = activeSourceVariant();
  downloadTextFile(
    currentSegmentFilename(`source-${safeFilePart(source.source_language, "source")}`),
    source.source_text || ""
  );
}

function downloadCurrentTranslation() {
  if (!state.currentSegment) {
    return;
  }
  const payload = collectTranslationPayload();
  const parts = [payload.target_text];
  if (payload.target_instructions) {
    parts.push("", "Instruction Translation:", payload.target_instructions);
  }
  downloadTextFile(currentSegmentFilename("translation"), parts.join("\n"));
}

function activeEditorView(segment = state.currentSegment) {
  return segment?.editor?.view || "standard";
}

function sourceVariants(segment = state.currentSegment) {
  if (segment?.source_variants?.length) {
    return segment.source_variants;
  }
  if (!segment) {
    return [];
  }
  return [
    {
      source_language: segment.source_language || "",
      source_text: segment.source_text || "",
      instructions: segment.instructions || "",
      source_lines: segment.source_lines || String(segment.source_text || "").split(/\r?\n/),
      is_default: true,
    },
  ];
}

function activeSourceVariant(segment = state.currentSegment) {
  const variants = sourceVariants(segment);
  if (!variants.length) {
    return {
      source_language: segment?.source_language || "",
      source_text: segment?.source_text || "",
      instructions: segment?.instructions || "",
      source_lines: segment?.source_lines || [],
      is_default: true,
    };
  }
  const active = state.activeSourceLanguage
    ? variants.find(
        (variant) =>
          String(variant.source_language || "").toLowerCase() ===
          state.activeSourceLanguage.toLowerCase()
      )
    : null;
  const selected = active || variants[0];
  state.activeSourceLanguage = selected.source_language || "";
  return selected;
}

function collectTranslationPayload() {
  if (activeEditorView() === "sentence_list") {
    const lines = [...els.sentenceEditor.querySelectorAll("textarea[data-target-line]")]
      .map((textarea) => textarea.value);
    return {
      target_text: lines.join("\n"),
      target_instructions: "",
    };
  }
  return {
    target_text: els.targetText.value,
    target_instructions: els.instructionPanel.classList.contains("has-target-instruction")
      ? els.targetInstructions.value
      : "",
  };
}

function setTranslationPayload(targetText = "", targetInstructions = "") {
  if (activeEditorView() === "sentence_list") {
    const targetLines = String(targetText || "").split(/\r?\n/);
    els.sentenceEditor.querySelectorAll("textarea[data-target-line]").forEach((textarea, index) => {
      textarea.value = targetLines[index] || "";
    });
    return;
  }
  els.targetText.value = targetText || "";
  els.targetInstructions.value = targetInstructions || "";
  els.targetPreview.innerHTML = renderMarkdown(els.targetText.value);
  renderInstructionArea();
}

function translationSnapshot(payload = collectTranslationPayload()) {
  return [
    payload.target_text,
    "---instructions---",
    payload.target_instructions,
    "---comment---",
    state.legacyComment,
  ].join("\n");
}

function clearFormatView() {
  els.workbench.classList.remove("sentence-list-view", "dual-field-view");
  els.instructionPanel.classList.remove("has-target-instruction");
  els.sourceText.classList.remove("hidden");
  els.sentenceEditor.classList.add("hidden");
  els.sentenceEditor.innerHTML = "";
  els.targetText.parentElement.classList.remove("hidden");
  els.targetTextLabel.textContent = "Translation";
}

function renderSentenceEditor(segment, preservedTargetLines = null) {
  const source = activeSourceVariant(segment);
  const sourceLines = source.source_lines?.length
    ? source.source_lines
    : String(source.source_text || "").split(/\r?\n/);
  const targetLines = Array.isArray(preservedTargetLines)
    ? preservedTargetLines
    : (segment.draft_text || segment.target_text || "").split(/\r?\n/);
  els.sentenceEditor.innerHTML = "";
  sourceLines.forEach((line, index) => {
    const row = document.createElement("div");
    row.className = "sentence-row";

    const sourceCell = document.createElement("label");
    sourceCell.className = "sentence-cell";
    const sourceLabel = document.createElement("span");
    sourceLabel.textContent = `Source ${index + 1}`;
    const sourceTextarea = document.createElement("textarea");
    sourceTextarea.readOnly = true;
    sourceTextarea.rows = 2;
    sourceTextarea.value = line;
    sourceCell.append(sourceLabel, sourceTextarea);

    const targetCell = document.createElement("label");
    targetCell.className = "sentence-cell";
    const targetLabel = document.createElement("span");
    targetLabel.textContent = `Translation ${index + 1}`;
    const targetTextarea = document.createElement("textarea");
    targetTextarea.rows = 2;
    targetTextarea.dataset.targetLine = String(index);
    targetTextarea.value = targetLines[index] || "";
    targetTextarea.addEventListener("input", markDirty);
    targetCell.append(targetLabel, targetTextarea);

    row.append(sourceCell, targetCell);
    els.sentenceEditor.append(row);
  });
}

function applyEditorView(segment, preserveTarget = false) {
  const preservedTargetLines = preserveTarget
    ? [...els.sentenceEditor.querySelectorAll("textarea[data-target-line]")].map(
        (textarea) => textarea.value
      )
    : null;
  clearFormatView();
  const view = activeEditorView(segment);
  if (view === "sentence_list") {
    els.workbench.classList.add("sentence-list-view");
    els.sourceText.classList.add("hidden");
    els.sentenceEditor.classList.remove("hidden");
    renderSentenceEditor(segment, preservedTargetLines);
    return;
  }
  if (view === "dual_field") {
    els.targetTextLabel.textContent = "Text Translation";
    els.instructionPanel.classList.add("has-target-instruction");
  }
}

function instructionTextForSegment(segment) {
  if (!segment) {
    return "";
  }
  const source = activeSourceVariant(segment);
  const defaultInstruction = `Translate from ${source.source_language || segment.source_language} to ${state.targetLanguage}.`;
  const extraInstructions = source.instructions || segment.instructions || "";
  return extraInstructions
    ? `${defaultInstruction}\n\n${extraInstructions}`
    : defaultInstruction;
}

function translatedInstructionForSegment(segment) {
  if (!segment || activeEditorView(segment) !== "dual_field") {
    return "";
  }
  return segment.draft_instructions || segment.target_instructions || els.targetInstructions.value || "";
}

function renderInstructionArea(segment = state.currentSegment) {
  const instructionText = instructionTextForSegment(segment);
  const translatedInstruction = translatedInstructionForSegment(segment).trim();
  let html = renderMarkdown(instructionText);
  if (translatedInstruction) {
    html += `<div class="instruction-divider">---</div>`;
    html += `<div class="instruction-translated-content">${renderMarkdown(translatedInstruction)}</div>`;
  }
  els.instructions.innerHTML = html;
}

function renderSourceTabs(segment) {
  const variants = sourceVariants(segment);
  els.sourceTabs.innerHTML = "";
  els.sourceTabs.classList.toggle("hidden", variants.length < 2);
  variants.forEach((variant) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = variant.source_language || "Source";
    button.className =
      String(variant.source_language || "").toLowerCase() ===
      String(state.activeSourceLanguage || "").toLowerCase()
        ? "active"
        : "";
    button.addEventListener("click", () => {
      state.activeSourceLanguage = variant.source_language || "";
      renderSourceArea(segment, true);
    });
    els.sourceTabs.append(button);
  });
}

function renderSourceArea(segment, preserveTarget = false) {
  const source = activeSourceVariant(segment);
  const status = (segment.status || "untranslated").replace("_", " ");
  els.segmentMeta.textContent = `${source.source_language || segment.source_language} to ${state.targetLanguage} | ${status} | version ${segment.version}`;
  els.sourceText.innerHTML = renderMarkdown(source.source_text || "");
  renderInstructionArea(segment);
  renderSourceTabs(segment);
  applyEditorView(segment, preserveTarget);
}

function updateAssignmentStatus(data) {
  const used = data.submitted_assignments ?? 0;
  const limit = data.assignment_limit ?? 0;
  els.assignmentCount.textContent = `${used} / ${limit}`;
  if ("completed_segments" in data && "total_segments" in data) {
    els.languageCount.textContent = `${data.completed_segments} / ${data.total_segments}`;
  }
  if (data.translator_name) {
    els.translatorName.textContent = data.translator_name;
  }
}

function updateRecentSubmissions(rows = []) {
  if (!els.recentSubmissions) {
    return;
  }
  els.recentSubmissions.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "None yet.";
    els.recentSubmissions.append(empty);
    return;
  }
  rows.forEach((row) => {
    const link = document.createElement("a");
    link.className = "recent-shortcut";
    link.href = `${translationsBase}/${row.id}?back=work`;

    const title = document.createElement("strong");
    title.textContent = row.identifier || `Segment ${row.id}`;
    const detail = document.createElement("span");
    detail.textContent = row.updated_at || "submitted";

    link.append(title, detail);
    els.recentSubmissions.append(link);
  });
}

function commentTimestamp(comment) {
  return comment.created_at || comment.resolved_at || "";
}

function renderComments(comments = [], message = "No comments yet.") {
  els.commentList.innerHTML = "";
  const rows = [...comments].sort((a, b) => {
    const stamp = commentTimestamp(a).localeCompare(commentTimestamp(b));
    return stamp || ((a.id || 0) - (b.id || 0));
  });

  if (!rows.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = message;
    els.commentList.append(empty);
    return;
  }

  rows.forEach((comment) => {
    const item = document.createElement("article");
    item.className = "comment-message";
    if (comment.resolved) {
      item.classList.add("resolved");
    }
    if (comment.legacy) {
      item.classList.add("legacy");
    }

    const meta = document.createElement("div");
    const author = document.createElement("strong");
    author.textContent = comment.created_by || comment.role || "translator";
    const detail = document.createElement("small");
    const role = comment.legacy ? "saved note" : comment.role || "comment";
    detail.textContent = [role, commentTimestamp(comment)].filter(Boolean).join(" | ");
    meta.append(author, detail);

    const body = document.createElement("p");
    body.textContent = comment.body || "";
    item.append(meta, body);

    if (comment.can_delete && !comment.legacy) {
      const actions = document.createElement("div");
      actions.className = "comment-message-actions";
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "secondary danger-text-button";
      deleteButton.dataset.commentDelete = String(comment.id);
      deleteButton.textContent = "Delete";
      actions.append(deleteButton);
      item.append(actions);
    }

    els.commentList.append(item);
  });
}

function segmentComments(segment) {
  const comments = [...(segment.comments || [])];
  const legacy = segment.comment || segment.draft_comment || "";
  if (legacy.trim()) {
    comments.push({
      id: -1,
      role: "translator",
      body: legacy,
      created_by: segment.updated_by || segment.draft_updated_by || els.translatorName.textContent,
      created_at: segment.updated_at || segment.draft_updated_at || "",
      resolved: 0,
      legacy: true,
    });
  }
  return comments;
}

function clearEditor(title, message) {
  clearTimeout(state.draftTimer);
  state.currentSegment = null;
  state.dirty = false;
  state.conflict = null;
  state.draftSnapshot = "";
  state.legacyComment = "";
  state.activeSourceLanguage = "";
  els.segmentTitle.textContent = title;
  els.segmentMeta.textContent = "";
  els.sourceText.textContent = message;
  els.sourceTabs.innerHTML = "";
  els.sourceTabs.classList.add("hidden");
  clearFormatView();
  els.instructions.innerHTML = "";
  els.targetText.value = "";
  els.targetInstructions.value = "";
  els.targetPreview.innerHTML = "";
  els.commentBody.value = "";
  renderComments([], "Select a text to see comments.");
  els.sourceFlagNote.value = "";
  els.sourceFlagPanel.classList.add("hidden");
  els.conflictPanel.classList.add("hidden");
  setEditorEnabled(false);
}

function renderSegment(segment) {
  clearTimeout(state.draftTimer);
  state.currentSegment = segment;
  state.dirty = false;
  state.conflict = null;
  els.segmentTitle.textContent = segment.identifier || `Segment ${segment.ordinal}`;
  activeSourceVariant(segment);
  renderSourceArea(segment);
  els.instructionPanel.open = true;
  setTranslationPayload(
    segment.draft_text || segment.target_text || "",
    segment.draft_instructions || segment.target_instructions || ""
  );
  state.legacyComment = segment.draft_comment || segment.comment || "";
  els.commentBody.value = "";
  renderComments(segmentComments(segment));
  els.sourceFlagNote.value = "";
  els.sourceFlagPanel.classList.add("hidden");
  state.draftSnapshot = translationSnapshot();
  els.conflictPanel.classList.add("hidden");
  setEditorEnabled(true);
  setSaveState("Claimed");
}

async function loadStatus(refreshOnly = false, autoClaim = false) {
  if (!refreshOnly) {
    setSaveState("Loading");
  }
  const response = await fetch(apiUrl("/status"));
  if (!response.ok) {
    clearEditor("Unavailable", "This translator link could not be loaded.");
    setSaveState("Load failed", "conflict");
    return;
  }
  const data = await response.json();
  state.targetLanguage = data.target_language;
  updateAssignmentStatus(data);
  updateRecentSubmissions(data.recent_submissions || []);
  if (!state.currentSegment) {
    if (autoClaim) {
      await getNextSegment();
      return;
    }
    clearEditor("Ready", "Refresh the page to claim your next available segment.");
    setSaveState("Ready");
  }
}

async function getNextSegment() {
  if (state.dirty && !window.confirm("Discard unsaved changes and get another text?")) {
    return;
  }
  setSaveState("Claiming");
  const response = await fetch(apiUrl("/next"), { method: "POST" });
  if (!response.ok) {
    setSaveState("Claim failed", "conflict");
    return;
  }
  const data = await response.json();
  updateAssignmentStatus(data);
  if (data.status === "ok") {
    renderSegment(data.segment);
    return;
  }
  if (data.status === "limit_reached") {
    clearEditor("Assignment limit reached", "Ask the project manager to assign more texts.");
    setSaveState("Limit reached", "conflict");
    return;
  }
  clearEditor("No texts available", data.message || "All available texts are already translated or claimed.");
  setSaveState("Done", "saved");
}

async function saveSegment(loadNext = false, force = false) {
  const segment = state.currentSegment;
  if (!segment) {
    return;
  }

  const draft = collectTranslationPayload();
  const comment = state.legacyComment;
  setSaveState("Saving");
  clearTimeout(state.draftTimer);
  const response = await fetch(apiUrl(`/segments/${segment.id}`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      target_text: draft.target_text,
      target_instructions: draft.target_instructions,
      comment,
      version: force && state.conflict ? state.conflict.server.version : segment.version,
    }),
  });

  if (response.status === 409) {
    const data = await response.json();
    state.conflict = {
      server: data.server,
      draft,
      comment,
    };
    els.serverText.value = [
      data.server.target_text || "",
      data.server.target_instructions ? `\nInstruction Translation:\n${data.server.target_instructions}` : "",
    ].join("");
    els.draftText.value = [
      draft.target_text,
      draft.target_instructions ? `\nInstruction Translation:\n${draft.target_instructions}` : "",
    ].join("");
    els.conflictPanel.classList.remove("hidden");
    setSaveState("Conflict", "conflict");
    return;
  }

  if (!response.ok) {
    setSaveState("Save failed", "conflict");
    return;
  }

  const data = await response.json();
  updateAssignmentStatus(data);
  segment.target_text = draft.target_text;
  segment.draft_text = "";
  segment.target_instructions = draft.target_instructions;
  segment.draft_instructions = "";
  segment.comment = comment;
  segment.draft_comment = "";
  segment.version = data.version;
  segment.status = data.translation_status || "submitted";
  segment.updated_by = data.updated_by;
  segment.updated_at = data.updated_at;
  state.draftSnapshot = translationSnapshot(draft);
  state.dirty = false;
  state.conflict = null;
  els.conflictPanel.classList.add("hidden");
  setSaveState("Saved", "saved");

  await loadStatus(true);
  if (loadNext) {
    await getNextSegment();
  }
}

async function saveDraft() {
  const segment = state.currentSegment;
  if (!segment || els.saveButton.disabled) {
    return;
  }
  const draft = collectTranslationPayload();
  const comment = state.legacyComment;
  const snapshot = translationSnapshot(draft);
  if (snapshot === state.draftSnapshot) {
    return;
  }
  const response = await fetch(apiUrl(`/segments/${segment.id}/draft`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      target_text: draft.target_text,
      target_instructions: draft.target_instructions,
      comment,
    }),
  });
  if (!response.ok) {
    setSaveState("Draft failed", "conflict");
    return;
  }
  const data = await response.json();
  segment.draft_text = draft.target_text;
  segment.draft_instructions = draft.target_instructions;
  segment.draft_comment = comment;
  segment.status = data.translation_status || segment.status || "draft";
  state.draftSnapshot = snapshot;
  state.dirty = false;
  setSaveState("Draft saved", "saved");
}

function scheduleDraftSave() {
  clearTimeout(state.draftTimer);
  state.draftTimer = setTimeout(saveDraft, 900);
}

async function skipSegment() {
  const segment = state.currentSegment;
  if (!segment) {
    return;
  }
  if (state.dirty && !window.confirm("Skip this text and discard unsaved changes?")) {
    return;
  }

  setSaveState("Skipping");
  clearTimeout(state.draftTimer);
  const response = await fetch(apiUrl(`/segments/${segment.id}/skip`), {
    method: "POST",
  });
  if (!response.ok) {
    setSaveState("Skip failed", "conflict");
    return;
  }

  const data = await response.json();
  updateAssignmentStatus(data);
  clearEditor("Skipped", "Claiming another available text...");
  await getNextSegment();
}

async function submitSourceFlag() {
  const segment = state.currentSegment;
  if (!segment) {
    return;
  }
  const note = els.sourceFlagNote.value.trim();
  if (!note) {
    setSaveState("Flag note required", "conflict");
    return;
  }

  setSaveState("Flagging source");
  const response = await fetch(apiUrl(`/segments/${segment.id}/source-flag`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ note }),
  });
  if (!response.ok) {
    setSaveState("Flag failed", "conflict");
    return;
  }
  const data = await response.json();
  segment.source_flags = data.source_flags || [];
  els.sourceFlagNote.value = "";
  els.sourceFlagPanel.classList.add("hidden");
  setSaveState("Source flagged", "saved");
}

async function submitComment() {
  const segment = state.currentSegment;
  if (!segment) {
    return;
  }
  const body = els.commentBody.value.trim();
  if (!body) {
    setSaveState("Comment required", "conflict");
    return;
  }

  els.postCommentButton.disabled = true;
  setSaveState("Posting comment");
  const response = await fetch(apiUrl(`/segments/${segment.id}/comments`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ body }),
  });
  els.postCommentButton.disabled = false;

  if (!response.ok) {
    setSaveState("Comment failed", "conflict");
    return;
  }

  const data = await response.json();
  segment.comments = data.comments || [];
  els.commentBody.value = "";
  renderComments(segmentComments(segment));
  setSaveState("Comment posted", "saved");
}

async function deleteComment(commentId) {
  const segment = state.currentSegment;
  if (!segment || !commentId) {
    return;
  }
  if (!window.confirm("Delete this comment?")) {
    return;
  }

  setSaveState("Deleting comment");
  const response = await fetch(apiUrl(`/segments/${segment.id}/comments/${commentId}`), {
    method: "DELETE",
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    setSaveState(data.message || "Delete failed", "conflict");
    return;
  }

  const data = await response.json();
  segment.comments = data.comments || [];
  renderComments(segmentComments(segment));
  setSaveState("Comment deleted", "saved");
}

function markDirty() {
  state.dirty = true;
  els.targetPreview.innerHTML = renderMarkdown(els.targetText.value);
  setSaveState("Draft unsaved");
  scheduleDraftSave();
}

els.targetText.addEventListener("input", markDirty);
els.targetInstructions.addEventListener("input", markDirty);

els.saveButton.addEventListener("click", () => saveSegment(false));
els.nextAfterSaveButton.addEventListener("click", () => saveSegment(true));
els.skipButton.addEventListener("click", skipSegment);
els.postCommentButton.addEventListener("click", submitComment);
els.commentList.addEventListener("click", (event) => {
  const deleteButton = event.target.closest("[data-comment-delete]");
  if (deleteButton) {
    deleteComment(deleteButton.dataset.commentDelete);
  }
});
els.flagSourceButton.addEventListener("click", () => {
  els.sourceFlagPanel.classList.toggle("hidden");
});
els.submitSourceFlagButton.addEventListener("click", submitSourceFlag);
els.cancelSourceFlagButton.addEventListener("click", () => {
  els.sourceFlagNote.value = "";
  els.sourceFlagPanel.classList.add("hidden");
});
els.downloadSourceButton.addEventListener("click", downloadCurrentSource);
els.downloadTranslationButton.addEventListener("click", downloadCurrentTranslation);
els.textFontSizeSlider.addEventListener("input", (event) => {
  applyTextFontSize(event.target.value);
});
els.increaseTextFontButton.addEventListener("click", () => {
  applyTextFontSize(clampTextFontSize(els.textFontSizeSlider.value) + 1);
});

els.useServer.addEventListener("click", () => {
  if (!state.conflict || !state.currentSegment) {
    return;
  }
  state.currentSegment.target_text = state.conflict.server.target_text;
  state.currentSegment.target_instructions = state.conflict.server.target_instructions || "";
  state.currentSegment.comment = state.conflict.server.comment || "";
  state.currentSegment.draft_instructions = "";
  state.currentSegment.draft_comment = "";
  state.currentSegment.version = state.conflict.server.version;
  renderSegment(state.currentSegment);
});

els.overwriteServer.addEventListener("click", () => {
  if (!state.conflict || !state.currentSegment) {
    return;
  }
  state.currentSegment.version = state.conflict.server.version;
  setTranslationPayload(
    state.conflict.draft.target_text,
    state.conflict.draft.target_instructions
  );
  state.legacyComment = state.conflict.comment;
  state.dirty = true;
  saveSegment(false, true);
});

initTextFontSizeControl();
loadStatus(false, true);
attachMarkdownEditor("targetText", "targetPreview");
