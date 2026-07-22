function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" rel="noopener noreferrer">$1</a>'
    );
}

function renderMarkdown(value) {
  const lines = String(value || "").split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let list = [];

  function flushParagraph() {
    if (paragraph.length) {
      html.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  }

  function flushList() {
    if (list.length) {
      html.push(`<ul>${list.map((item) => `<li>${item}</li>`).join("")}</ul>`);
      list = [];
    }
  }

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const bullet = line.match(/^[-*]\s+(.+)$/);
    const quote = line.match(/^>\s?(.+)$/);

    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
    } else if (bullet) {
      flushParagraph();
      list.push(renderInlineMarkdown(bullet[1]));
    } else if (quote) {
      flushParagraph();
      flushList();
      html.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`);
    } else {
      flushList();
      paragraph.push(line);
    }
  }

  flushParagraph();
  flushList();
  return html.join("") || "<p></p>";
}

function applyMarkdownAction(textarea, action) {
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const selected = textarea.value.slice(start, end);
  const fallback = selected || "text";
  const replacements = {
    bold: `**${fallback}**`,
    italic: `*${fallback}*`,
    heading: `## ${fallback}`,
    list: selected
      ? selected
          .split(/\r?\n/)
          .map((line) => `- ${line}`)
          .join("\n")
      : "- text",
    quote: selected
      ? selected
          .split(/\r?\n/)
          .map((line) => `> ${line}`)
          .join("\n")
      : "> text",
    code: `\`${fallback}\``,
  };
  const replacement = replacements[action] || fallback;
  textarea.setRangeText(replacement, start, end, "select");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  textarea.focus();
}

function attachMarkdownEditor(textareaId, previewId) {
  const textarea = document.getElementById(textareaId);
  const preview = document.getElementById(previewId);
  if (!textarea || !preview) {
    return;
  }
  const toolbar = document.querySelector(`[data-target="${textareaId}"]`);
  const previewButton = toolbar ? toolbar.querySelector("[data-md-preview]") : null;
  let previewVisible = false;

  function updatePreview() {
    preview.innerHTML = renderMarkdown(textarea.value);
  }

  function setPreviewVisible(visible) {
    previewVisible = visible;
    if (previewVisible) {
      updatePreview();
      textarea.classList.add("hidden");
      preview.classList.remove("hidden");
      if (previewButton) {
        previewButton.textContent = "Edit";
        previewButton.classList.add("active");
      }
    } else {
      preview.classList.add("hidden");
      textarea.classList.remove("hidden");
      if (previewButton) {
        previewButton.textContent = "Preview";
        previewButton.classList.remove("active");
      }
      textarea.focus();
    }
  }

  document.querySelectorAll(`[data-target="${textareaId}"] [data-md]`).forEach((button) => {
    button.addEventListener("click", () => {
      if (previewVisible) {
        setPreviewVisible(false);
      }
      applyMarkdownAction(textarea, button.dataset.md);
    });
  });
  if (previewButton) {
    previewButton.addEventListener("click", () => setPreviewVisible(!previewVisible));
  }
  textarea.addEventListener("input", () => {
    if (previewVisible) {
      updatePreview();
    }
  });
  updatePreview();
}
