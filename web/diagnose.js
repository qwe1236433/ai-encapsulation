/**
 * diagnose.js
 * ===========
 * 笔记诊断 card 的前端逻辑，完全独立于 app.js。
 *
 * 设计原则：
 *   - 不引入任何 CDN 依赖；自带极小 Markdown → HTML 转换（只支持诊断报告
 *     会出现的语法：h1/h2/h3、列表、blockquote、hr、粗体、行内 code）。
 *   - 只调用 POST /api/diagnose/note；绝不自己解析链接。
 *   - 错误全部降级到 #diag-msg，不阻塞页面其余模块。
 */
(function () {
  const $ = (s) => document.querySelector(s);

  const titleEl = $("#diag-title");
  const bodyEl = $("#diag-body");
  const btnRun = $("#btn-diagnose");
  const btnClear = $("#btn-diag-clear");
  const btnCopy = $("#btn-diag-copy");
  const msgEl = $("#diag-msg");
  const summaryEl = $("#diag-summary");
  const renderedEl = $("#diag-rendered");

  if (!titleEl || !bodyEl || !btnRun) return; // 允许诊断 card 不存在时 no-op

  let lastMarkdown = "";

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // 极小 Markdown 渲染（够用即可；避免引入外部依赖）
  function mdToHtml(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let inList = false;
    let listIndentStack = [];
    const closeLists = (toDepth = 0) => {
      while (listIndentStack.length > toDepth) {
        out.push("</ul>");
        listIndentStack.pop();
      }
      if (listIndentStack.length === 0) inList = false;
    };

    function inline(s) {
      // 注意：传进来的 s 在 for 循环里已经被 escapeHtml 过，这里不再二次 escape
      // code span：`xxx` → <code>xxx</code>；xxx 保持已转义状态即可
      s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
      // bold：**xxx** → <strong>xxx</strong>；xxx 也已转义过
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      return s;
    }

    for (let raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) {
        closeLists();
        continue;
      }
      // hr
      if (/^---+\s*$/.test(line)) {
        closeLists();
        out.push("<hr />");
        continue;
      }
      // headings
      let m = line.match(/^(#{1,6})\s+(.*)$/);
      if (m) {
        closeLists();
        const level = m[1].length;
        out.push(`<h${level}>${inline(escapeHtml(m[2]))}</h${level}>`);
        continue;
      }
      // blockquote
      if (line.startsWith(">")) {
        closeLists();
        const content = line.replace(/^>\s?/, "");
        out.push(`<blockquote>${inline(escapeHtml(content))}</blockquote>`);
        continue;
      }
      // list item (支持 2/4 空格缩进)
      const liMatch = line.match(/^(\s*)-\s+(.*)$/);
      if (liMatch) {
        const indent = liMatch[1].length;
        const depth = Math.floor(indent / 2) + 1;
        while (listIndentStack.length < depth) {
          out.push("<ul>");
          listIndentStack.push(depth);
        }
        while (listIndentStack.length > depth) {
          out.push("</ul>");
          listIndentStack.pop();
        }
        inList = true;
        out.push(`<li>${inline(escapeHtml(liMatch[2]))}</li>`);
        continue;
      }
      // plain paragraph
      closeLists();
      out.push(`<p>${inline(escapeHtml(line))}</p>`);
    }
    closeLists();
    return out.join("\n");
  }

  function setMsg(text, isError) {
    msgEl.textContent = text || "";
    msgEl.style.color = isError ? "var(--err)" : "var(--muted)";
  }

  function renderSummary(result) {
    const byLevel = { high: 0, medium: 0, info: 0 };
    (result.suggestions || []).forEach((s) => {
      if (byLevel[s.severity] !== undefined) byLevel[s.severity] += 1;
    });
    const infoCount = (result.info_notes || []).length;
    const pills = [];
    if (byLevel.high) pills.push(`<span class="pill high">🔴 高 ${byLevel.high}</span>`);
    if (byLevel.medium) pills.push(`<span class="pill med">🔵 中 ${byLevel.medium}</span>`);
    if (!byLevel.high && !byLevel.medium) pills.push(`<span class="pill ok">✅ 无强证据扣分项</span>`);
    if (infoCount) pills.push(`<span class="pill info">🟡 参考 ${infoCount}</span>`);
    pills.push(`<span class="pill">模型：${escapeHtml(result.model_ref || "-")}</span>`);
    summaryEl.innerHTML = pills.join("");
    summaryEl.style.display = "flex";
  }

  async function runDiagnose() {
    const title = (titleEl.value || "").trim();
    const body = (bodyEl.value || "").trim();
    if (!title && !body) {
      setMsg("请至少填写标题或正文后再诊断。", true);
      return;
    }
    btnRun.disabled = true;
    setMsg("诊断中…", false);
    renderedEl.style.display = "none";
    summaryEl.style.display = "none";
    try {
      const r = await fetch("/api/diagnose/note", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, body, return_markdown: true }),
      });
      const text = await r.text();
      let data;
      try {
        data = text ? JSON.parse(text) : {};
      } catch {
        data = { raw: text };
      }
      if (!r.ok) {
        const msg = data.detail || data.error || r.statusText || String(r.status);
        throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
      }
      lastMarkdown = data.markdown || "";
      renderSummary(data.result || {});
      renderedEl.innerHTML = mdToHtml(lastMarkdown);
      renderedEl.style.display = "block";
      setMsg(
        `完成：共触发 ${data.trigger_count || 0} 条建议。提示仅供参考，务必阅读报告末尾的使用边界。`,
        false
      );
    } catch (e) {
      setMsg(`诊断失败：${e.message || e}`, true);
    } finally {
      btnRun.disabled = false;
    }
  }

  function clearAll() {
    titleEl.value = "";
    bodyEl.value = "";
    setMsg("", false);
    renderedEl.style.display = "none";
    summaryEl.style.display = "none";
    lastMarkdown = "";
  }

  async function copyMarkdown() {
    if (!lastMarkdown) {
      setMsg("当前没有可复制的诊断报告。先点「开始诊断」。", true);
      return;
    }
    try {
      await navigator.clipboard.writeText(lastMarkdown);
      setMsg("已复制 Markdown 到剪贴板。", false);
    } catch {
      // 降级：选中复制
      const ta = document.createElement("textarea");
      ta.value = lastMarkdown;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        setMsg("已复制 Markdown 到剪贴板。", false);
      } catch {
        setMsg("复制失败，请手动选择渲染区内容复制。", true);
      } finally {
        document.body.removeChild(ta);
      }
    }
  }

  btnRun.addEventListener("click", runDiagnose);
  btnClear.addEventListener("click", clearAll);
  btnCopy.addEventListener("click", copyMarkdown);

  // 键盘快捷：Ctrl+Enter 提交
  [titleEl, bodyEl].forEach((el) => {
    el.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        runDiagnose();
      }
    });
  });
})();
