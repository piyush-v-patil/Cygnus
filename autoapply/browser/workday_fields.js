// Workday application pages: one entry per visible question block.
// Workday wraps every question in [data-automation-id^="formField-"] and builds its own
// widgets: listbox buttons ("Select One"), search-and-pick multiselects, split date
// boxes. Keys start with "wd:"; data-aa-key is stamped on the block, so fills find
// the control inside it. Element ids from live tenants and Simplify 3.1.10's Workday
// selectors, 2026-10-23.
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => !!el && el.offsetParent !== null && getComputedStyle(el).visibility !== "hidden";
  const root = document.querySelector('[data-automation-id="applyFlowPage"]') || document.querySelector("#mainContent") || document.body;
  const blocks = [...root.querySelectorAll('[data-automation-id^="formField-"]')]
    .filter((b) => !b.closest('[data-automation-id="signInContent"]'))
    .filter((b) => !b.parentElement.closest('[data-automation-id^="formField-"]'));   // outermost only
  const out = [];
  const used = new Set();

  const labelOf = (b) => {
    const lab = b.querySelector("legend, label");
    let t = clean(lab ? lab.innerText : "");
    if (!t) {
      const q = b.querySelector('[data-automation-id="richText"]');
      t = clean(q ? q.innerText : "");
    }
    if (!t) {
      const c = b.querySelector("[aria-label]");
      t = clean(c ? c.getAttribute("aria-label") : "");
    }
    return t;
  };
  // repeatable entries (Simplify 3.1.10's Workday ids: workExperience-N, education-N,
  // websitePanelSet-N, language-N): "Work Experience 2 › From", so one entry's answer is
  // never taken for another entry, or for another company's form
  const SECTION_NAMES = { workExperience: "Work Experience", education: "Education", websitePanelSet: "Website",
                          language: "Language", Languages: "Language" };
  const sectionOf = (b) => {
    for (let el = b.parentElement; el && el !== root; el = el.parentElement) {
      const m = /^(workExperience|education|websitePanelSet|language|Languages)-(\d+)/.exec(el.getAttribute("data-automation-id") || "");
      if (!m) continue;
      const h = el.querySelector("h3, h4, h5, [role=heading]");
      const t = clean(h ? h.innerText : "");
      return t && /\d/.test(t) ? t : `${SECTION_NAMES[m[1]]} ${m[2]}`;
    }
    return "";
  };
  const required = (b, label) => /\*\s*$/.test(label) || !!b.querySelector('[aria-required="true"], [required]')
    || !!b.querySelector('abbr[title="required"]');
  const keyFor = (b, ctrl) => {
    let k = "wd:" + ((ctrl && ctrl.id) || b.getAttribute("data-automation-id"));
    let n = k, i = 2;
    while (used.has(n)) n = k + "#" + i++;
    used.add(n);
    return n;
  };

  for (const b of blocks) {
    if (!visible(b)) continue;
    const own = labelOf(b);
    const req = required(b, own);
    const section = sectionOf(b);
    const label = section ? `${section} › ${own}` : own;
    const push = (kind, ctrl, value, options, extra) => {
      const key = keyFor(b, ctrl);
      b.setAttribute("data-aa-key", key);
      out.push(Object.assign({ key, label, kind, required: req, value: value || "", options: options || [],
                               visible: true, section }, extra || {}));
    };

    const file = b.querySelector('input[type="file"]');
    if (file) {
      const items = [...b.querySelectorAll('[data-automation-id="file-upload-item"]')].map((i) => clean(i.innerText));
      push("file", file, items.join("; "), [], { accept: file.getAttribute("accept") || "" });
      continue;
    }
    const multi = b.querySelector('[data-automation-id="multiselectInputContainer"], input[placeholder="Search"]');
    if (multi) {
      const chosen = [...b.querySelectorAll('[data-automation-id="selectedItem"]')]
        .map((i) => clean(i.getAttribute("title") || i.innerText))
        .filter((t) => t && !/^no (items|matches|results)/i.test(t));
      push("wd_multi", b.querySelector("input") || multi, chosen.join("; "));
      continue;
    }
    const drop = b.querySelector('button[aria-haspopup="listbox"]');
    if (drop) {
      const v = clean(drop.innerText);
      push("wd_select", drop, /^select one$/i.test(v) ? "" : v);
      continue;
    }
    const dates = [...b.querySelectorAll('input[data-automation-id^="dateSection"]')];
    if (dates.length) {
      const part = (n) => { const e = b.querySelector(`input[data-automation-id="dateSection${n}-input"]`);
                            return e ? clean(e.value) : null; };
      const m = part("Month"), d = part("Day"), y = part("Year");
      const parts = [m, d, y].filter((x) => x !== null);
      const value = parts.every((x) => x) ? parts.join("/") : "";
      push("wd_date", dates[0], value, [], { input_type: [m !== null && "MM", d !== null && "DD", y !== null && "YYYY"]
        .filter(Boolean).join("/") });
      continue;
    }
    const radios = [...b.querySelectorAll('input[type="radio"]')];
    if (radios.length) {
      const optText = (r) => clean((r.labels && r.labels[0] && r.labels[0].innerText) || r.getAttribute("aria-label") || r.value);
      const on = radios.find((r) => r.checked);
      push("radio", radios[0], on ? optText(on) : "", radios.map(optText));
      continue;
    }
    const boxes = [...b.querySelectorAll('input[type="checkbox"]')];
    if (boxes.length > 1) {
      const optText = (r) => clean((r.labels && r.labels[0] && r.labels[0].innerText) || r.getAttribute("aria-label") || r.value);
      push("checkbox_group", boxes[0], boxes.filter((x) => x.checked).map(optText).join("; "), boxes.map(optText));
      continue;
    }
    if (boxes.length === 1) {
      push("checkbox", boxes[0], boxes[0].checked ? "true" : "");
      continue;
    }
    const area = b.querySelector("textarea");
    if (area) {
      push("textarea", area, area.value, [], { max_length: area.maxLength > 0 ? area.maxLength : null });
      continue;
    }
    const input = [...b.querySelectorAll("input")].find((i) =>
      !["hidden", "file", "radio", "checkbox"].includes(i.type) && i.getAttribute("data-automation-id") !== "beecatcher");
    if (input) {
      push("text", input, input.value, [], { input_type: input.type || "text",
                                              max_length: input.maxLength > 0 ? input.maxLength : null });
    }
  }
  // the resume upload area is not always inside a formField block (Simplify's own Workday
  // selector looks for resumeUpload / quickApplyUpload with a file-upload-input-ref)
  for (const up of root.querySelectorAll('[data-automation-id="resumeUpload"], [data-automation-id="quickApplyUpload"]')) {
    if (up.closest('[data-automation-id^="formField-"]') || !visible(up)) continue;
    const file = up.querySelector('input[type="file"]');
    if (!file) continue;
    const key = keyFor(up, file);
    up.setAttribute("data-aa-key", key);
    const items = [...up.querySelectorAll('[data-automation-id="file-upload-item"]')].map((i) => clean(i.innerText));
    out.push({ key, label: "Resume/CV", kind: "file", required: true, value: items.join("; "), options: [],
               visible: true, section: "", accept: file.getAttribute("accept") || "" });
  }
  return out;
}
