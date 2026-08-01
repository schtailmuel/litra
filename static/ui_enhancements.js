(function () {
  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function isBackAction(node) {
    const text = normalizeText(node && node.textContent).toLowerCase();
    return text.startsWith("back");
  }

  function findActionIcon(text) {
    const value = String(text || "").toLowerCase();
    if (!value || value.length <= 2) {
      return "";
    }
    if (value.startsWith("back")) return "←";
    if (value.includes("download") || value.includes("export")) return "⤓";
    if (value.includes("create") || value.includes("new") || value.includes("add") || value.includes("import") || value.includes("upload")) return "+";
    if (value.includes("save") || value.includes("update")) return "";
    if (value.includes("open")) return "↗";
    if (value.includes("delete") || value.includes("remove") || value.includes("revoke")) return "🗑";
    if (value.includes("login")) return "⇤";
    if (value.includes("logout")) return "⇥";
    if (value.includes("copy")) return "⎘";
    return "";
  }

  function applyIcons() {
    const candidates = document.querySelectorAll("a.button, button, .topbar nav a");
    for (const node of candidates) {
      if (node.hasAttribute("data-icon")) {
        continue;
      }
      const text = normalizeText(node.textContent);
      if (!text) {
        continue;
      }
      const icon = findActionIcon(text);
      if (!icon) {
        continue;
      }
      node.setAttribute("data-icon", icon);
    }
  }

  function relocateBackButtons() {
    const hasBreadcrumbs = !!document.querySelector(".breadcrumbs li");
    const pageHeads = document.querySelectorAll(".page-head");
    for (const head of pageHeads) {
      const actions = head.querySelector(":scope > .page-actions");
      const directButtons = Array.from(head.querySelectorAll(":scope > a.button, :scope > button"));
      const actionButtons = actions
        ? Array.from(actions.querySelectorAll("a.button, button"))
        : [];
      const backButtons = [...directButtons, ...actionButtons].filter(isBackAction);
      if (!backButtons.length) {
        continue;
      }

      if (hasBreadcrumbs) {
        for (const backButton of backButtons) {
          backButton.remove();
        }
        if (actions && !actions.querySelector("a.button, button")) {
          actions.remove();
        }
        continue;
      }

      const back = backButtons[0];

      back.classList.add("page-back-link", "secondary");
      if (!back.hasAttribute("data-icon")) {
        back.setAttribute("data-icon", "←");
      }

      const firstChild = head.firstElementChild;
      if (firstChild && firstChild !== back) {
        head.insertBefore(back, firstChild);
      }
      head.classList.add("page-head--with-back");

      if (actions && !actions.querySelector("a.button, button")) {
        actions.remove();
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      relocateBackButtons();
      applyIcons();
    });
  } else {
    relocateBackButtons();
    applyIcons();
  }
})();
