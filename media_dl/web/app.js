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

  // ------------------------------------------------------------------ //
  // PC 桌面通知：任务结束（完成 / 失败 / 取消）时提醒。
  //   仅在页面处于后台（document.hidden）时打扰，前台用户已能看到任务卡；
  //   权限在用户首次创建任务时惰性申请（不弹在首屏）。
  // ------------------------------------------------------------------ //
  const NOTIFY_KEY = "media_dl_notify";   // "1" 开启 / "0" 关闭 / 缺省 = 开启

  function notifyEnabled() {
    if (NATIVE || !("Notification" in window)) return false;
    try { return localStorage.getItem(NOTIFY_KEY) !== "0"; } catch (e) { return true; }
  }

  /** 惰性申请通知权限：失败 / 拒绝都静默，不影响主流程。 */
  function ensureNotifyPermission() {
    if (!notifyEnabled() || Notification.permission !== "default") return;
    try { Notification.requestPermission().catch(() => {}); } catch (e) { /* 忽略 */ }
  }

  function notifyDesktop(title, body) {
    if (!notifyEnabled() || !document.hidden || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, { body, tag: "media-dl-job", silent: false });
      n.onclick = () => { window.focus(); n.close(); };
      setTimeout(() => n.close(), 6000);
    } catch (e) { /* 部分环境构造即抛错，忽略 */ }
  }

  // 通知开关放在头部主题按钮旁（移动端无通知能力，直接隐藏）
  function initNotifyToggle() {
    const btn = $("btn-notify");
    if (!btn) return;
    btn.style.display = notifyEnabled() || "Notification" in window ? "" : "none";
    if (NATIVE) { btn.style.display = "none"; return; }
    const paint = () => { btn.textContent = notifyEnabled() ? "🔔" : "🔕"; };
    paint();
    btn.addEventListener("click", () => {
      const on = notifyEnabled();
      try { localStorage.setItem(NOTIFY_KEY, on ? "0" : "1"); } catch (e) { /* 忽略 */ }
      paint();
      if (!on) ensureNotifyPermission();   // 刚打开：补申请权限
      toast(on ? "桌面通知已关闭" : "桌面通知已开启（任务结束后提醒）");
    });
  }

  // ------------------------------------------------------------------ //
  // 深浅色主题（PC 端）：默认跟随系统，点 🌙/☀️ 手动切换并记住。
  //   移动端本来就是深色视觉（body.is-mobile），不参与本开关。
  // ------------------------------------------------------------------ //
  const THEME_KEY = "media_dl_theme";   // "auto" | "light" | "dark"
  const mqDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function systemDark() { return !!(mqDark && mqDark.matches); }
  function themePref() {
    try { return localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { return "auto"; }
  }

  /** 计算并落主题：auto → 跟随系统；light/dark → 手动指定。 */
  function applyTheme() {
    if (MOBILE) { document.body.classList.remove("theme-dark"); return; }
    const pref = themePref();
    const dark = pref === "dark" || (pref === "auto" && systemDark());
    document.body.classList.toggle("theme-dark", dark);
    const btn = $("btn-theme");
    if (btn) {
      btn.textContent = dark ? "☀️" : "🌙";
      btn.title = dark ? "切换到浅色模式" : "切换到深色模式";
    }
  }

  function toggleTheme() {
    // 在「亮 ↔ 暗」之间切换，并把结果存为显式偏好（不再是 auto）
    const dark = document.body.classList.contains("theme-dark");
    try { localStorage.setItem(THEME_KEY, dark ? "light" : "dark"); } catch (e) { /* 忽略 */ }
    applyTheme();
    toast(document.body.classList.contains("theme-dark") ? "已切换到深色模式" : "已切换到浅色模式");
  }

  // 跟随系统的前提下，系统切换深浅色时实时响应（仅 auto 模式）
  if (mqDark) {
    const onSysTheme = () => { if (themePref() === "auto") applyTheme(); };
    if (mqDark.addEventListener) mqDark.addEventListener("change", onSysTheme);
    else if (mqDark.addListener) mqDark.addListener(onSysTheme);   // 旧 WebView 兼容
  }
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
      expand_shortlinks: $("opt-expand").checked,
      cookie: ($("opt-cookie") ? $("opt-cookie").value.trim() : "")
    };
  }

  // 移动端顶栏不做文案铺陈，底部也不显示水印提示徽标；只在 PC 上更新这枚文字，
  // 避免移动端（已用 CSS 隐藏）在切换「去除水印」时还去写一个不可见节点。
  function updateWmChip() {
    if (!$("wm-chip") || MOBILE) return;
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
    Array.prototype.forEach.call(document.querySelectorAll(".result-item.dd-active"),
                                     (d) => d.classList.remove("dd-active"));
  }

  // 浮层定位：默认向下展开；下方空间不足时向上翻转，避免被视口 / 卡片下边界裁切
  // （结果条里的画质下拉在卡片底部，向上展开更稳妥）。同时给所属结果条加 dd-active，
  // 让整条抬升到相邻结果条之上（比依赖 :has() 更稳，老 WebView 也生效）。
  function positionDropdown(root) {
    const list = root.querySelector(".dd-list");
    const btn = root.querySelector(".dd-btn");
    if (!list || !btn) return;
    root.classList.remove("drop-up");
    const owner = root.closest(".result-item");
    if (owner) owner.classList.add("dd-active");
    const rect = btn.getBoundingClientRect();
    const need = Math.min(list.scrollHeight || 240, 300) + 14;
    if (window.innerHeight - rect.bottom < need && rect.top > need) {
      root.classList.add("drop-up");
    }
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
      if (!wasOpen) { root.classList.add("open"); positionDropdown(root); }
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
    if (saved.cookie !== undefined && $("opt-cookie")) $("opt-cookie").value = saved.cookie;
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

  function qualityOf(item) {
    // 视频：解析出的真实清晰度（默认最高）作为按条覆盖；无档位时沿用全局画质上限
    if (item.kind !== "video" || !item.qualities || !item.qualities.length) return "";
    const root = document.querySelector('.q-dd[data-i="' + item.index + '"]');
    let h = item.qualities[0];
    if (root) {
      const sel = root.querySelector(".dd-opt.sel");
      if (sel) h = parseInt(sel.dataset.value, 10);
    }
    return h ? ("&q=" + h) : "";
  }

  // 结果栏逐条画质选择：用与全局设置一致的自定义下拉（两端通用、触控友好、深色适配）
  function initItemQualities() {
    Array.prototype.forEach.call(document.querySelectorAll(".q-dd"), (root) => {
      const btn = root.querySelector(".dd-btn");
      const list = root.querySelector(".dd-list");
      const opts = list.querySelectorAll(".dd-opt");
      const first = opts[0];
      if (first) {
        first.classList.add("sel");
        btn.innerHTML = "<span>" + escapeHtml(first.textContent) + "</span>";
      }
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasOpen = root.classList.contains("open");
        closeAllDropdowns();
        if (!wasOpen) { root.classList.add("open"); positionDropdown(root); }
      });
      Array.prototype.forEach.call(opts, (o) => {
        o.addEventListener("click", (e) => {
          e.stopPropagation();
          Array.prototype.forEach.call(opts, (x) => x.classList.remove("sel"));
          o.classList.add("sel");
          btn.innerHTML = "<span>" + escapeHtml(o.textContent) + "</span>";
          closeAllDropdowns();
        });
      });
    });
  }

  // ------------------------------------------------------------------ //
  // 下载任务中心：把「黑盒下载」变成可见的任务列表（进度 / 速度 / 剩余 / 取消）
  //   - 移动端：Java 原生下载，进度经 JS Bridge 回调（taskId 归属）
  //   - PC 端：走后端 /api/jobs 后台任务，前端轮询进度
  // 两种实现共用一个 TASKS 表与同一套渲染，界面表现一致。
  // ------------------------------------------------------------------ //
  const TASKS = new Map();          // key -> { key, title, status, percent, downloaded, total, speed, eta, message, cancel, _pcJob }
  const PC_JOBS = new Map();        // jobId -> { timer, keys:Set }
  const TERMINAL = { done: 1, error: 1, cancelled: 1, existed: 1 };

  const STATUS_TEXT = { pending: "排队中", downloading: "下载中", done: "已完成",
    error: "失败", cancelled: "已取消", skipped: "已跳过", existed: "已存在（未重新下载）" };

  // 移动端原生下载并发治理：同时最多 N 个，其余排队，避免图集一次性打满线程池
  const NATIVE_CONCURRENCY = 3;
  const nativeQueue = [];           // [{ key, url, title, taskId }]
  let nativeActive = 0;
  const nativeCancelled = new Set(); // 排队阶段被取消的 taskId

  /** 入队并尽量启动下一个原生下载。 */
  function enqueueNative(url, title, taskId) {
    nativeQueue.push({ url: url, title: title, taskId: taskId });
    upsertTask(taskId, { title: title || "媒体文件", status: "pending" });
    pumpNativeQueue();
  }

  function pumpNativeQueue() {
    while (nativeActive < NATIVE_CONCURRENCY && nativeQueue.length) {
      const job = nativeQueue.shift();
      if (nativeCancelled.has(job.taskId)) { nativeCancelled.delete(job.taskId); continue; }
      nativeActive++;
      let started = false;
      try { started = BRIDGE.download(job.url, job.title, job.taskId) !== false; }
      catch (e) { started = false; }
      if (!started) {
        nativeActive--;
        upsertTask(job.taskId, { status: "error", message: "下载启动失败" });
        continue;
      }
      upsertTask(job.taskId, { status: "downloading" });
    }
  }

  /** 某个原生下载结束（终态）后释放并发位并启动排队的下一个。 */
  function releaseNativeSlot(taskId) {
    if (nativeActive > 0) nativeActive--;
    pumpNativeQueue();
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
  }
  function fmtSpeed(bps) {
    if (!bps || bps <= 0) return "";
    return fmtBytes(bps) + "/s";
  }
  function fmtEta(sec) {
    if (sec == null || sec < 0) return "";
    if (sec < 60) return sec + " 秒";
    return Math.floor(sec / 60) + " 分 " + (sec % 60) + " 秒";
  }

  function upsertTask(key, patch) {
    const cur = TASKS.get(key) || { key: key, percent: 0, downloaded: 0, total: 0, speed: 0, eta: null, status: "pending", title: "" };
    Object.assign(cur, patch || {});
    // 计算瞬时速度（原生端不回报速度，由前端按字节差估算）
    const now = Date.now();
    if (cur.status === "downloading" && cur.downloaded) {
      if (cur._t && cur._b != null && now > cur._t) {
        const inst = (cur.downloaded - cur._b) / ((now - cur._t) / 1000);
        if (inst > 0) cur.speed = inst;
      }
      cur._t = now; cur._b = cur.downloaded;
    }
    if (cur.total > 0) cur.percent = Math.max(0, Math.min(100, cur.downloaded / cur.total * 100));
    else if (cur.status === "done") cur.percent = 100;
    TASKS.set(key, cur);
    renderJobs();
    return cur;
  }

  function taskRowHtml(t) {
    const st = t.status || "pending";
    const terminal = !!TERMINAL[st];
    const pct = (st === "done") ? 100 : (t.total > 0 ? t.percent : 0);
    const indeterminate = (st === "downloading" || st === "pending") && !(t.total > 0);
    const meta = [];
    if (t.total > 0) meta.push(fmtBytes(t.downloaded) + " / " + fmtBytes(t.total));
    else if (t.downloaded > 0) meta.push(fmtBytes(t.downloaded));
    if (st === "downloading" && t.speed > 0) meta.push(fmtSpeed(t.speed));
    if (st === "downloading" && t.eta != null) meta.push("剩 " + fmtEta(t.eta));
    if (terminal) meta.push(STATUS_TEXT[st] || st);
    if (st === "error" && t.message) meta.push(t.message);
    const bar = '<div class="job-bar' + (indeterminate ? " indet" : "") + '"><i style="width:' +
      (indeterminate ? 38 : pct).toFixed(1) + '%"></i></div>';
    const cancelBtn = terminal ? "" :
      '<button class="job-cancel" data-cancel="' + escapeHtml(t.key) + '">取消</button>';
    // 失败 / 取消的任务卡提供「重试」：新建一个只含失败条目的下载任务（续传 .part）
    const retryBtn = (st === "error" || st === "cancelled") && t._pcJob ?
      '<button class="job-cancel retry" data-retry="' + escapeHtml(t._pcJob) + '">重试</button>' : "";
    // 状态徽标：排队中 / 下载中 / 已完成 / 失败 / 已取消 / 已存在
    const badge = '<span class="tag' + (st === "done" || st === "existed" ? " ok" : (st === "error" ? " err" : "")) + '">' +
      escapeHtml(STATUS_TEXT[st] || st) + "</span>";
    return '<div class="job-item ' + st + '">' +
      '<div class="job-top">' +
        '<span class="job-title">' + escapeHtml(t.title || "媒体文件") + "</span>" +
        badge + retryBtn + cancelBtn +
      "</div>" + bar +
      '<div class="job-meta">' + escapeHtml(meta.join(" · ")) + "</div>" +
    "</div>";
  }

  function renderJobs() {
    const list = $("jobs-list");
    if (!list) return;
    const all = Array.from(TASKS.values());
    const card = $("jobs-card");
    if (!all.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    // 进行中在前，已结束在后；同类按加入顺序
    all.sort((a, b) => (TERMINAL[a.status] ? 1 : 0) - (TERMINAL[b.status] ? 1 : 0));
    $("jobs-summary").textContent = all.length + " 项 · " +
      all.filter((t) => t.status === "done").length + " 已完成";
    list.innerHTML = all.map(taskRowHtml).join("");
    Array.prototype.forEach.call(list.querySelectorAll("[data-cancel]"), (b) => {
      b.addEventListener("click", () => cancelTask(b.dataset.cancel));
    });
    Array.prototype.forEach.call(list.querySelectorAll("[data-retry]"), (b) => {
      b.addEventListener("click", () => retryJob(b.dataset.retry));
    });
  }

  /** 重试失败 / 已取消的任务：调后端 /retry 新建任务并开始轮询。 */
  function retryJob(jid) {
    api("/api/jobs/" + encodeURIComponent(jid) + "/retry", { method: "POST", body: "{}" })
      .then((res) => {
        if (res && res.id) {
          toast("已创建重试任务");
          pollPcJob(res.id);
        } else {
          toast((res && res.error) || "重试失败");
        }
      })
      .catch(() => toast("重试失败，请稍后再试"));
  }

  function cancelTask(key) {
    const t = TASKS.get(key);
    if (!t) return;
    if (t._pcJob) {
      // PC：取消整个批任务
      api("/api/jobs/" + encodeURIComponent(t._pcJob) + "/cancel", { method: "POST", body: "{}" })
        .catch(() => {});
    } else if (t.status === "pending") {
      // 移动端：还在排队，未真正开始下载 —— 标记后由队列跳过，直接落终态
      nativeCancelled.add(key);
      upsertTask(key, { status: "cancelled" });
      toast("已取消排队项");
      return;
    } else if (NATIVE && BRIDGE && typeof BRIDGE.cancelDownload === "function") {
      try { BRIDGE.cancelDownload(key); } catch (e) { /* 忽略 */ }
    }
    upsertTask(key, { status: "cancelled" });
    toast("已请求取消");
  }

  /** PC 端：把一批链接交给后端后台任务下载，前端轮询进度。 */
  async function startPcJob(text) {
    ensureNotifyPermission();   // 首次创建任务时惰性申请通知权限
    let data;
    try {
      data = await api("/api/jobs", { method: "POST", body: JSON.stringify({ text: text, options: collectOptions() }) });
    } catch (e) {
      toast("创建任务失败：" + e.message);
      return null;
    }
    const jid = data.id;
    PC_JOBS.set(jid, { timer: null, keys: new Set() });
    upsertTask(jid, { title: "解析中…", status: "downloading", _pcJob: jid, _placeholder: true });
    pollPcJob(jid);
    return jid;
  }

  function pollPcJob(jid) {
    const rec = PC_JOBS.get(jid);
    if (!rec) return;
    rec.timer = setTimeout(async () => {
      let job;
      try {
        job = await api("/api/jobs/" + encodeURIComponent(jid));
      } catch (e) {
        upsertTask(jid, { status: "error", message: "任务查询失败" });
        PC_JOBS.delete(jid);
        return;
      }
      const cards = job.cards || {};
      const keys = new Set();
      const entries = Object.keys(cards);
      if (!entries.length) {
        upsertTask(jid, { title: "解析中…", status: job.status === "error" ? "error" : "downloading" });
        keys.add(jid);
      } else {
        // 移除仅用于占位的批次行，改为逐条展示
        if (TASKS.has(jid) && TASKS.get(jid)._placeholder) TASKS.delete(jid);
        entries.forEach((eid) => {
          const c = cards[eid] || {};
          const key = jid + "|" + eid;
          keys.add(key);
          const st = c.status === "finished" ? "done" : (c.status || "pending");
          upsertTask(key, {
            title: c.title || "媒体文件",
            status: st,
            downloaded: c.downloaded || 0,
            total: c.total || 0,
            percent: c.percent || 0,
            eta: c.eta,
            message: c.message || "",
            _pcJob: jid,
          });
        });
      }
      // 清理本任务内已消失的旧键
      (rec.keys || new Set()).forEach((k) => { if (!keys.has(k)) TASKS.delete(k); });
      rec.keys = keys;

      if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
        // 收尾：把尚在进行的行落成终态
        entries.forEach((eid) => {
          const key = jid + "|" + eid;
          const t = TASKS.get(key);
          if (t && !TERMINAL[t.status]) {
            upsertTask(key, { status: t.percent >= 100 ? "done" : "error",
              message: t.message || "任务已结束" });
          }
        });
        // 仅在「始终没解析出任何条目」时才保留一条批次汇总行
        if (TASKS.has(jid)) {
          const ok = job.status === "done";
          upsertTask(jid, {
            status: job.status === "error" ? "error" : (job.status === "cancelled" ? "cancelled" : "done"),
            title: ok ? "下载完成" : (job.status === "cancelled" ? "已取消" : "任务失败"),
            downloaded: 1, total: 1,
          });
        }
        PC_JOBS.delete(jid);
        if (job.status === "done") {
          toast("下载任务已结束");
          notifyDesktop("下载完成",
            (job.cards && Object.keys(job.cards).length || 1) + " 项已下载到本地");
          if (libraryOpen()) refreshLibrary(false);
        } else if (job.status === "error") {
          notifyDesktop("下载任务失败",
            (job.errors && job.errors[0] || "请回到页面查看原因").slice(0, 90));
        }
        return;
      }
      pollPcJob(jid);
    }, 900);
  }

  function clearFinishedTasks() {
    Array.from(TASKS.keys()).forEach((k) => {
      const t = TASKS.get(k);
      if (t && TERMINAL[t.status]) TASKS.delete(k);
    });
    renderJobs();
  }

  function triggerDownload(item) {
    // 移动端图集：不打包 zip，逐张图片单独下载到手机本地（DCIM/随手存）
    if (NATIVE && item.kind === "gallery" && item.count > 1) {
      downloadGalleryAsImages(item);
      return;
    }
    const url = downloadHref(item) + qualityOf(item);
    if (NATIVE) {
      const taskId = "t" + Date.now() + "_" + item.index;
      enqueueNative(url, item.title || "media", taskId);
      setStatus("正在保存到手机本地：" + item.title);
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

  // 移动端：图集逐张下载。每张用 &zip=0&j=<序号> 取单图，Java 原生写入 DCIM/随手存
  function downloadGalleryAsImages(item) {
    const base = apiUrl(item.download_url);
    for (let j = 0; j < item.count; j++) {
      const url = base + "&zip=0&j=" + j;
      const taskId = "g" + Date.now() + "_" + item.index + "_" + j;
      enqueueNative(url, item.title + "_" + (j + 1), taskId);
    }
    toast("已开始把 " + item.count + " 张图片逐张保存到手机本地");
    setStatus("正在把 " + item.count + " 张图片逐张保存到手机本地（DCIM/随手存）");
  }

  // Java 原生下载线程通过 evaluateJavascript 回调这里。
  // state: progress | done | error | cancelled；taskId 用于归属到任务中心的对应行；
  // message 在 progress 时是进度 JSON（{bytes,total,speed}），其余为文件名/错误原因。
  window.onNativeDownload = function (state, filename, taskId, message) {
    const key = taskId || "";
    if (state === "progress") {
      let info = {};
      try { info = JSON.parse(message || "{}"); } catch (e) { info = {}; }
      upsertTask(key, {
        title: TASKS.get(key) ? TASKS.get(key).title : (filename || "媒体文件"),
        status: "downloading",
        downloaded: info.bytes || 0,
        total: info.total || 0,
      });
      setStatus("正在保存到手机本地… " + fmtBytes(info.bytes || 0));
      return;
    }
    if (state === "done") {
      const t = TASKS.get(key) || {};
      upsertTask(key, { title: t.title || filename, status: "done",
        downloaded: t.total || t.downloaded, total: t.total || t.downloaded });
      releaseNativeSlot(key);
      setStatus("已保存到手机相册（DCIM/随手存）：" + filename);
      toast("保存完成：" + filename);
      if (libraryOpen()) refreshLibrary(false);
      return;
    }
    if (state === "cancelled") {
      upsertTask(key, { status: "cancelled", title: (TASKS.get(key) || {}).title || filename });
      releaseNativeSlot(key);
      setStatus("已取消保存");
      return;
    }
    if (state === "error") {
      upsertTask(key, { status: "error", title: (TASKS.get(key) || {}).title || filename,
        message: message || "未知错误" });
      releaseNativeSlot(key);
      setStatus("保存失败：" + (message || "未知错误"));
      toast("保存失败：" + (message || "未知错误"));
    }
  };

  // 移动端抖音登录态同步完成后，Java 端回调这里刷新界面状态
  window.onCookieSaved = function () {
    refreshCookieStatus();
    toast("已自动获取移动端抖音登录态并保存");
  };

  /**
   * Android 系统分享 / 「用其他应用打开」带入的链接。
   * Java 端在页面就绪后通过 evaluateJavascript 调这里（push 通道）；
   * 页面就绪前攒下的链接则由 boot() 末尾调用 takeSharedText() 主动拉取（pull 通道）。
   */
  window.onSharedLink = function (text) {
    const t = (text || "").trim();
    if (!t) return;
    ingestShared(t);
  };

  /** 把分享带来的文本填入输入框并立即识别；纯链接与带口令文本都支持。 */
  function ingestShared(text) {
    const box = $("input");
    // 同一个链接不重复追加（避免 push + pull 两次进入）
    if (box.value.indexOf(text) !== -1) return;
    box.value = (box.value.trim() ? box.value.trim() + "\n" : "") + text;
    updateCount();
    runDetect();
    toast("已从分享带入链接，点「下载」即可保存");
    setStatus("已从分享带入链接 · 点下方按钮即可保存到手机");
    $("result-card").classList.add("hidden");
    // 分享场景下用户意图明确，滚到顶部让操作栏可见
    try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch (e) { window.scrollTo(0, 0); }
  }

  /** 合集 / 系列提示条：解析出「同链接多条视频」时展示，一键全部下载。 */
  function renderCollectionTip(results) {
    const el = $("collection-tip");
    if (!el) return;
    const col = (results || []).find((r) => r.collection && r.collection.count > 1);
    if (!col) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    const c = col.collection;
    el.classList.remove("hidden");
    el.innerHTML =
      '<div class="coll-main"><span class="coll-badge">合集</span>' +
      '<span class="coll-title">' + escapeHtml(c.title) + " · 共 " + c.count + " 个视频</span>" +
      '<span class="coll-sub">一次全部下载，无需逐个点</span></div>' +
      '<button class="btn btn-sm btn-primary" id="btn-col-download">' +
      (NATIVE ? "全部保存到手机" : "全部下载") + "</button>";
    const btn = $("btn-col-download");
    if (btn) btn.addEventListener("click", downloadAll);
  }

  function renderErrors(results) {
    const lines = [];
    (results || []).forEach((r) => {
      if (r.error) lines.push({ tag: "解析失败", cls: "err", url: r.url, msg: r.error, detail: false });
      (r.warnings || []).forEach((w) => {
        // 「技术细节：」是给排查用的原始异常，收进可折叠区，不直接铺在主文案里
        const technical = w.startsWith("技术细节：");
        lines.push({
          tag: technical ? "详情" : "提示",
          cls: technical ? "detail" : "warn",
          url: r.url,
          msg: technical ? w.slice("技术细节：".length) : w,
        });
      });
    });
    if (!LAST_ITEMS.length && !lines.length) {
      lines.push({ tag: "无结果", cls: "warn", url: "", msg: "没有可下载内容，请确认链接有效后重试" });
    }
    const main = lines.filter((l) => l.cls !== "detail");
    const details = lines.filter((l) => l.cls === "detail");
    let html = main.map((l) =>
      '<div class="err-line ' + l.cls + '"><span class="tag ' + (l.cls === "err" ? "err" : "") + '">' +
      escapeHtml(l.tag) + "</span> " +
      (l.url ? '<span class="err-url">' + escapeHtml(l.url) + "</span> — " : "") +
      escapeHtml(l.msg) + "</div>").join("");
    if (details.length) {
      html += '<details class="err-details"><summary>技术详情（' + details.length + '）</summary>' +
        details.map((l) =>
          '<div class="err-line detail"><span class="err-url">' +
          escapeHtml(l.url || "") + "</span> " + escapeHtml(l.msg) + "</div>").join("") +
        "</details>";
    }
    $("result-errors").innerHTML = html;
  }

  function renderResults(data) {
    LAST_ITEMS = data.items || [];
    const results = data.results || [];
    $("result-card").classList.remove("hidden");
    $("result-summary").textContent = LAST_ITEMS.length + " 个可下载项";
    $("btn-download-all").textContent = ALL_LABEL;
    renderErrors(results);
    renderCollectionTip(data.results || []);

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
        (item.count > 1 && !NATIVE ? '<span class="tag">' + item.count + " 张 · zip</span>" :
          (item.count > 1 ? '<span class="tag">' + item.count + " 张</span>" : ""));
      // 按条清晰度选择：解析出真实档位（如 蓝光/1080/720/540）时给出下拉，默认最高
      let qsel = "";
      if (item.kind === "video" && item.qualities && item.qualities.length) {
        const labels = item.quality_labels || {};
        const optsHtml = item.qualities.map((h) =>
          '<button type="button" class="dd-opt" data-value="' + h + '">' +
          escapeHtml(labels[h] || (h + "P")) + "</button>").join("");
        qsel = '<div class="ri-quality"><span>画质</span>' +
          '<div class="dd q-dd" data-i="' + item.index + '">' +
          '<button type="button" class="dd-btn" aria-haspopup="listbox"></button>' +
          '<div class="dd-list" role="listbox">' + optsHtml + "</div></div></div>";
      }
      return '<div class="result-item">' +
        '<div class="ri-main">' +
          '<div class="ri-thumb"><img loading="lazy" src="' + escapeHtml(thumbHref(item)) +
            '" alt="封面" onerror="this.style.visibility=\'hidden\'"></div>' +
          '<div class="ri-info"><div class="ri-title">' + escapeHtml(item.title) + "</div>" +
            '<div class="ri-badges">' + badges + "</div>" + qsel + "</div>" +
        "</div>" +
        '<div class="ri-actions">' +
          '<button class="btn btn-sm pv-one" data-i="' + item.index + '">预览</button>' +
          '<button class="btn btn-sm btn-primary dl-one" data-i="' + item.index + '">' +
            (NATIVE ? "保存到手机" : "下载") + "</button>" +
        "</div></div>";
    }).join("");

    initItemQualities();

    $("result-tip").textContent = NATIVE
      ? "点「预览」查看图片或播放视频；点「保存到手机」视频 / 单图写入手机相册（DCIM/随手存），图集逐张保存到手机相册。"
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

  async function downloadAll() {
    if (!LAST_ITEMS.length) { toast("请先解析链接"); return; }
    if (NATIVE) {
      // 移动端：逐条入队，靠并发上限（3）自动调度，超出部分显示「排队中」
      LAST_ITEMS.forEach((item) => triggerDownload(item));
      setStatus("已开始把 " + LAST_ITEMS.length + " 项保存到手机（最多同时下载 " +
        NATIVE_CONCURRENCY + " 项）");
      return;
    }
    // PC 端：交给后端后台任务队列，前端可见进度 / 速度 / 取消
    await startPcJob($("input").value.trim());
    setStatus("已创建下载任务 · 共 " + LAST_ITEMS.length + " 项，可在「下载任务」中查看进度");
  }

  async function pasteFromClipboard() {
    // 1) APK：页面在 http://127.0.0.1（非安全上下文），navigator.clipboard 不可用，
    //    必须走 Android 原生桥读剪贴板。
    if (BRIDGE && typeof BRIDGE.readClipboard === "function") {
      let text = "";
      try { text = BRIDGE.readClipboard() || ""; } catch (e) { text = ""; }
      if (text.trim()) { insertPasted(text); return; }
      toast("剪贴板为空，请先复制分享链接");
      return;
    }
    // 2) PC / 普通浏览器：安全上下文可用时走 Clipboard API
    try {
      const text = await navigator.clipboard.readText();
      if (!text) { toast("剪贴板为空"); return; }
      insertPasted(text);
    } catch (e) {
      $("input").focus();
      toast("无法读取剪贴板，请长按输入框粘贴（PC 按 Ctrl+V）");
    }
  }

  /** 把剪贴板文本追加进输入框并触发识别。 */
  function insertPasted(text) {
    const t = (text || "").trim();
    if (!t) { toast("剪贴板为空"); return; }
    const box = $("input");
    box.value = (box.value.trim() ? box.value.trim() + "\n" : "") + t;
    updateCount();
    runDetect();
    toast("已粘贴 " + t.length + " 个字符");
  }

  // 顶栏文案统一在这里决定，保证与最终确定的 MOBILE 一致。
  //   PC：随手存 [电脑端] + 完整副标题 + 「已就绪 · vX · 本机」+「可去水印」
  //   移动：随手存 [Android] + 隐藏副标题 + 仅「vX」版本号（可去水印、底部水印徽标都隐藏）
  function applyHeader() {
    const edition = IS_APK ? "Android"
      : (CFG && CFG.runtime === "android" ? "Android Web" : "电脑端");
    $("edition").textContent = edition;

    $("subtitle").textContent = MOBILE
      ? ""
      : "自动识别平台与类型 · 免 Cookie · 免代理 · 可选去水印 · 结果可预览";
    $("subtitle").style.display = MOBILE ? "none" : "";

    const ver = (CFG && CFG.version) ? CFG.version : "-";
    $("state-pill").textContent = MOBILE
      ? "v" + ver
      : "已就绪 · v" + ver + " · 本机";
    $("state-pill").className = "pill ok";

    $("nowm-pill").textContent = MOBILE ? "" : "可去水印";
    $("nowm-pill").style.display = MOBILE ? "none" : "";
  }

  // ------------------------------------------------------------------ //
  // 初始化：按端裁剪文案与功能
  // ------------------------------------------------------------------ //
  function applyClientUi() {
    // 以运行端为准（覆盖启动时的初始值）：APK 或 runtime=android 才是移动端
    MOBILE = IS_APK || (CFG && CFG.runtime === "android");
    document.body.classList.remove("is-mobile", "is-pc");
    document.body.classList.add(MOBILE ? "is-mobile" : "is-pc");
    applyHeader();
    $("btn-download").textContent = SAVE_LABEL;
    $("btn-download-all").textContent = ALL_LABEL;
    $("btn-preview-download").textContent = NATIVE ? "保存到手机" : "下载此项";
    applyTheme();   // 端别落定后同步主题（移动端恒深色、PC 跟随偏好）
    // PC 专属功能：退出按钮；移动端隐藏
    const pcOnly = !MOBILE;
    $("btn-exit").style.display = pcOnly ? "" : "none";
    // 移动端默认收起设置，减少首屏纵向占用
    if (MOBILE) {
      $("settings").classList.add("collapsed");
      $("settings-arrow").textContent = "▸";
      $("settings-head").setAttribute("aria-expanded", "false");
    }
    // APK：显示「手机号验证码登录抖音自动获取 Cookie」按钮
    if (IS_APK && $("btn-dy-login")) $("btn-dy-login").style.display = "";
  }

  // ------------------------------------------------------------------ //
  // 首启引导：**只在第一次进入时**出现；点过「知道了」之后不再自动弹出。
  //   以服务端 first_run 为准（写在本机持久目录），因此换版本、重启、清浏览器缓存
  //   都不会让它再冒出来；重新安装才会回到「第一次」。
  //   ffmpeg 缺失属于「本机环境提示」：首次在卡片里如实说明，之后仅保留设置区
  //   的一行小字 + 顶部「使用说明」可随时回看，不再每次开屏都弹卡片。
  // ------------------------------------------------------------------ //
  const GUIDE_SEEN_KEY = "media_dl_guide_seen";   // 本地缓存，仅用于开屏瞬间少闪一下

  function guideSeenLocal() {
    try { return localStorage.getItem(GUIDE_SEEN_KEY) === "1"; } catch (e) { return false; }
  }
  function markGuideSeenLocal() {
    try { localStorage.setItem(GUIDE_SEEN_KEY, "1"); } catch (e) { /* ignore */ }
  }

  /** 是否处于「从未引导过」状态：服务端状态为准，本地缓存仅作开屏兜底。 */
  function isFirstRun() {
    if (CFG && typeof CFG.first_run === "boolean") return CFG.first_run;
    return !guideSeenLocal();
  }

  function renderGuide() {
    const card = $("guide-card");
    if (!card) return;
    const needFfmpeg = CFG && CFG.ffmpeg === false;
    // 已经引导过 → 静默；缺 ffmpeg 也不再弹卡，改用设置区低调提示（renderFfmpegHint）
    if (!isFirstRun()) { card.classList.add("hidden"); return; }

    const rows = [];
    rows.push('<p>把链接粘进输入框（或直接点「粘贴」），勾选需要的选项，点「下载」即可。' +
      (NATIVE ? "文件会直接存进手机相册（DCIM/随手存）。" : "文件会由浏览器保存到本机。") + "</p>");
    rows.push('<div class="guide-row"><span class="guide-ico">🔒</span><span>' +
      '<strong>全程本机、不上传。</strong>解析与下载都在你这台设备上完成，' +
      '没有账号、没有服务器、没有云端同步；登录态 Cookie 只发往对应平台，' +
      (CFG && CFG.secure_store === "dpapi"
        ? "并以系统级加密（DPAPI，绑定当前电脑用户）保存在本机。"
        : "且只保存在应用私有目录内。") +
      "</span></div>");
    if (needFfmpeg) {
      rows.push('<div class="guide-row"><span class="guide-ico">⚠️</span><span>' +
        '<span class="warn-flag">未检测到 ffmpeg。</span>不影响绝大多数下载，但以下能力会受限：' +
        '<ul><li>高画质视频的「音视频分轨」可能无法自动合轨，个别站点会退回较低清晰度；</li>' +
        '<li>仅下载音频（mp3）等需要转码的功能不可用。</li></ul>' +
        "如需完整能力：PC 端安装 ffmpeg 并加入 PATH 后重启随手存即可。" +
        (NATIVE ? "（手机端为内嵌环境，通常无需也不便自行安装。）" : "") +
        "</span></div>");
    } else if (NATIVE) {
      rows.push('<p class="guide-note">提示：从抖音 / 相册 / 浏览器「分享」到随手存，可免去复制粘贴，一步带入链接。</p>');
    }
    $("guide-title").textContent = needFfmpeg ? "开始使用 · 环境提示" : "开始使用";
    $("guide-body").innerHTML = rows.join("");
    card.classList.remove("hidden");
  }

  /** 手动「使用说明」：复用首启卡片的渲染路径，随时可看，且不影响首启状态。 */
  function openGuideManually() {
    const card = $("guide-card");
    if (!card) return;
    const wasFirstRun = CFG ? CFG.first_run : undefined;
    if (CFG) CFG.first_run = true;                   // 借用同一渲染路径，后续还原
    renderGuide();
    if (CFG) CFG.first_run = wasFirstRun;
    card.classList.remove("hidden");                 // 兜底：确保手动打开一定可见
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeGuide() {
    markGuideSeenLocal();
    if (CFG) CFG.first_run = false;
    $("guide-card").classList.add("hidden");
    // 记到服务端持久目录：换版本 / 重启 / 清缓存都不会再自动弹出
    api("/api/first-run-done", { method: "POST", body: "{}" }).catch(() => { /* 忽略 */ });
  }

  /** 缺 ffmpeg 时的低调提示：只在设置区留一行小字，不再每次开屏弹卡。 */
  function renderFfmpegHint() {
    const el = $("ffmpeg-hint");
    if (!el) return;
    if (CFG && CFG.ffmpeg === false) {
      el.textContent = "⚠️ 未检测到 ffmpeg：高画质分轨可能不合轨、转码类功能不可用。"
        + (NATIVE ? "" : " 安装 ffmpeg 并加入 PATH 后重启即可。详情见「使用说明」。");
      el.classList.remove("hidden");
    } else {
      el.textContent = "";
      el.classList.add("hidden");
    }
  }

  // ------------------------------------------------------------------ //
  // 媒体库（已下载）：读取 /api/files，展示去向与体积，支持再次保存 / 打开文件夹
  //   - PC：打开所在文件夹 / 再次下载
  //   - 移动端：Java 原生写入相册，这里提示「在系统相册查看」，并可再次下载
  // ------------------------------------------------------------------ //
  let LIBRARY = [];

  function fmtTime(sec) {
    if (!sec) return "";
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function libIcon(kind) {
    return kind === "video" ? "🎬" : kind === "image" ? "🖼" : kind === "audio" ? "🎵" : "📄";
  }

  // ---- 媒体库真实缩略图（仅 PC）：图片直接用 /api/file，视频用 canvas 截首帧 ---- //
  const THUMB_DONE = new Set();   // 已生成过缩略图的文件路径，避免重复解码

  function thumbFor(f, i) {
    // 移动端文件在系统相册，本地路径不可读，保持图标形态
    if (NATIVE || !f.path || THUMB_DONE.has(f.path)) return "";
    if (f.kind === "image") {
      THUMB_DONE.add(f.path);
      return '<img class="lib-thumb" loading="lazy" alt="" src="' +
        apiUrl("/api/file?path=" + encodeURIComponent(f.path)) +
        '" onerror="this.remove()">';
    }
    if (f.kind === "video") return '<div class="lib-thumb lib-thumb-video" data-vi="' + i + '"></div>';
    return "";
  }

  /** 视频→首帧：requestVideoFrameCallback / seeked 双兜底，生成 96px 宽截图。 */
  function paintVideoThumb(holder, src) {
    const v = document.createElement("video");
    v.muted = true; v.preload = "metadata"; v.src = src;
    const paint = () => {
      try {
        const w = 96, ratio = v.videoHeight ? (v.videoHeight / v.videoWidth) : 0.5625;
        const c = document.createElement("canvas");
        c.width = w; c.height = Math.round(w * ratio);
        c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
        holder.innerHTML = "";
        holder.appendChild(c);
      } catch (e) { holder.innerHTML = ""; }   // 解码失败回退图标
    };
    v.requestVideoFrameCallback
      ? v.requestVideoFrameCallback(() => { paint(); v.src = ""; })
      : (v.onseeked = () => { paint(); v.src = ""; });
    v.onerror = () => { holder.innerHTML = ""; };
    // 拿到时长后 seek 到 10% 处触发解码（很多格式首帧是黑屏，取 10% 更有信息量）
    v.onloadedmetadata = () => { try { v.currentTime = Math.min(1, (v.duration || 1) * 0.1); } catch (e) { paint(); } };
  }

  function attachVideoThumbs(root) {
    if (NATIVE) return;
    Array.prototype.forEach.call((root || document).querySelectorAll(".lib-thumb-video"), (h) => {
      const f = LIBRARY[parseInt(h.dataset.vi, 10)];
      if (!f || !f.path) return;
      paintVideoThumb(h, apiUrl("/api/file?path=" + encodeURIComponent(f.path)));
    });
  }

  function renderLibrary() {
    const card = $("library-card");
    if (!card) return;
    if (!LIBRARY.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    // 列表有内容后，恢复上次的展开状态（默认收起）
    if (!card._stateInited) {
      card._stateInited = true;
      let open = false;
      try { open = localStorage.getItem(LIB_OPEN_KEY) === "1"; } catch (e) { open = false; }
      const body = $("library-body");
      body.classList.toggle("collapsed", !open);
      $("library-arrow").textContent = open ? "▾" : "▸";
      const head = $("library-head");
      if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
    }
    $("library-summary").textContent = LIBRARY.length + " 个文件";
    $("library-list").innerHTML = LIBRARY.map((f, i) => {
      const dest = NATIVE ? "已存到相册（DCIM/随手存）" : (f.group || "下载目录");
      // 移动端文件已在系统相册里，条目仅作回看；PC 端可从下载目录再次保存 / 定位
      const actions = NATIVE ? "" :
        '<div class="lib-actions">' +
          '<button class="btn btn-sm lib-reveal" data-i="' + i + '" aria-label="在文件夹中打开">打开文件夹</button>' +
          '<button class="btn btn-sm btn-primary lib-again" data-i="' + i + '" aria-label="再次保存该文件">再次保存</button>' +
        "</div>";
      return '<div class="lib-item" role="listitem">' +
        (thumbFor(f, i) || '<div class="lib-ico ' + escapeHtml(f.kind || "other") + '" aria-hidden="true">' + libIcon(f.kind) + "</div>") +
        '<div class="lib-info"><div class="lib-name">' + escapeHtml(f.name) + "</div>" +
          '<div class="lib-meta"><span>' + escapeHtml(dest) + "</span><span>" +
          fmtBytes(f.size) + "</span><span>" + fmtTime(f.time) + "</span></div></div>" +
        actions + "</div>";
    }).join("");

    Array.prototype.forEach.call(document.querySelectorAll(".lib-reveal"), (b) => {
      b.addEventListener("click", () => {
        const f = LIBRARY[parseInt(b.dataset.i, 10)];
        if (!f) return;
        api("/api/reveal", { method: "POST", body: JSON.stringify({ path: f.path }) })
          .then(() => toast("已在文件夹中定位"))
          .catch((e) => toast("打开文件夹失败：" + e.message));
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".lib-again"), (b) => {
      b.addEventListener("click", () => {
        const f = LIBRARY[parseInt(b.dataset.i, 10)];
        if (!f) return;
        const url = apiUrl("/api/file?path=" + encodeURIComponent(f.path));
        if (NATIVE) {
          const taskId = "l" + Date.now() + "_" + parseInt(b.dataset.i, 10);
          enqueueNative(url, f.name, taskId);
        } else {
          const a = document.createElement("a");
          a.href = apiUrl("/api/download?path=" + encodeURIComponent(f.path));
          a.rel = "noopener"; a.style.display = "none";
          document.body.appendChild(a); a.click();
          setTimeout(() => { if (a.parentNode) document.body.removeChild(a); }, 1500);
        }
      });
    });
    attachVideoThumbs($("library-list"));   // PC：视频条目补首帧缩略图
  }

  async function refreshLibrary(showToast) {
    // 移动端：文件由 Java 原生写入相册，不在 Python 下载目录里，改从原生列举。
    if (NATIVE && BRIDGE && typeof BRIDGE.listDownloads === "function") {
      try {
        const raw = BRIDGE.listDownloads();
        LIBRARY = JSON.parse(raw || "[]");
        LIBRARY.forEach((f) => { f.path = ""; f.group = ""; });
        renderLibrary();
        if (showToast) toast(LIBRARY.length ? ("已加载 " + LIBRARY.length + " 个已保存文件") : "还没有已保存的文件");
      } catch (e) {
        if (showToast) toast("读取失败");
      }
      return;
    }
    try {
      const data = await api("/api/files");
      LIBRARY = data.files || [];
      renderLibrary();
      if (showToast) toast(LIBRARY.length ? ("已加载 " + LIBRARY.length + " 个已下载文件") : "还没有已下载的文件");
    } catch (e) {
      if (showToast) toast("读取失败：" + e.message);
    }
  }

  const LIB_OPEN_KEY = "media_dl_library_open";

  function libraryOpen() {
    const body = $("library-body");
    return !!(body && !body.classList.contains("collapsed"));
  }

  /** 展开 / 收起媒体库主体（默认收起，减少首屏占用）。 */
  function setLibraryOpen(open, opts) {
    const body = $("library-body");
    if (!body) return;
    body.classList.toggle("collapsed", !open);
    $("library-arrow").textContent = open ? "▾" : "▸";
    const head = $("library-head");
    if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
    try { localStorage.setItem(LIB_OPEN_KEY, open ? "1" : "0"); } catch (e) { /* 忽略 */ }
    if (open && !(opts && opts.silent)) {
      refreshLibrary(false).then(() => {
        if (LIBRARY.length) {
          try { $("library-card").scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { /* 忽略 */ }
        }
      });
      return;
    }
    if (open) refreshLibrary(false);
  }

  function toggleLibraryOpen() {
    setLibraryOpen(!libraryOpen());
  }

  // 工具栏「已下载」：打开并展开媒体库（若已展开则收起）
  function toggleLibrary() {
    const card = $("library-card");
    if (libraryOpen()) { setLibraryOpen(false); return; }
    card.classList.remove("hidden");
    setLibraryOpen(true);
  }

  async function boot() {
    try {
      CFG = await api("/api/config");
      $("platforms").innerHTML = (CFG.platforms || []).map((p) =>
        '<span class="platform-chip">' + escapeHtml(p) + "</span>").join("");
      applyClientUi();
      updateCount();
      runDetect();
      renderGuide();
      renderFfmpegHint();
      setStatus(NATIVE ? "已就绪 · 粘贴链接后点下方按钮，文件直接保存到手机本地"
                       : "已就绪 · 粘贴链接后点下方按钮");
    } catch (e) {
      applyClientUi();          // 先按端裁剪界面，再落错误状态（否则会被顶栏文案覆盖）
      $("state-pill").textContent = "服务未启动";
      $("state-pill").className = "pill warn";
      setStatus("连不上本机服务：" + e.message);
      toast("无法连接本机服务：" + e.message);
    }    // APP 内登录获取的抖音登录态：自动填充并提示，避免手动复制；首次还会自动静默探测
    if (IS_APK) {
      refreshCookieStatus().then(() => maybeAutoOpenLogin());
      // 拉取冷启动时 Java 端攒下的分享链接（push 通道的兜底）
      if (BRIDGE && typeof BRIDGE.takeSharedText === "function") {
        try {
          const shared = BRIDGE.takeSharedText();
          if (shared && shared.trim()) ingestShared(shared.trim());
        } catch (e) { /* 忽略 */ }
      }
    }
  }

  async function refreshCookieStatus() {
    try {
      const r = await api("/api/cookie");
      const box = $("opt-cookie");
      const status = $("cookie-status");
      if (r.has_cookie) {
        if (box && !box.value) box.value = r.cookie;
        if (status) status.textContent = "✓ 已自动获取移动端抖音登录态，解析抖音时自动使用（有效期由抖音决定，无需手动操作）。";
      } else if (status) {
        status.textContent = "系统内不提供登录。若手机上的移动端抖音已登录，点上方按钮即可自动读取登录态（无需输入账号密码）。";
      }
      return !!r.has_cookie;
    } catch (e) { return false; }
  }

  // APP 端：启动后先静默探测移动端抖音登录态（不弹界面、不打扰）；
  // 只有确实读不到、且用户从未手动同步过时，才提示可点按钮读取。
  // 系统内不做任何登录流程。
  async function maybeAutoOpenLogin() {
    if (!IS_APK || !BRIDGE) return;
    const has = await refreshCookieStatus();
    if (has) return;
    // 静默探测：后台加载移动端抖音页面读取已有 Cookie（不显示界面）
    if (typeof BRIDGE.probeDouyinCookie === "function") {
      try { BRIDGE.probeDouyinCookie(); } catch (e) { /* 忽略 */ }
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
    initClipboardWatch();
  }

  // ------------------------------------------------------------------ //
  // 剪贴板自动检测：回到页面时读一次剪贴板，含新链接才浮出「一键填入」条。
  // 不自动下载、不打断输入；同一条链接只提示一次（与上次已填内容比对）。
  // ------------------------------------------------------------------ //
  let lastClipText = "";        // 上次检测到的剪贴板原文（避免重复提示同一条）
  let clipDismissed = "";       // 用户点「✕」忽略的文本，本次会话不再提示

  function initClipboardWatch() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkClipboardForLink();
    });
    window.addEventListener("focus", () => checkClipboardForLink());
  }

  async function checkClipboardForLink() {
    // 输入框已有同样内容 → 无需提示
    const box = $("input");
    if (!box || document.activeElement === box) return;
    let text = "";
    try {
      if (BRIDGE && typeof BRIDGE.readClipboard === "function") {
        text = BRIDGE.readClipboard() || "";
      } else if (navigator.clipboard && window.isSecureContext) {
        text = await navigator.clipboard.readText();
      }
    } catch (e) { return; }     // 无权限/无内容：静默跳过
    text = (text || "").trim();
    if (!text || text === lastClipText || text === clipDismissed) return;
    // 只对"含链接"的剪贴板提示；与输入框现值相同也不提示
    if (!/https?:\/\/[^\s<>"']+/i.test(text)) return;
    if (box.value.trim() === text) { lastClipText = text; return; }
    lastClipText = text;
    $("clip-bar").classList.remove("hidden");
  }

  function dismissClipBar() {
    clipDismissed = lastClipText;
    $("clip-bar").classList.add("hidden");
  }

  $("input").addEventListener("input", () => {
    updateCount();
    clearTimeout(detectTimer);
    detectTimer = setTimeout(runDetect, 300);
  });
  $("btn-download").addEventListener("click", () => parseAndDownload(true));
  $("btn-parse").addEventListener("click", () => parseAndDownload(false));
  if ($("btn-theme")) $("btn-theme").addEventListener("click", toggleTheme);
  initNotifyToggle();
  $("btn-download-all").addEventListener("click", downloadAll);
  $("btn-clear-jobs").addEventListener("click", clearFinishedTasks);
  $("btn-paste").addEventListener("click", pasteFromClipboard);
  if ($("btn-clip-fill")) {
    $("btn-clip-fill").addEventListener("click", () => {
      $("clip-bar").classList.add("hidden");
      insertPasted(lastClipText);
    });
  }
  if ($("btn-clip-dismiss")) $("btn-clip-dismiss").addEventListener("click", dismissClipBar);
  if ($("btn-library")) $("btn-library").addEventListener("click", toggleLibrary);
  if ($("library-head")) {
    $("library-head").addEventListener("click", toggleLibraryOpen);
    $("library-head").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleLibraryOpen(); }
    });
  }
  if ($("btn-refresh-library")) $("btn-refresh-library").addEventListener("click", () => refreshLibrary(true));
  if ($("btn-guide-close")) $("btn-guide-close").addEventListener("click", closeGuide);
  if ($("btn-help")) $("btn-help").addEventListener("click", openGuideManually);
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
  $("btn-dy-login").addEventListener("click", () => {
    if (BRIDGE && typeof BRIDGE.openDouyinLogin === "function") {
      try {
        BRIDGE.openDouyinLogin();
        toast("正在读取移动端抖音登录态（若手机抖音已登录会自动获取）");
      } catch (e) { toast("读取失败：" + e.message); }
    } else {
      toast("当前环境不支持自动读取，可手动粘贴 Cookie");
    }
  });
  $("btn-preview-download").addEventListener("click", () => {
    if (PREVIEW_ITEM) triggerDownload(PREVIEW_ITEM);
  });
  $("settings-head").addEventListener("click", () => {
    const el = $("settings");
    el.classList.toggle("collapsed");
    const closed = el.classList.contains("collapsed");
    $("settings-arrow").textContent = closed ? "▸" : "▾";
    $("settings-head").setAttribute("aria-expanded", closed ? "false" : "true");
  });
  $("settings-head").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("settings-head").click(); }
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
