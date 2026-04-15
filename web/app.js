(function () {
  const $ = (s) => document.querySelector(s);

  async function fetchJSON(url, opts) {
    const r = await fetch(url, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
    });
    const text = await r.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }
    if (!r.ok) {
      let msg = data.detail || data.error || r.statusText;
      if (Array.isArray(msg)) msg = msg.map((x) => x.msg || JSON.stringify(x)).join("; ");
      throw new Error(msg || String(r.status));
    }
    return data;
  }

  function setHealth(h) {
    const el = $("#health-status");
    if (!h) {
      el.textContent = "无法获取";
      return;
    }
    const ha = h.hermes?.ok ? "OK" : "离线";
    const oa = h.openclaw?.ok ? "OK" : "离线";
    el.innerHTML = `Hermes <strong>${ha}</strong> · OpenClaw <strong>${oa}</strong> · ${h.time || ""}`;
    el.className = "status-line " + (h.hermes?.ok && h.openclaw?.ok ? "all-ok" : "partial");
  }

  function setConfig(c) {
    const el = $("#config-paths");
    if (!c || !el) return;
    el.innerHTML = [
      `<div><span class="k">爬虫 JSONL</span> <span class="mono">${escapeHtml(c.mediacrawler_jsonl)}</span></div>`,
      `<div><span class="k">Feed 输出</span> <span class="mono">${escapeHtml(c.feed_out)}</span></div>`,
      `<div><span class="k">默认 goal</span> <span class="mono">${escapeHtml(c.bench_goal_default)}</span></div>`,
    ].join("");
    const g = $("#input-goal");
    if (g && c.bench_goal_default && !String(g.value).trim()) g.value = c.bench_goal_default;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** 优先 textarea 主题；否则才传 goal_path（未传则走后端默认文件）。 */
  function addGoalFieldsToBody(body) {
    const ta = $("#input-goal-text");
    const topic = ta ? String(ta.value).trim() : "";
    const pathInput = $("#input-goal");
    const goalPath = pathInput ? String(pathInput.value).trim() : "";
    if (topic) body.goal_text = topic;
    else if (goalPath) body.goal_path = goalPath;
    return body;
  }

  function stageLabel(stage) {
    const m = {
      pending: "准备中…",
      merge: "第 1/3 步：合并爬取 → samples.json",
      docker: "第 2/3 步：启动 Docker 容器",
      bench: "第 3/3 步：生成正文（约 1～3 分钟）",
      done: "全部完成",
    };
    return m[stage] || stage || "—";
  }

  function formatJobForLog(j, mode) {
    if (mode === "full" || j.kind === "full-pipeline") {
      const lines = (j.pipeline_logs || []).join("\n");
      let tail = "";
      if (j.status === "error" && (j.error || j.stderr)) {
        tail = "\n\n——— 技术详情 ———\n";
        if (j.error) tail += j.error + "\n";
        if (j.stderr) tail += String(j.stderr).slice(-4000);
      }
      return lines + tail || "（等待日志…）";
    }
    return JSON.stringify(j, null, 2);
  }

  function updatePipelineStageEl(j) {
    const el = $("#pipeline-stage");
    if (!el) return;
    if (j.kind !== "full-pipeline") {
      el.textContent = "";
      el.className = "";
      return;
    }
    el.textContent = stageLabel(j.stage);
    const doneOk = j.stage === "done" && j.status === "done";
    el.className = doneOk ? "done" : "";
  }

  async function pollJob(jobId, logEl, options) {
    const mode = options && options.mode;
    const t0 = Date.now();
    while (Date.now() - t0 < 600000) {
      const j = await fetchJSON("/api/jobs/" + jobId);
      updatePipelineStageEl(j);
      logEl.textContent = formatJobForLog(j, mode);
      if (j.status === "done" || j.status === "error") return j;
      await new Promise((r) => setTimeout(r, 1500));
    }
    throw new Error("轮询超时");
  }

  $("#btn-refresh").addEventListener("click", async () => {
    try {
      const [h, c] = await Promise.all([fetchJSON("/api/health"), fetchJSON("/api/config")]);
      setHealth(h);
      setConfig(c);
      $("#msg").textContent = "";
    } catch (e) {
      $("#msg").textContent = String(e.message || e);
    }
  });

  $("#btn-full-pipeline").addEventListener("click", async () => {
    const log = $("#job-log");
    const btn = $("#btn-full-pipeline");
    log.textContent = "正在启动…";
    $("#msg").textContent = "";
    const ps = $("#pipeline-stage");
    if (ps) {
      ps.textContent = "已排队…";
      ps.className = "";
    }
    btn.disabled = true;
    try {
      const max = parseInt($("#input-max").value, 10) || 6;
      const body = addGoalFieldsToBody({
        max_attempts: max,
        skip_export: $("#chk-skip-export").checked,
      });
      const { job_id } = await fetchJSON("/api/run/full-pipeline", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const j = await pollJob(job_id, log, { mode: "full" });
      if (j.status === "done") {
        $("#msg").textContent = "全部完成。下方已刷新最新正文预览。";
        if (ps) ps.className = "done";
        await loadLatest();
      } else {
        $("#msg").textContent = "未成功完成，请查看日志或确认 Hermes / OpenClaw / Docker 是否正常。";
      }
    } catch (e) {
      $("#msg").textContent = String(e.message || e);
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-export").addEventListener("click", async () => {
    const log = $("#job-log");
    log.textContent = "…";
    $("#msg").textContent = "";
    if ($("#pipeline-stage")) $("#pipeline-stage").textContent = "";
    try {
      const { job_id } = await fetchJSON("/api/export-feed", { method: "POST" });
      await pollJob(job_id, log);
      $("#msg").textContent = "合并完成，可 Docker Up 后跑 Bench。";
    } catch (e) {
      $("#msg").textContent = String(e.message || e);
    }
  });

  $("#btn-docker").addEventListener("click", async () => {
    const log = $("#job-log");
    log.textContent = "…";
    $("#msg").textContent = "";
    if ($("#pipeline-stage")) $("#pipeline-stage").textContent = "";
    try {
      const { job_id } = await fetchJSON("/api/docker-up", { method: "POST" });
      await pollJob(job_id, log);
      $("#msg").textContent = "docker compose 已执行。";
    } catch (e) {
      $("#msg").textContent = String(e.message || e);
    }
  });

  $("#btn-bench").addEventListener("click", async () => {
    const log = $("#job-log");
    log.textContent = "Bench 运行中，约 1～3 分钟…";
    $("#msg").textContent = "";
    if ($("#pipeline-stage")) $("#pipeline-stage").textContent = "";
    try {
      const max = parseInt($("#input-max").value, 10) || 6;
      const body = addGoalFieldsToBody({ max_attempts: max });
      const { job_id } = await fetchJSON("/api/run/bench", {
        method: "POST",
        body: JSON.stringify(body),
      });
      await pollJob(job_id, log);
      $("#msg").textContent = "Bench 结束，可刷新下方「最新导出」。";
      await loadLatest();
    } catch (e) {
      $("#msg").textContent = String(e.message || e);
    }
  });

  $("#btn-latest").addEventListener("click", loadLatest);

  async function loadLatest() {
    const el = $("#latest-preview");
    el.textContent = "加载中…";
    try {
      const d = await fetchJSON("/api/runs/latest");
      if (!d.found) {
        el.textContent = "暂无 outputs/xhs-runs/*.txt";
        return;
      }
      el.textContent = `# ${d.name} (${d.modified})\n\n${d.text}`;
    } catch (e) {
      el.textContent = String(e.message || e);
    }
  }

  $("#btn-refresh").click();
  loadLatest();
})();
