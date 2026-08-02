(function () {
  const forms = Array.from(document.querySelectorAll("[data-fast-review-form]"));
  if (!forms.length) {
    return;
  }

  const statusLabel = {
    approved: "reviewed",
    needs_revision: "needs revision",
  };

  function statusText(value) {
    if (!value) {
      return "";
    }
    if (statusLabel[value]) {
      return statusLabel[value];
    }
    return String(value).replaceAll("_", " ");
  }

  function pillClass(value) {
    return value === "approved" ? "reviewed" : value;
  }

  function setFeedback(node, text, mode) {
    if (!node) {
      return;
    }
    node.textContent = text || "";
    node.classList.remove("saved", "error", "busy");
    if (mode) {
      node.classList.add(mode);
    }
  }

  function normalized(value) {
    return String(value || "").trim().toLowerCase();
  }

  function hasTranslationText(form) {
    const lineInputs = Array.from(form.querySelectorAll('textarea[name="target_lines"]'));
    if (lineInputs.length) {
      return lineInputs.some((input) => normalized(input.value) !== "");
    }
    const targetText = form.querySelector('textarea[name="target_text"]');
    return normalized(targetText?.value) !== "";
  }

  function rowMatchesActiveFilters(form, nextStatus, qaWarningCount, qaWarningCodes) {
    const filterLanguage = normalized(form.querySelector('input[name="language"]')?.value);
    const filterStatus = normalized(form.querySelector('input[name="filter_status"]')?.value);
    const filterMissing = normalized(form.querySelector('input[name="missing"]')?.value);
    const filterComments = normalized(form.querySelector('input[name="comments"]')?.value);
    const filterWarnings = normalized(form.querySelector('input[name="warnings"]')?.value);
    const filterWarningCode = normalized(form.querySelector('input[name="warning_code"]')?.value);
    const rowLanguage = normalized(form.querySelector('input[name="target_language"]')?.value);
    const commentValue = normalized(form.querySelector('textarea[name="comment"]')?.value);
    const threadCommentCount = Number(form.dataset.threadCommentCount || "0") || 0;
    const translated = hasTranslationText(form);

    if (filterLanguage && rowLanguage !== filterLanguage) {
      return false;
    }
    if (filterStatus && normalized(nextStatus) !== filterStatus) {
      return false;
    }
    if (filterMissing && translated) {
      return false;
    }
    if (filterWarnings && (Number(qaWarningCount) || 0) < 1) {
      return false;
    }
    if (filterWarningCode) {
      const rowCodes = Array.isArray(qaWarningCodes)
        ? qaWarningCodes.map((code) => normalized(code)).filter(Boolean)
        : [];
      if (!rowCodes.includes(filterWarningCode)) {
        return false;
      }
    }
    if (filterComments && threadCommentCount < 1 && !commentValue) {
      return false;
    }
    return true;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitter = event.submitter;
    const feedback = form.querySelector("[data-fast-review-feedback]");
    const statusPill = form.querySelector("[data-review-status-pill]");
    const statusSelect = form.querySelector('select[name="translation_status"]');
    const versionInput = form.querySelector('input[name="version"]');
    const metaLine = form.querySelector("[data-fast-review-meta]");
    const qaTag = form.querySelector("[data-fast-review-qa-tag]");
    const actionButtons = form.querySelectorAll('button[type="submit"]');

    const formData = new FormData(form);
    if (submitter && submitter.name) {
      formData.set(submitter.name, submitter.value);
    }

    actionButtons.forEach((button) => {
      button.disabled = true;
    });
    setFeedback(feedback, "Saving…", "busy");

    try {
      const response = await fetch(window.location.href, {
        method: "POST",
        body: formData,
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
      });

      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.status === "error" || payload.status === "conflict") {
        if (payload.version !== undefined && versionInput) {
          versionInput.value = String(payload.version);
        }
        const message = payload.message || "Save failed. Please try again.";
        setFeedback(feedback, message, "error");
        return;
      }

      const nextStatus = payload.translation_status || statusSelect?.value || "";
      if (statusSelect) {
        statusSelect.value = nextStatus;
      }
      if (statusPill) {
        statusPill.textContent = statusText(nextStatus);
        statusPill.className = `review-status ${pillClass(nextStatus)}`;
      }
      if (versionInput && payload.version !== undefined) {
        versionInput.value = String(payload.version);
      }
      if (metaLine) {
        const updater = payload.updated_by ? `Updated by ${payload.updated_by}` : "Translation saved";
        const timestamp = payload.updated_at ? ` · ${payload.updated_at}` : "";
        metaLine.textContent = `${updater}${timestamp}`;
      }
      if (qaTag && payload.qa_warning_count !== undefined) {
        const count = Number(payload.qa_warning_count) || 0;
        qaTag.textContent = `${count} QA`;
        qaTag.classList.toggle("hidden", count < 1);
      }

      const qaCount = Number(payload.qa_warning_count) || 0;
      const qaCodes = Array.isArray(payload.qa_warning_codes)
        ? payload.qa_warning_codes
        : [];
      if (rowMatchesActiveFilters(form, nextStatus, qaCount, qaCodes)) {
        form.classList.remove("hidden");
        setFeedback(feedback, payload.message || "Saved.", "saved");
      } else {
        form.classList.add("hidden");
        setFeedback(feedback, "Saved. Row no longer matches active filters.", "saved");
      }
    } catch (error) {
      setFeedback(feedback, "Network error. Please retry.", "error");
    } finally {
      actionButtons.forEach((button) => {
        button.disabled = false;
      });
    }
  }

  forms.forEach((form) => {
    form.addEventListener("submit", handleSubmit);
  });
})();
