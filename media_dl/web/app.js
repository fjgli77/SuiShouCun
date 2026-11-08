/* 随手存 · 唯一的前端逻辑（PC EXE 与 Android APK 共用同一个文件）
   两端是同一个应用：同一套 DOM、同一套 API；差异只在 style.css 的响应式样式
   和这里的按端能力开关（原生下载 / 浏览器下载、PC 专属选项、退出按钮）。 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const BRIDGE = window.MediaDL || null;
  const IS_APK = !!(BRIDGE && typeof BRIDGE.isApk === "function" && BRIDGE.isApk());
  const NATIVE = !!(BRIDGE && typeof BRIDGE.download === "function");

  // 界面形态由「运行端」决定，不再由窗口宽度决定：
  //   - Android APK 或 runtime=android → 移动端（深色 / 大触控 / 底部固定操作栏）
  //   - PC EXE / 桌面浏览器 → 桌面端（浅色），即便窗口缩得很小也保持桌面布局
  // CFG 是异步加载的，先用 IS_APK 给一个初始值，applyClientUi 拿到 config 后最终确定。
  let MOBILE = IS_APK;
  document.body.classList.add(MOBILE ? "is-mobile" : "is-pc");

  let CFG = {};
  let LAST_ITEMS = [];
  let PREVIEW_ITEM = null;
  let detectTimer = null;

  const SAVE_LABEL = NATIVE ? "下载到手机" : "下载到浏览器";
  const ALL_LABEL = NATIVE ? "全部保存到手机" : "全部下载";

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
  }

  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2800);
  }

  function setStatus(msg) { $("status").textContent = msg; }
  function apiUrl(path) { return location.origin + path; }

  async function api(path, options) {
    const opts = Object.assign({}, options || {});
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    const resp = await fetch(apiUrl(path), opts);
    let data = {};
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
    return data;
  }

  // ------------------------------------------------------------------ //
  // 下载设置
  // ------------------------------------------------------------------ //
  function collectOptions() {
    return {
      quality: $("opt-quality").value,
      max_images: parseInt($("opt-max-images").value, 10) || 30,
      audio_only: $("opt-audio-only").checked,
      remove_watermark: $("opt-remove-watermark").checked,
      write_thumbnail: $("opt-thumbnail").checked,
      page_images: $("opt-page-images").checked,
      expand_shortlinks: $("opt-expand").checked
    };
  }

  function updateWmChip() {
    $("wm-chip").textContent = $("opt-remove-watermark").checked
      ? "去水印 · 免 Cookie" : "原始文件 · 免 Cookie";
  }

  function saveSettings() {
    try { localStorage.setItem("media_dl_options", JSON.stringify(collectOptions())); } catch (e) { /* ignore */ }
    updateWmChip();
  }

  // ------------------------------------------------------------------ //
  // 自定义下拉框：原生 select 的弹出列表样式无法控制（两端都会发灰），
  // 改成按钮 + 浮层列表，PC 浅色 / 移动深色由 style.css 分别渲染。
  // 真实取值仍存在同 id 的 hidden input 里，collectOptions 无需感知。
  // ------------------------------------------------------------------ //
  function closeAllDropdowns() {
    Array.prototype.forEach.call(document.querySelectorAll(".dd.open"), (d) => d.classList.remove("open"));
  }

  function initDropdown(hiddenId) {
    const root = $("dd-" + hiddenId.replace("opt-", ""));
    if (!root) return;
    const hidden = $(hiddenId);
    const btn = root.querySelector(".dd-btn");
    const list = root.querySelector(".dd-list");

    function labelOf(value) {
      const opt = list.querySelector('.dd-opt[data-value="' + value + '"]');
      return opt ? opt.textContent : value;
    }
    function render() {
      const value = hidden.value;
      Array.prototype.forEach.call(list.querySelectorAll(".dd-opt"), (o) => {
        o.classList.toggle("sel", o.dataset.value === value);
      });
      btn.innerHTML = "<span>" + escapeHtml(labelOf(value)) + "</span>";
    }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = root.classList.contains("open");
      closeAllDropdowns();
      if (!wasOpen) root.classList.add("open");
    });
    Array.prototype.forEach.call(list.querySelectorAll(".dd-opt"), (o) => {
      o.addEventListener("click", (e) => {
        e.stopPropagation();
        if (hidden.value !== o.dataset.value) {
          hidden.value = o.dataset.value;
          hidden.dispatchEvent(new Event("change"));   // 复用原有的保存/联动逻辑
        }
        closeAllDropdowns();
      });
    });
    render();
  }

  function loadSettings() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("media_dl_options") || "{}"); } catch (e) { /* ignore */ }
    [["opt-quality", "quality"], ["opt-max-images", "max_images"]].forEach(([id, key]) => {
      if (saved[key] !== undefined) $(id).value = saved[key];
    });
    [["opt-remove-watermark", "remove_watermark"], ["opt-audio-only", "audio_only"],
     ["opt-thumbnail", "write_thumbnail"], ["opt-page-images", "page_images"],
     ["opt-expand", "expand_shortlinks"]
    ].forEach(([id, key]) => {
      if (saved[key] !== undefined) $(id).checked = !!saved[key];
    });
    updateWmChip();
  }

  // ------------------------------------------------------------------ //
  // 粘贴即识别
  // ------------------------------------------------------------------ //
  function updateCount() {
    const found = ($("input").value.match(/https?:\/\/[^\s<>"']+/gi) || []);
    $("input-count").textContent = found.length + " 个链接";
  }

  function renderDetect(results) {
    const el = $("detect-list");
    if (!results || !results.length) { el.innerHTML = ""; return; }
    el.innerHTML = results.slice(0, 12).map((r) => {
      const url = r.url.length > 44 ? r.url.slice(0, 44) + "…" : r.url;
      return '<div class="detect-chip">' +
        '<span class="dp">' + escapeHtml(r.platform_name) + "</span>" +
        '<span class="dk ' + escapeHtml(r.kind) + '">' + escapeHtml(r.kind_name) + "</span>" +
        (r.shortlink ? '<span class="ds">短链</span>' : "") +
        '<span class="du">' + escapeHtml(url) + "</span></div>";
    }).join("");
  }

  async function runDetect() {
    const text = $("input").value.trim();
    if (!text) { renderDetect([]); return; }
    try {
      const data = await api("/api/detect", { method: "POST", body: JSON.stringify({ text }) });
      renderDetect(data.results);
    } catch (e) { /* 服务未就绪时保持空白 */ }
  }

  // ------------------------------------------------------------------ //
  // 解析结果 / 预览 / 下载（唯一的分叉点：原生下载 vs 浏览器下载）
  // ------------------------------------------------------------------ //
  function downloadHref(item) { return apiUrl(item.download_url); }
  function previewHref(item) { return apiUrl(item.preview_url); }
  function thumbHref(item) { return apiUrl(item.thumb_url); }

  function triggerDownload(item) {
    const url = downloadHref(item);
    if (NATIVE) {
      try {
        const started = BRIDGE.download(url, item.title || "media");
        if (started === false) { toast("下载启动失败，请重试"); return; }
        toast("已开始直接保存到手机本地");
        setStatus("正在保存到手机本地：" + item.title);
      } catch (e) {
        toast("调用本地下载失败：" + e.message);
      }
      return;
    }
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { if (a.parentNode) document.body.removeChild(a); }, 1500);
  }

  // Java 原生下载线程通过 evaluateJavascript 回调这里
  window.onNativeDownload = function (state, filename, message) {
    if (state === "done") {
      setStatus("已保存到手机相册（DCIM/随手存）：" + filename);
      toast("保存完成：" + filename);
    } else if (state === "error") {
      setStatus("保存失败：" + (message || "未知错误"));
      toast("保存失败：" + (message || "未知错误"));
    } else if (state === "progress") {
      setStatus("正在保存到手机本地… " + (message || ""));
    }
  };

  function renderErrors(results) {
    const lines = [];
    (results || []).forEach((r) => {
      if (r.error) lines.push({ tag: "解析失败", cls: "err", url: r.url, msg: r.error });
      (r.warnings || []).forEach((w) => lines.push({ tag: "提示", cls: "warn", url: r.url, msg: w }));
    });
    if (!LAST_ITEMS.length && !lines.length) {
      lines.push({ tag: "无结果", cls: "warn", url: "", msg: "没有可下载内容，请确认链接有效后重试" });
    }
    $("result-errors").innerHTML = lines.map((l) =>
      '<div class="err-line ' + l.cls + '"><span class="tag ' + (l.cls === "err" ? "err" : "") + '">' +
      escapeHtml(l.tag) + "</span> " +
      (l.url ? '<span class="err-url">' + escapeHtml(l.url) + "</span> — " : "") +
      escapeHtml(l.msg) + "</div>").join("");
  }

  function renderResults(data) {
    LAST_ITEMS = data.items || [];
    const results = data.results || [];
    $("result-card").classList.remove("hidden");
    $("result-summary").textContent = LAST_ITEMS.length + " 个可下载项";
    $("btn-download-all").textContent = ALL_LABEL;
    renderErrors(results);

    if (!LAST_ITEMS.length) {
      $("result-list").innerHTML = '<p class="muted">没有可下载的内容，请查看上方失败原因后调整链接重试。</p>';
      $("result-tip").textContent = "解析失败原因已列在上方。";
      $("btn-download-all").style.display = "none";
      return;
    }

    $("btn-download-all").style.display = "";
    $("result-list").innerHTML = LAST_ITEMS.map((item) => {
      const badges = '<span class="tag">' + escapeHtml(item.platform_name) + "</span>" +
        '<span class="tag kind">' + escapeHtml(item.kind_name) + "</span>" +
        (item.watermark_free ? '<span class="tag ok">无水印</span>' :
          '<span class="tag">原始文件</span>') +
        (item.count > 1 ? '<span class="tag">' + item.count + " 张 · zip</span>" : "");
      return '<div class="result-item">' +
        '<div class="ri-main">' +
          '<div class="ri-thumb"><img loading="lazy" src="' + escapeHtml(thumbHref(item)) +
            '" alt="封面" onerror="this.style.visibility=\'hidden\'"></div>' +
          '<div class="ri-info"><div class="ri-title">' + escapeHtml(item.title) + "</div>" +
            '<div class="ri-badges">' + badges + "</div></div>" +
        "</div>" +
        '<div class="ri-actions">' +
          '<button class="btn btn-sm pv-one" data-i="' + item.index + '">预览</button>' +
          '<button class="btn btn-sm btn-primary dl-one" data-i="' + item.index + '">' +
            (NATIVE ? "保存到手机" : "下载") + "</button>" +
        "</div></div>";
    }).join("");

    $("result-tip").textContent = NATIVE
      ? "点「预览」查看图片或播放视频；点「保存到手机」图片 / 视频直接写入手机相册（DCIM/随手存），图集打包为 zip 存到下载目录。"
      : "点「预览」查看图片或播放视频；点「下载」由浏览器保存。图集自动打包为 zip。" +
        (LAST_ITEMS.length > 1 ? "浏览器若提示允许多个下载，请选择允许。" : "");

    Array.prototype.forEach.call(document.querySelectorAll(".dl-one"), (btn) => {
      btn.addEventListener("click", () => {
        const item = LAST_ITEMS[parseInt(btn.dataset.i, 10)];
        if (item) { triggerDownload(item); if (!NATIVE) toast("已交给浏览器下载：" + item.title); }
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".pv-one"), (btn) => {
      btn.addEventListener("click", () => {
        const item = LAST_ITEMS[parseInt(btn.dataset.i, 10)];
        if (item) openPreview(item);
      });
    });
  }

  function openPreview(item) {
    PREVIEW_ITEM = item;
    $("preview-title").textContent = item.title;
    const src = escapeHtml(previewHref(item));
    if (item.preview_kind === "image") {
      $("preview-body").innerHTML = '<img class="pv-img" src="' + src + '" alt="预览图片">';
    } else {
      $("preview-body").innerHTML = '<video class="pv-video" controls autoplay playsinline ' +
        'src="' + src + '">当前设备不支持视频预览</video>';
    }
    $("preview-modal").classList.remove("hidden");
  }

  function closePreview() {
    $("preview-body").innerHTML = "";      // 释放视频/图片请求
    $("preview-modal").classList.add("hidden");
    PREVIEW_ITEM = null;
  }

  function setBusy(busy) {
    $("btn-download").disabled = busy;
    $("btn-download").textContent = busy ? "解析中..." : SAVE_LABEL;
    $("btn-parse").disabled = busy;
  }

  async function parseAndDownload(auto) {
    const text = $("input").value.trim();
    if (!text) { toast("请先粘贴链接或分享口令"); return; }
    setBusy(true);
    $("result-summary").textContent = "解析中...";
    setStatus("正在解析链接，请稍候…");
    try {
      saveSettings();
      const data = await api("/api/parse", {
        method: "POST", body: JSON.stringify({ text, options: collectOptions() })
      });
      renderResults(data);
      if (auto && (data.items || []).length) {
        triggerDownload(data.items[0]);
        setStatus(NATIVE ? "已开始直接保存到手机本地" : "已交给浏览器下载（共 " + data.items.length + " 项）");
      } else {
        setStatus("解析完成 · 共 " + (data.items || []).length + " 项，点「预览」可先看内容");
      }
    } catch (e) {
      renderErrors([{ url: text.slice(0, 120), error: e.message, warnings: [] }]);
      $("result-card").classList.remove("hidden");
      $("result-list").innerHTML = "";
      $("btn-download-all").style.display = "none";
      setStatus("解析失败：" + e.message);
      toast("解析失败：" + e.message);
    } finally {
      setBusy(false);
    }
  }

  function downloadAll() {
    if (!LAST_ITEMS.length) { toast("请先解析链接"); return; }
    LAST_ITEMS.forEach((item, i) => setTimeout(() => triggerDownload(item), i * 420));
    setStatus(NATIVE ? "已开始把 " + LAST_ITEMS.length + " 项保存到手机" :
      "已把 " + LAST_ITEMS.length + " 项交给浏览器下载");
  }

  async function pasteFromClipboard() {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) { toast("剪贴板为空"); return; }
      const box = $("input");
      box.value = (box.value.trim() ? box.value.trim() + "\n" : "") + text.trim();
      updateCount();
      runDetect();
      toast("已粘贴 " + text.trim().length + " 个字符");
    } catch (e) {
      $("input").focus();
      toast("无法读取剪贴板，请长按输入框粘贴（PC 按 Ctrl+V）");
    }
  }

  // ------------------------------------------------------------------ //
  // 初始化：按端裁剪文案与功能
  // ------------------------------------------------------------------ //
  function applyClientUi() {
    // 以运行端为准（覆盖启动时的初始值）：APK 或 runtime=android 才是移动端
    MOBILE = IS_APK || (CFG && CFG.runtime === "android");
    document.body.classList.remove("is-mobile", "is-pc");
    document.body.classList.add(MOBILE ? "is-mobile" : "is-pc");
    $("edition").textContent = IS_APK ? "Android" : (CFG.runtime === "android" ? "Android Web" : "电脑端");
    $("subtitle").textContent = MOBILE
      ? "粘贴链接 · 直接存到手机 · 免 Cookie"
      : "自动识别平台与类型 · 免 Cookie · 免代理 · 可选去水印 · 结果可预览";
    $("btn-download").textContent = SAVE_LABEL;
    $("btn-download-all").textContent = ALL_LABEL;
    $("btn-preview-download").textContent = NATIVE ? "保存到手机" : "下载此项";
    // PC 专属功能：退出按钮；移动端隐藏
    const pcOnly = !MOBILE;
    $("btn-exit").style.display = pcOnly ? "" : "none";
    // 移动端默认收起设置，减少首屏纵向占用
    if (MOBILE) {
      $("settings").classList.add("collapsed");
      $("settings-arrow").textContent = "▸";
    }
  }

  async function boot() {
    try {
      CFG = await api("/api/config");
      $("state-pill").textContent = "已就绪 · v" + (CFG.version || "-") + " · 本机";
      $("state-pill").className = "pill ok";
      $("nowm-pill").textContent = "可去水印";
      $("platforms").innerHTML = (CFG.platforms || []).map((p) =>
        '<span class="platform-chip">' + escapeHtml(p) + "</span>").join("");
      applyClientUi();
      updateCount();
      runDetect();
      setStatus(NATIVE ? "已就绪 · 粘贴链接后点下方按钮，文件直接保存到手机本地"
                       : "已就绪 · 粘贴链接后点下方按钮");
    } catch (e) {
      $("state-pill").textContent = "服务未启动";
      $("state-pill").className = "pill warn";
      setStatus("连不上本机服务：" + e.message);
      toast("无法连接本机服务：" + e.message);
      applyClientUi();
    }
  }

  function init() {
    const params = new URLSearchParams(location.search);
    const sharedText = params.get("text");
    params.delete("text");      // 分享带入的链接用完后从地址栏移除
    history.replaceState(null, "", location.pathname +
      (params.toString() ? "?" + params.toString() : ""));
    if (sharedText) { $("input").value = sharedText; toast("已从分享带入链接"); }
    loadSettings();
    ["opt-quality", "opt-max-images"].forEach(initDropdown);
    updateCount();
    boot();
    if (!IS_APK && "serviceWorker" in navigator && window.isSecureContext) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
  }

  $("input").addEventListener("input", () => {
    updateCount();
    clearTimeout(detectTimer);
    detectTimer = setTimeout(runDetect, 300);
  });
  $("btn-download").addEventListener("click", () => parseAndDownload(true));
  $("btn-parse").addEventListener("click", () => parseAndDownload(false));
  $("btn-download-all").addEventListener("click", downloadAll);
  $("btn-paste").addEventListener("click", pasteFromClipboard);
  $("btn-clear").addEventListener("click", () => {
    $("input").value = ""; updateCount(); renderDetect([]);
    $("result-card").classList.add("hidden"); LAST_ITEMS = [];
    setStatus("已清空 · 粘贴链接后点下方按钮即可下载");
  });
  $("btn-exit").addEventListener("click", async () => {
    if (!window.confirm("确定要退出随手存电脑端吗？")) return;
    try { await api("/api/shutdown", { method: "POST", body: "{}" }); } catch (e) { /* 服务已停止 */ }
    toast("桌面端已退出，可以关闭此窗口");
  });
  $("btn-preview-close").addEventListener("click", closePreview);
  $("btn-preview-download").addEventListener("click", () => {
    if (PREVIEW_ITEM) triggerDownload(PREVIEW_ITEM);
  });
  $("settings-head").addEventListener("click", () => {
    const el = $("settings");
    el.classList.toggle("collapsed");
    $("settings-arrow").textContent = el.classList.contains("collapsed") ? "▸" : "▾";
  });
  $("opt-remove-watermark").addEventListener("change", saveSettings);
  ["opt-quality", "opt-max-images", "opt-page-images", "opt-expand",
   "opt-audio-only", "opt-thumbnail"].forEach((id) => {
    $(id).addEventListener("change", saveSettings);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeAllDropdowns(); closePreview(); }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") parseAndDownload(true);
  });
  document.addEventListener("click", closeAllDropdowns);
  init();
})();
