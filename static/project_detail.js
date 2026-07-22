const translationJsonlInput = document.querySelector("#translationJsonlInput");
const translationSchemaStatus = document.querySelector("#translationSchemaStatus");
const translationSchemaSummary = document.querySelector("#translationSchemaSummary");
const translationSampleRow = document.querySelector("#translationSampleRow");
const uploadTargetLanguage = document.querySelector("#uploadTargetLanguage");
const translationMessageIdKey = document.querySelector("#translationMessageIdKey");
const translationTextKey = document.querySelector("#translationTextKey");
const translationLanguageKey = document.querySelector("#translationLanguageKey");
const translationCommentKey = document.querySelector("#translationCommentKey");
const uploadTranslatedInstructionKey = document.querySelector("#uploadTranslatedInstructionKey");
const sourceVariantFile = document.querySelector("#sourceVariantFile");
const sourceVariantFileType = document.querySelector("#sourceVariantFileType");
const sourceVariantRowsPath = document.querySelector("#sourceVariantRowsPath");
const sourceVariantLanguage = document.querySelector("#sourceVariantLanguage");
const sourceVariantIdKey = document.querySelector("#sourceVariantIdKey");
const sourceVariantTextKey = document.querySelector("#sourceVariantTextKey");
const sourceVariantLanguageKey = document.querySelector("#sourceVariantLanguageKey");
const sourceVariantInstructionKey = document.querySelector("#sourceVariantInstructionKey");
const sourceVariantTextIsList = document.querySelector("#sourceVariantTextIsList");
const sourceVariantSchemaStatus = document.querySelector("#sourceVariantSchemaStatus");
const sourceVariantSchemaSummary = document.querySelector("#sourceVariantSchemaSummary");
const sourceVariantSampleRow = document.querySelector("#sourceVariantSampleRow");

const uploadIdCandidates = ["message_id", "identifier", "id", "text_id", "segment_id", "uniq_id", "pid", "uid", "key"];
const uploadTranslationCandidates = [
  "translation",
  "target_text",
  "tgt_text",
  "translated_text",
  "text",
];
const uploadLanguageCandidates = ["target_language", "tgt_lang", "language", "lang", "locale"];
const uploadSourceLanguageCandidates = ["source_language", "source_lang", "src_lang", ...uploadLanguageCandidates];
const uploadSourceTextCandidates = [
  "source_text",
  "src_text",
  "text",
  "translation",
  "target_text",
  "tgt_text",
  "translated_text",
  "sentences",
  "content",
  "body",
];
const uploadCommentCandidates = ["comment", "tgt_comment", "note", "review_comment"];
const uploadInstructionCandidates = ["instructions", "instruction", "response", "source_instruction", "source_instructions"];
const uploadTranslatedInstructionCandidates = [
  "translated_instruction",
  "translated_instructions",
  "target_instruction",
  "target_instructions",
  "tgt_instruction",
  "tgt_instructions",
];

function escapeUploadHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function uploadValuePreview(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value).slice(0, 80);
  }
  return String(value).slice(0, 80);
}

function pickUploadKey(keys, preferred, fallback = "") {
  const lowerLookup = new Map(keys.map((key) => [key.toLowerCase(), key]));
  for (const candidate of preferred) {
    if (lowerLookup.has(candidate)) {
      return lowerLookup.get(candidate);
    }
  }
  return fallback;
}

function addUploadOption(select, value, label, selectedValue) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label || value;
  option.selected = value === selectedValue;
  select.append(option);
}

function fillUploadSelect(select, keys, selectedValue, extraOptions = []) {
  select.innerHTML = "";
  extraOptions.forEach((item) => addUploadOption(select, item.value, item.label, selectedValue));
  keys.forEach((key) => addUploadOption(select, key, key, selectedValue));
}

function parseUploadSample(text) {
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
  return { rows, errors };
}

function getUploadPath(item, path) {
  return String(path || "")
    .split(".")
    .filter(Boolean)
    .reduce((value, part) => {
      if (value && typeof value === "object" && !Array.isArray(value) && part in value) {
        return value[part];
      }
      return undefined;
    }, item);
}

function parseJsonUploadSample(text, rowsPath = "") {
  const payload = JSON.parse(text);
  let rows = rowsPath ? getUploadPath(payload, rowsPath) : payload;
  if (!rowsPath && payload && typeof payload === "object" && Array.isArray(payload.translations)) {
    rows = payload.translations;
  }
  if (rows && typeof rows === "object" && !Array.isArray(rows)) {
    rows = [rows];
  }
  if (!Array.isArray(rows)) {
    throw new Error("Rows path must point to a JSON object or array.");
  }
  return rows.filter((row) => row && typeof row === "object" && !Array.isArray(row)).slice(0, 25);
}

function parseSourceVariantSample(text, file, rowsPath = "") {
  const fileName = (file?.name || "").toLowerCase();
  if (fileName.endsWith(".json")) {
    return { rows: parseJsonUploadSample(text, rowsPath), errors: [] };
  }
  const parsed = parseUploadSample(text);
  if (parsed.rows.length) {
    return parsed;
  }
  try {
    return { rows: parseJsonUploadSample(text, rowsPath), errors: [] };
  } catch (error) {
    return parsed.errors.length ? parsed : { rows: [], errors: [error.message] };
  }
}

function uploadKeyStats(rows) {
  const stats = new Map();
  rows.forEach((row) => {
    Object.entries(row).forEach(([key, value]) => {
      if (!stats.has(key)) {
        stats.set(key, { key, count: 0, example: "" });
      }
      const item = stats.get(key);
      item.count += 1;
      if (!item.example) {
        item.example = uploadValuePreview(value);
      }
    });
  });
  return [...stats.values()].sort((a, b) => a.key.localeCompare(b.key));
}

function renderUploadSummary(stats, rowCount, target = translationSchemaSummary) {
  if (!stats.length) {
    target.innerHTML = "<span>No object keys found.</span>";
    return;
  }
  target.innerHTML = stats
    .map(
      (item) => `
        <div>
          <strong>${escapeUploadHtml(item.key)}</strong>
          <span>${item.count} / ${rowCount} sampled rows</span>
          <small>${escapeUploadHtml(item.example)}</small>
        </div>
      `
    )
    .join("");
}

async function detectTranslationUploadSchema(file) {
  if (!file) {
    return;
  }
  translationSchemaStatus.textContent = "Reading translation file sample...";
  const text = await file.slice(0, 256 * 1024).text();
  const { rows, errors } = parseUploadSample(text);
  if (!rows.length) {
    translationSchemaStatus.textContent = errors[0] || "No valid JSONL rows found.";
    renderUploadSummary([], 0);
    return;
  }

  const stats = uploadKeyStats(rows);
  const keys = stats.map((item) => item.key);
  const detectedId = pickUploadKey(keys, uploadIdCandidates, keys[0] || "message_id");
  const detectedTranslation = pickUploadKey(keys, uploadTranslationCandidates);
  const detectedLanguage = pickUploadKey(keys, uploadLanguageCandidates);
  const detectedComment = pickUploadKey(keys, uploadCommentCandidates);
  const detectedInstruction = pickUploadKey(keys, uploadTranslatedInstructionCandidates);

  fillUploadSelect(translationMessageIdKey, keys, detectedId);
  fillUploadSelect(translationTextKey, keys, detectedTranslation, [
    { value: "", label: "Choose translation key" },
  ]);
  fillUploadSelect(translationLanguageKey, keys, detectedLanguage, [
    { value: "", label: "Use language name above" },
  ]);
  fillUploadSelect(translationCommentKey, keys, detectedComment, [
    { value: "", label: "No comment key" },
  ]);
  fillUploadSelect(uploadTranslatedInstructionKey, keys, detectedInstruction, [
    { value: "", label: "No translated instruction key" },
  ]);

  if (detectedLanguage && !uploadTargetLanguage.value.trim()) {
    const detectedLanguageValue = rows.find((row) => row[detectedLanguage]);
    if (detectedLanguageValue) {
      uploadTargetLanguage.value = String(detectedLanguageValue[detectedLanguage]).trim();
    }
  }

  translationSampleRow.textContent = JSON.stringify(rows[0], null, 2);
  translationSchemaStatus.textContent = `${rows.length} row sample detected. Confirm the keys before upload.`;
  renderUploadSummary(stats, rows.length);
}

async function detectSourceVariantSchema(file) {
  if (!file) {
    return;
  }
  sourceVariantSchemaStatus.textContent = "Reading source file sample...";
  const isJsonFile = (file.name || "").toLowerCase().endsWith(".json");
  const text = isJsonFile ? await file.text() : await file.slice(0, 512 * 1024).text();
  const { rows, errors } = parseSourceVariantSample(text, file, sourceVariantRowsPath.value.trim());
  if (!rows.length) {
    sourceVariantSchemaStatus.textContent = errors[0] || "No valid rows found.";
    renderUploadSummary([], 0, sourceVariantSchemaSummary);
    return;
  }

  if (isJsonFile) {
    sourceVariantFileType.value = "json";
    if (!sourceVariantRowsPath.value.trim()) {
      try {
        const payload = JSON.parse(text);
        if (payload && typeof payload === "object" && !Array.isArray(payload) && Array.isArray(payload.translations)) {
          sourceVariantRowsPath.value = "translations";
        }
      } catch (error) {
        // The visible status already reports parse failures below.
      }
    }
  }

  const stats = uploadKeyStats(rows);
  const keys = stats.map((item) => item.key);
  const detectedId = pickUploadKey(keys, uploadIdCandidates, keys[0] || "identifier");
  const detectedText = pickUploadKey(keys, uploadSourceTextCandidates);
  const detectedLanguage = pickUploadKey(keys, uploadSourceLanguageCandidates);
  const detectedInstruction = pickUploadKey(keys, uploadInstructionCandidates);

  fillUploadSelect(sourceVariantIdKey, keys, detectedId);
  fillUploadSelect(sourceVariantTextKey, keys, detectedText, [
    { value: "", label: "Choose source text key" },
  ]);
  fillUploadSelect(sourceVariantLanguageKey, keys, detectedLanguage, [
    { value: "", label: "Use source language name above" },
  ]);
  fillUploadSelect(sourceVariantInstructionKey, keys, detectedInstruction, [
    { value: "", label: "No instruction key" },
  ]);

  if (detectedText) {
    sourceVariantTextIsList.checked = rows.some((row) => Array.isArray(row[detectedText]));
  }
  if (detectedLanguage && !sourceVariantLanguage.value.trim()) {
    const rowWithLanguage = rows.find((row) => row[detectedLanguage]);
    if (rowWithLanguage) {
      sourceVariantLanguage.value = String(rowWithLanguage[detectedLanguage]).trim();
    }
  }

  sourceVariantSampleRow.textContent = JSON.stringify(rows[0], null, 2);
  sourceVariantSchemaStatus.textContent = `${rows.length} row sample detected. Confirm the keys before upload.`;
  renderUploadSummary(stats, rows.length, sourceVariantSchemaSummary);
}

if (translationJsonlInput) {
  translationJsonlInput.addEventListener("change", () => {
    detectTranslationUploadSchema(translationJsonlInput.files[0]).catch((error) => {
      translationSchemaStatus.textContent = error.message || "Could not read file.";
    });
  });
}

if (sourceVariantFile) {
  sourceVariantFile.addEventListener("change", () => {
    detectSourceVariantSchema(sourceVariantFile.files[0]).catch((error) => {
      sourceVariantSchemaStatus.textContent = error.message || "Could not read file.";
    });
  });
}

if (sourceVariantRowsPath) {
  sourceVariantRowsPath.addEventListener("change", () => {
    if (sourceVariantFile?.files?.[0]) {
      detectSourceVariantSchema(sourceVariantFile.files[0]).catch((error) => {
        sourceVariantSchemaStatus.textContent = error.message || "Could not read file.";
      });
    }
  });
}
