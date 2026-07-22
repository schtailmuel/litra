const jsonlInput = document.querySelector("#jsonlInput");
const importFormatSelect = document.querySelector("#importFormatSelect");
const fileType = document.querySelector("#fileType");
const rowsPath = document.querySelector("#rowsPath");
const schemaStatus = document.querySelector("#schemaStatus");
const sourceTextKey = document.querySelector("#sourceTextKey");
const sourceTextIsList = document.querySelector("#sourceTextIsList");
const instructionKey = document.querySelector("#instructionKey");
const instructionKeyCustom = document.querySelector("#instructionKeyCustom");
const sourceLanguageKey = document.querySelector("#sourceLanguageKey");
const identifierKey = document.querySelector("#identifierKey");
const sourceLanguageManual = document.querySelector("#sourceLanguageManual");
const manualSourceLanguage = document.querySelector("#manualSourceLanguage");
const hasSeedTranslation = document.querySelector("#hasSeedTranslation");
const targetTextKey = document.querySelector("#targetTextKey");
const targetTextKeyCustom = document.querySelector("#targetTextKeyCustom");
const targetTextIsList = document.querySelector("#targetTextIsList");
const targetLanguageKey = document.querySelector("#targetLanguageKey");
const translatedInstructionKey = document.querySelector("#translatedInstructionKey");
const translatedInstructionKeyCustom = document.querySelector("#translatedInstructionKeyCustom");
const targetLanguageName = document.querySelector("#targetLanguageName");
const seedTranslationFields = document.querySelector("#seedTranslationFields");
const schemaSummary = document.querySelector("#schemaSummary");
const sampleRow = document.querySelector("#sampleRow");
const importFormats = window.importFormats || [];

const sourceTextCandidates = ["source_text", "src_text", "text", "sentences", "content", "body", "prompt"];
const instructionCandidates = [
  "instructions",
  "instruction",
  "source_instruction",
  "source_instructions",
  "response",
];
const sourceLanguageCandidates = [
  "source_language",
  "source_lang",
  "src_lang",
  "language",
  "lang",
  "locale",
];
const identifierCandidates = ["identifier", "message_id", "id", "text_id", "segment_id", "uid", "key"];
const targetTextCandidates = [
  "target_text",
  "tgt_text",
  "translation",
  "translated_text",
  "translated_prompt",
];
const translatedInstructionCandidates = [
  "translated_instruction",
  "translated_instructions",
  "translated_response",
  "edit_translation",
  "target_instruction",
  "target_instructions",
  "tgt_instruction",
  "tgt_instructions",
];

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function valuePreview(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value).slice(0, 90);
  }
  return String(value).slice(0, 90);
}

function pickKey(keys, preferred, fallback = "") {
  const lowerLookup = new Map(keys.map((key) => [key.toLowerCase(), key]));
  for (const candidate of preferred) {
    if (lowerLookup.has(candidate)) {
      return lowerLookup.get(candidate);
    }
  }
  return fallback;
}

function addOption(select, value, label, selectedValue) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label || value;
  option.selected = value === selectedValue;
  select.append(option);
}

function fillSelect(select, keys, selectedValue, extraOptions = []) {
  select.innerHTML = "";
  extraOptions.forEach((item) => addOption(select, item.value, item.label, selectedValue));
  const optionValues = new Set(extraOptions.map((item) => item.value));
  keys.forEach((key) => {
    if (!optionValues.has(key)) {
      addOption(select, key, key, selectedValue);
      optionValues.add(key);
    }
  });
  if (selectedValue && !optionValues.has(selectedValue)) {
    addOption(select, selectedValue, selectedValue, selectedValue);
  }
  select.value = selectedValue;
}

function setCustomKey(input, value, emptyValues = []) {
  input.value = emptyValues.includes(value) ? "" : value || "";
}

function getPath(item, path) {
  if (!path) {
    return item;
  }
  return path.split(".").filter(Boolean).reduce((value, part) => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value[part];
    }
    return undefined;
  }, item);
}

function firstArrayPath(item) {
  if (Array.isArray(item)) {
    return "";
  }
  if (!item || typeof item !== "object") {
    return "";
  }
  const entry = Object.entries(item).find(([, value]) => Array.isArray(value));
  return entry ? entry[0] : "";
}

function rowPaths(row, prefix = "") {
  const paths = [];
  Object.entries(row || {}).forEach(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    paths.push(path);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      rowPaths(value, path).forEach((childPath) => paths.push(childPath));
    }
  });
  return paths;
}

function parseJsonSample(text, configuredRowsPath = "") {
  const parsed = JSON.parse(text);
  const detectedRowsPath = configuredRowsPath || firstArrayPath(parsed);
  const rowsValue = detectedRowsPath ? getPath(parsed, detectedRowsPath) : parsed;
  const rows = Array.isArray(rowsValue) ? rowsValue : [rowsValue];
  return {
    rows: rows.filter((row) => row && typeof row === "object" && !Array.isArray(row)).slice(0, 25),
    errors: [],
    detectedRowsPath,
  };
}

function parseJsonlSample(text) {
  const rows = [];
  const errors = [];
  for (const [index, rawLine] of text.split(/\r?\n/).entries()) {
    const line = rawLine.trim();
    if (!line) {
      continue;
    }
    try {
      const parsed = JSON.parse(line);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        rows.push(parsed);
      } else {
        errors.push(`Line ${index + 1} is not a JSON object.`);
      }
    } catch (error) {
      errors.push(`Line ${index + 1}: ${error.message}`);
    }
    if (rows.length >= 25 || errors.length >= 3) {
      break;
    }
  }
  return { rows, errors, detectedRowsPath: "" };
}

function parseSample(text, selectedFileType, configuredRowsPath = "") {
  if (selectedFileType === "json") {
    try {
      return parseJsonSample(text, configuredRowsPath);
    } catch (error) {
      return { rows: [], errors: [error.message], detectedRowsPath: configuredRowsPath };
    }
  }
  return parseJsonlSample(text);
}

function keyStats(rows) {
  const stats = new Map();
  rows.forEach((row) => {
    rowPaths(row).forEach((key) => {
      const value = getPath(row, key);
      if (!stats.has(key)) {
        stats.set(key, { key, count: 0, example: "", isList: false });
      }
      const item = stats.get(key);
      item.count += 1;
      if (!item.example) {
        item.example = valuePreview(value);
      }
      if (Array.isArray(value)) {
        item.isList = true;
      }
    });
  });
  return [...stats.values()].sort((a, b) => a.key.localeCompare(b.key));
}

function renderSummary(stats, rowCount) {
  if (!stats.length) {
    schemaSummary.innerHTML = `
      <div>
        <strong>No object keys found</strong>
        <span>Check that the selected file type matches the uploaded file.</span>
      </div>
    `;
    return;
  }
  schemaSummary.innerHTML = stats
    .map(
      (item) => `
        <div>
          <strong>${escapeHtml(item.key)}</strong>
          <span>${item.count} / ${rowCount} sampled rows${item.isList ? " · list" : ""}</span>
          <small>${escapeHtml(item.example)}</small>
        </div>
      `
    )
    .join("");
}

function setManualLanguage(enabled) {
  manualSourceLanguage.disabled = !enabled;
  sourceLanguageKey.disabled = enabled;
  if (enabled) {
    manualSourceLanguage.focus();
  }
}

function setSeedTranslation(enabled) {
  seedTranslationFields.dataset.active = String(enabled);
  targetTextKey.disabled = false;
  translatedInstructionKey.disabled = false;
  targetLanguageKey.disabled = false;
  targetLanguageName.disabled = false;
  targetTextKey.required = enabled;
  targetLanguageName.required = enabled && !targetLanguageKey.value;
}

function selectedFormat() {
  const id = Number(importFormatSelect.value || 0);
  return importFormats.find((item) => Number(item.id) === id);
}

function applyFormat(format, keys = []) {
  if (!format) {
    return;
  }
  fileType.value = format.file_type || "jsonl";
  rowsPath.value = format.rows_path || "";
  sourceTextIsList.checked = Boolean(format.source_text_is_list);
  targetTextIsList.checked = Boolean(format.target_text_is_list);
  sourceLanguageManual.checked = Boolean(format.manual_source_language);
  manualSourceLanguage.value = format.manual_source_language || "";
  hasSeedTranslation.checked = Boolean(format.has_seed_translation);
  targetLanguageName.value = format.target_language_name || "";

  fillSelect(sourceTextKey, keys, format.source_text_path || "", [
    { value: "", label: "Choose source text path" },
  ]);
  fillSelect(instructionKey, keys, format.instruction_path || "__none__", [
    { value: "__none__", label: "No instruction path" },
  ]);
  fillSelect(sourceLanguageKey, keys, format.source_language_path || "", [
    { value: "", label: "Choose source language path" },
  ]);
  fillSelect(identifierKey, keys, format.identifier_path || "__auto__", [
    { value: "__auto__", label: "Generate IDs from row order" },
  ]);
  fillSelect(targetTextKey, keys, format.target_text_path || "", [
    { value: "", label: "No translation text path" },
  ]);
  fillSelect(targetLanguageKey, keys, format.target_language_path || "", [
    { value: "", label: "No translation language path" },
  ]);
  fillSelect(translatedInstructionKey, keys, format.translated_instruction_path || "__none__", [
    { value: "__none__", label: "No translated instruction path" },
  ]);

  setCustomKey(instructionKeyCustom, instructionKey.value, ["__none__"]);
  setCustomKey(targetTextKeyCustom, targetTextKey.value, [""]);
  setCustomKey(translatedInstructionKeyCustom, translatedInstructionKey.value, ["__none__"]);
  setManualLanguage(sourceLanguageManual.checked);
  setSeedTranslation(hasSeedTranslation.checked);
}

async function detectSchema(file) {
  if (!file) {
    return;
  }
  schemaStatus.textContent = "Reading file sample...";
  const format = selectedFormat();
  const fileName = file.name.toLowerCase();
  if (format) {
    fileType.value = format.file_type || fileType.value;
    rowsPath.value = format.rows_path || rowsPath.value;
  } else if (fileName.endsWith(".json")) {
    fileType.value = "json";
  }
  let text = fileType.value === "json"
    ? await file.text()
    : await file.slice(0, 256 * 1024).text();
  let result = parseSample(
    text,
    fileType.value,
    rowsPath.value.trim()
  );
  if (!result.rows.length && fileType.value === "jsonl") {
    const fullText = text.length < file.size ? await file.text() : text;
    const jsonResult = parseSample(fullText, "json", rowsPath.value.trim());
    if (jsonResult.rows.length) {
      fileType.value = "json";
      text = fullText;
      result = jsonResult;
    }
  }
  const { rows, errors, detectedRowsPath } = result;
  if (!rows.length) {
    schemaStatus.textContent = errors[0] || "No valid rows found.";
    renderSummary([], 0);
    return;
  }
  if (!rowsPath.value.trim() && detectedRowsPath) {
    rowsPath.value = detectedRowsPath;
  }

  const stats = keyStats(rows);
  const keys = stats.map((item) => item.key);
  const detectedSourceText = pickKey(keys, sourceTextCandidates, keys[0] || "");
  const detectedInstruction = pickKey(keys, instructionCandidates, "__none__");
  const detectedSourceLanguage = pickKey(keys, sourceLanguageCandidates);
  const detectedIdentifier = pickKey(keys, identifierCandidates, "__auto__");
  const detectedTargetText = pickKey(keys, targetTextCandidates);
  const detectedTranslatedInstruction = pickKey(
    keys,
    translatedInstructionCandidates,
    "__none__"
  );
  const detectedTargetLanguage = pickKey(keys, sourceLanguageCandidates);

  if (format) {
    applyFormat(format, keys);
  } else {
    fillSelect(sourceTextKey, keys, detectedSourceText);
    fillSelect(instructionKey, keys, detectedInstruction, [
      { value: "__none__", label: "No instruction path" },
    ]);
    setCustomKey(instructionKeyCustom, detectedInstruction, ["__none__"]);
    fillSelect(sourceLanguageKey, keys, detectedSourceLanguage);
    fillSelect(identifierKey, keys, detectedIdentifier, [
      { value: "__auto__", label: "Generate IDs from row order" },
    ]);
    fillSelect(targetTextKey, keys, detectedTargetText, [
      { value: "", label: "No translation text path" },
    ]);
    setCustomKey(targetTextKeyCustom, detectedTargetText, [""]);
    fillSelect(targetLanguageKey, keys, "", [
      { value: "", label: "No translation language path" },
    ]);
    fillSelect(translatedInstructionKey, keys, detectedTranslatedInstruction, [
      { value: "__none__", label: "No translated instruction path" },
    ]);
    setCustomKey(translatedInstructionKeyCustom, detectedTranslatedInstruction, ["__none__"]);
    sourceTextIsList.checked = Boolean(stats.find((item) => item.key === detectedSourceText)?.isList);
    targetTextIsList.checked = Boolean(stats.find((item) => item.key === detectedTargetText)?.isList);
    if (detectedTargetLanguage && detectedTargetText) {
      targetLanguageKey.value = detectedTargetLanguage;
    }
  }

  schemaStatus.textContent = `${rows.length} row sample detected. Confirm the mapping before import.`;
  sampleRow.textContent = JSON.stringify(rows[0], null, 2);
  renderSummary(stats, rows.length);

  if (!detectedSourceLanguage) {
    sourceLanguageManual.checked = true;
  }
  if (detectedTargetText) {
    hasSeedTranslation.checked = true;
  }
  setManualLanguage(sourceLanguageManual.checked);
  setSeedTranslation(hasSeedTranslation.checked);
}

sourceLanguageManual.addEventListener("change", () => {
  setManualLanguage(sourceLanguageManual.checked);
});

importFormatSelect.addEventListener("change", () => {
  applyFormat(selectedFormat());
  if (jsonlInput.files[0]) {
    detectSchema(jsonlInput.files[0]).catch((error) => {
      schemaStatus.textContent = error.message || "Could not read file.";
    });
  }
});

fileType.addEventListener("change", () => {
  if (jsonlInput.files[0]) {
    detectSchema(jsonlInput.files[0]).catch((error) => {
      schemaStatus.textContent = error.message || "Could not read file.";
    });
  }
});

hasSeedTranslation.addEventListener("change", () => {
  setSeedTranslation(hasSeedTranslation.checked);
});

instructionKey.addEventListener("change", () => {
  setCustomKey(instructionKeyCustom, instructionKey.value, ["__none__"]);
});

targetTextKey.addEventListener("change", () => {
  setCustomKey(targetTextKeyCustom, targetTextKey.value, [""]);
  if (targetTextKey.value) {
    hasSeedTranslation.checked = true;
    setSeedTranslation(true);
  } else {
    hasSeedTranslation.checked = false;
    setSeedTranslation(false);
  }
});

targetTextKeyCustom.addEventListener("input", () => {
  if (targetTextKeyCustom.value.trim()) {
    hasSeedTranslation.checked = true;
    setSeedTranslation(true);
  }
});

targetLanguageName.addEventListener("input", () => {
  if (targetLanguageName.value.trim()) {
    hasSeedTranslation.checked = true;
    setSeedTranslation(true);
  }
});

targetLanguageKey.addEventListener("change", () => {
  if (targetLanguageKey.value) {
    hasSeedTranslation.checked = true;
    setSeedTranslation(true);
  }
});

translatedInstructionKey.addEventListener("change", () => {
  setCustomKey(translatedInstructionKeyCustom, translatedInstructionKey.value, ["__none__"]);
  if (translatedInstructionKey.value && translatedInstructionKey.value !== "__none__") {
    hasSeedTranslation.checked = true;
    setSeedTranslation(true);
  }
});

translatedInstructionKeyCustom.addEventListener("input", () => {
  if (translatedInstructionKeyCustom.value.trim()) {
    hasSeedTranslation.checked = true;
    setSeedTranslation(true);
  }
});

jsonlInput.addEventListener("change", () => {
  detectSchema(jsonlInput.files[0]).catch((error) => {
    schemaStatus.textContent = error.message || "Could not read file.";
  });
});

setManualLanguage(sourceLanguageManual.checked);
setSeedTranslation(hasSeedTranslation.checked);
