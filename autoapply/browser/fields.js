// Enumerate every answerable field on an application form.
//
// Each control gets a deterministic key (from its id, name or label) stamped
// as data-aa-key, so Python can address it and a replay on a fresh page finds
// the same field again. Runs in the page; `hints` comes from autoapply/ats/*.
(hints) => {
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "search"]);
  const norm = (t) => (t || "").replace(/\s+/g, " ").trim();
  const baseContainers = [hints.container || "", ".field-wrapper", ".application-question",
    ".ashby-application-form-field-entry", "fieldset", ".form-group", ".field"].filter(Boolean);
  // `li` only for single controls: in option lists every choice sits in its own li
  const containerSel = [...baseContainers, "li"].join(", ");
  const groupSel = baseContainers.join(", ");

  const visible = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const textOf = (el) => {
    if (!el) return "";
    const c = el.cloneNode(true);
    c.querySelectorAll("input,select,textarea,button,svg,style,script,ul[role=listbox]").forEach((x) => x.remove());
    return norm(c.textContent);
  };
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      // a file input's label is often just its button ("Attach") - the question is elsewhere
      if (l && textOf(l) && !/^(attach|upload|browse|choose( a)? file|select file)\b/i.test(textOf(l))) return textOf(l);
    }
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((i) => textOf(document.getElementById(i))).join(" ");
      if (norm(t)) return norm(t);
    }
    const wrapping = el.closest("label");
    const c = el.closest(containerSel);
    // Lever: <li class=application-question><label><div class=application-label>
    // most specific first: a Lever row's wrapping <label> also holds helper text
    const inner = c && [".application-label", "legend", "label", "[class*=label]"]
      .map((q) => c.querySelector(q)).find((x) => x && textOf(x));
    if (inner && !inner.contains(el) && textOf(inner)) return textOf(inner);
    if (wrapping && textOf(wrapping)) return textOf(wrapping);
    if (el.getAttribute("aria-label")) return norm(el.getAttribute("aria-label"));
    if (inner && textOf(inner)) return textOf(inner);
    return norm(el.getAttribute("placeholder") || el.name || "");
  };
  // label + the container it was found in, walking outwards past option wrappers
  const groupInfo = (els) => {
    const fs = els[0].closest("fieldset");
    if (fs) {
      const lg = fs.querySelector(":scope > legend");
      if (lg && textOf(lg)) return { label: textOf(lg), box: fs };
    }
    let c = els[0].closest(groupSel);
    while (c) {
      const l = c.querySelector(".application-label, :scope > label:not(:has(input)), legend, " +
        "label[for]:not(:has(input)):not([for*=labeled]), [class*=label]:not(:has(input)):not(label[for*=labeled])");
      if (l && !els.some((e) => l.contains(e)) && textOf(l) && !els.map(optionLabel).includes(textOf(l))) {
        return { label: textOf(l), box: c };
      }
      c = c.parentElement && c.parentElement.closest(groupSel);
    }
    return { label: labelFor(els[0]), box: els[0].closest(containerSel) };
  };
  const groupLabel = (els) => groupInfo(els).label;
  const optionLabel = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const w = el.closest("label");
    if (w && textOf(w)) return textOf(w);
    return norm(el.value || el.getAttribute("aria-label") || "");
  };
  const isRequired = (el, label) => {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    if (/[*✱]\s*$/.test(label || "") || /\(required\)\s*$/i.test(label || "")) return true;
    const c = el.closest(containerSel);
    if (c && c.querySelector(".required, [class*=_required], .asterisk")) return true;
    return false;
  };
  const comboValue = (el) => {
    const c = el.closest(".select__container, .select-shell, [class*=select], [class*=autocomplete]") || el.parentElement;
    const sv = c && c.querySelector("[class*=single-value], [class*=singleValue], [class*=multi-value__label]");
    if (sv && norm(sv.textContent)) {
      return norm([...c.querySelectorAll("[class*=single-value], [class*=singleValue], [class*=multi-value__label]")]
        .map((x) => x.textContent).join("; "));
    }
    return norm(el.value);
  };

  const used = new Map();
  const mkKey = (base) => {
    const n = (used.get(base) || 0) + 1;
    used.set(base, n);
    return n === 1 ? base : `${base}#${n}`;
  };
  const keyFor = (el, label, prefix) => {
    if (el.id && !/^react-select|^:r|^radix-|^headlessui/.test(el.id)) return mkKey(`${prefix}id:${el.id}`);
    if (el.name) return mkKey(`${prefix}name:${el.name}`);
    return mkKey(`${prefix}label:${label.toLowerCase().slice(0, 80)}`);
  };

  const fields = [];
  const seenGroups = new Set();
  const controls = [...document.querySelectorAll("input, textarea, select")];
  for (const el of controls) {
    const type = (el.type || el.tagName).toLowerCase();
    if (SKIP_TYPES.has(type)) continue;
    if (el.closest("[class*=recaptcha], [class*=captcha], .simplify-jobs-shadow-root")) continue;
    if (/captcha-response|^g-recaptcha/.test(el.name || el.id || "")) continue;
    if (el.closest("[data-aa-ignore]")) continue;
    const role = el.getAttribute("role");
    // mirrors / helper inputs with nothing to address them by (Greenhouse requiredInput)
    if (!el.id && !el.name && role !== "combobox" && type !== "file" && type !== "checkbox" && type !== "radio") continue;
    if (el.tagName === "INPUT" && el.closest("[class*=yesno], [data-aa-buttons]") && type !== "file") continue;

    if (type === "radio" || type === "checkbox") {
      const box = el.closest(groupSel);
      const inBox = box ? [...box.querySelectorAll(`input[type=${type}]`)] : [];
      let group = null, gid = null, keySrc = null;
      if (type === "radio" && el.name) {
        group = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)];
        gid = `n:${el.name}`; keySrc = { name: el.name };
      } else if (inBox.length > 1) {
        group = inBox;
        gid = `c:${[...document.querySelectorAll(groupSel)].indexOf(box)}`;
        const idBase = (group[0].id || "").replace(/-?\d+$/, "");
        keySrc = idBase ? { id: idBase } : (group[0].name ? { name: group[0].name } : {});
      }
      if (group) {
        if (seenGroups.has(gid)) continue;
        seenGroups.add(gid);
        const info = groupInfo(group);
        const label = info.label;
        const key = keyFor(keySrc, label, type === "radio" ? "radio:" : "checks:");
        const options = group.map(optionLabel);
        group.forEach((g, i) => { g.setAttribute("data-aa-key", key); g.setAttribute("data-aa-opt", String(i)); });
        const checked = group.filter((g) => g.checked).map(optionLabel);
        fields.push({
          key, label, kind: type === "radio" ? "radio" : "checkbox_group", options,
          required: group.some((g) => g.required) || isRequired(group[0], label) ||
            !!(info.box && info.box.querySelector(".required, [class*=_required]")),
          value: type === "radio" ? (checked[0] || "") : checked.join("; "),
          visible: group.some((g) => visible(g) || visible(g.closest("label"))),
        });
        continue;
      }
      // a single checkbox (consent, "I agree")
      const label = labelFor(el);
      const key = keyFor(el, label, "check:");
      el.setAttribute("data-aa-key", key);
      fields.push({ key, label, kind: "checkbox", options: [], required: isRequired(el, label),
        value: el.checked ? "true" : "", visible: visible(el) || visible(el.closest("label")) });
      continue;
    }

    const label = labelFor(el);
    let kind = el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : "text";
    if (type === "file") kind = "file";
    if (role === "combobox") kind = "combobox";
    const key = keyFor(el, label, "");
    el.setAttribute("data-aa-key", key);
    let value = el.value || "";
    let options = [];
    if (kind === "select") {
      options = [...el.options].filter((o) => o.value !== "" && !/^select\b|^choose|^--/i.test(o.text.trim()))
        .map((o) => norm(o.text));
      const o = el.selectedOptions[0];
      value = o && o.value !== "" ? norm(o.text) : "";
    } else if (kind === "combobox") {
      value = comboValue(el);
    } else if (kind === "file") {
      const c = el.closest(containerSel);
      const shown = c ? norm((c.querySelector("[class*=filename], [class*=file-name], [class*=fileName], .filename") || {}).textContent) : "";
      value = el.files && el.files.length ? [...el.files].map((f) => f.name).join("; ") : shown;
    }
    fields.push({
      key, label, kind, input_type: type, options, required: isRequired(el, label), value: norm(value),
      max_length: el.maxLength > 0 ? el.maxLength : null,
      visible: kind === "file" ? true : visible(el),
      accept: el.getAttribute("accept") || "",
    });
  }

  // Ashby-style yes/no: a labelled container holding pressed/unpressed buttons
  const btnGroups = new Set();
  document.querySelectorAll("button[aria-pressed]").forEach((b) => {
    const g = b.parentElement;
    if (!g || btnGroups.has(g) || g.closest(".simplify-jobs-shadow-root")) return;
    const btns = [...g.querySelectorAll(":scope > button[aria-pressed]")];
    if (btns.length < 2) return;
    btnGroups.add(g);
    const c = g.closest(containerSel);
    const l = c && c.querySelector("label, legend, [class*=label]");
    const label = l ? textOf(l) : "";
    const forId = l && l.getAttribute("for");
    const key = mkKey(forId ? `buttons:id:${forId}` : `buttons:label:${label.toLowerCase().slice(0, 80)}`);
    g.setAttribute("data-aa-key", key);
    g.setAttribute("data-aa-buttons", "1");
    const pressed = btns.find((x) => x.getAttribute("aria-pressed") === "true");
    fields.push({ key, label, kind: "buttons", options: btns.map((x) => norm(x.textContent)),
      required: !!(l && /required/i.test(l.className || "")) || /[*✱]\s*$/.test(label),
      value: pressed ? norm(pressed.textContent) : "", visible: visible(g) });
  });
  return fields;
}
