/* 守望 Sentinel 面板逻辑 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PARAMS = new URLSearchParams(location.search);
  const TOKEN = PARAMS.get("token") || localStorage.getItem("sentinel-token") || "";
  if (TOKEN) localStorage.setItem("sentinel-token", TOKEN);

  let state = null;
  let cfg = null;
  let dragging = false;
  let deviceSig = "";
  let eventSig = "";
  let previewTimer = null;
  let saveTimer = null;
  let pending = {};

  // ---------- 主题 ----------
  function applyTheme(mode) {
    document.documentElement.dataset.theme = mode;
    $("themeBtn").firstElementChild.firstElementChild.setAttribute(
      "href", mode === "dark" ? "#i-sun" : "#i-moon"
    );
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = mode === "dark" ? "#141218" : "#FEF7FF";
    localStorage.setItem("sentinel-theme", mode);
  }
  const initialTheme = PARAMS.get("theme")
    || localStorage.getItem("sentinel-theme")
    || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  applyTheme(initialTheme);
  $("themeBtn").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });

  // ---------- 网络 ----------
  async function api(path, options = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    if (TOKEN) headers["X-Sentinel-Token"] = TOKEN;
    const resp = await fetch(path, Object.assign({}, options, { headers, cache: "no-store" }));
    if (resp.status === 401) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
      throw new Error("unauthorized");
    }
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
    if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
    return data;
  }

  let snackTimer = null;
  function toast(msg, isError = false) {
    const el = $("snackbar");
    el.textContent = msg;
    el.classList.toggle("error", !!isError);
    el.classList.add("show");
    clearTimeout(snackTimer);
    snackTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  // ---------- 配置绑定 ----------
  const FIELDS = {
    btTarget: ["bt", "target_name", "text"],
    btEnabled: ["bt", "enabled", "bool"],
    btRequire: ["bt", "require_disconnected", "bool"],
    presenceEnabled: ["presence", "enabled", "bool"],
    camIndex: ["camera", "index", "int"],
    camBackend: ["camera", "backend", "text"],
    camFps: ["camera", "fps", "int"],
    camIdle: ["camera", "idle_release_seconds", "int"],
    apiPort: ["api", "port", "int"],
    apiToken: ["api", "token", "text"],
    tgChat: ["notify", "chat_id", "text"],
    tgProxy: ["notify", "proxy", "text"],
    tgPhoto: ["notify", "send_photo", "bool"],
    tgText: ["notify", "send_text_for_change", "bool"],
    faceEnabled: ["detect", "face_enabled", "bool"],
    bodyEnabled: ["detect", "body_enabled", "bool"],
    burstEnabled: ["burst", "enabled", "bool"],
  };

  const SLIDERS = [
    { id: "sCompare", label: "vCompare", path: ["detect", "compare_seconds"], scale: 1, fmt: (v) => `${Number(v).toFixed(1)} s` },
    { id: "sChange", label: "vChange", path: ["detect", "change_ratio"], scale: 100, fmt: (v) => `${(v * 100).toFixed(1)} %` },
    { id: "sPixel", label: "vPixel", path: ["detect", "pixel_diff"], scale: 1, fmt: (v) => `${Math.round(v)}` },
    { id: "sSpeed", label: "vSpeed", path: ["detect", "motion_speed"], scale: 1, fmt: (v) => `${Math.round(v)} %/s` },
    { id: "sCooldown", label: "vCooldown", path: ["detect", "cooldown_seconds"], scale: 1, fmt: (v) => `${Math.round(v)} s` },
    { id: "sFaceScore", label: "vFaceScore", path: ["detect", "face_score"], scale: 1, fmt: (v) => Number(v).toFixed(2) },
    { id: "sFaceSize", label: "vFaceSize", path: ["detect", "face_min_size"], scale: 1, fmt: (v) => `${Math.round(v)}` },
    { id: "sBodyRatio", label: "vBodyRatio", path: ["detect", "body_ratio"], scale: 100, fmt: (v) => `${Math.round(v * 100)}` },
    { id: "sBurstInterval", label: "vBurstInterval", path: ["burst", "interval_ms"], scale: 1, fmt: (v) => `${Math.round(v)} ms` },
    { id: "sBurstCount", label: "vBurstCount", path: ["burst", "count"], scale: 1, fmt: (v) => `${Math.round(v)} 张` },
    { id: "sBurstKeep", label: "vBurstKeep", path: ["burst", "keep"], scale: 1, fmt: (v) => `${Math.round(v)} 张` },
  ];

  function getPath(obj, path) {
    return path.reduce((acc, key) => (acc == null ? acc : acc[key]), obj);
  }
  function setPath(obj, path, value) {
    let cur = obj;
    for (let i = 0; i < path.length - 1; i++) {
      if (typeof cur[path[i]] !== "object" || cur[path[i]] === null) cur[path[i]] = {};
      cur = cur[path[i]];
    }
    cur[path[path.length - 1]] = value;
  }

  function paintSlider(el, value, scale) {
    const min = Number(el.min);
    const max = Number(el.max);
    const raw = value * scale;
    const pct = ((raw - min) / (max - min)) * 100;
    el.style.setProperty("--fill", `${Math.max(0, Math.min(100, pct))}%`);
  }

  function applyConfig(c) {
    cfg = c;
    for (const [id, [section, key, type]] of Object.entries(FIELDS)) {
      const el = $(id);
      if (!el) continue;
      if (type === "bool") el.checked = !!c[section][key];
      else if (document.activeElement !== el) el.value = c[section][key];
    }
    for (const s of SLIDERS) {
      const el = $(s.id);
      const val = getPath(c, s.path);
      if (dragging && document.activeElement === el) continue;
      el.value = val * s.scale;
      paintSlider(el, val, s.scale);
      $(s.label).textContent = s.fmt(val);
    }
    $("faceSliders").style.display = c.detect.face_enabled ? "flex" : "none";
    $("bodySliders").style.display = c.detect.body_enabled ? "flex" : "none";
    $("panelUrl").textContent = location.origin;
    if (c.api.port !== Number(location.port) && location.hostname !== "127.0.0.1") {
      /* 端口变更提示由保存 toast 处理 */
    }
  }

  function queueSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, 380);
  }

  async function flushSave() {
    if (!Object.keys(pending).length) return;
    const patch = pending;
    pending = {};
    try {
      const res = await api("/api/config", { method: "PATCH", body: JSON.stringify(patch) });
      cfg = res.config;
      toast("已保存");
    } catch (err) {
      toast(`保存失败：${err.message}`, true);
    }
  }

  function saveField(section, key, value) {
    if (!pending[section]) pending[section] = {};
    pending[section][key] = value;
    queueSave();
  }

  for (const [id, [section, key, type]] of Object.entries(FIELDS)) {
    const el = $(id);
    if (!el) continue;
    const evt = type === "bool" ? "change" : "input";
    el.addEventListener(evt, () => {
      let value;
      if (type === "bool") value = el.checked;
      else if (type === "int") value = parseInt(el.value || "0", 10);
      else value = el.value;
      if (type !== "bool" && el.value === "") return;
      if (id === "apiPort" || id === "apiToken") {
        toast("端口 / Token 修改后需重启服务生效");
      }
      saveField(section, key, value);
    });
  }

  for (const s of SLIDERS) {
    const el = $(s.id);
    el.addEventListener("pointerdown", () => { dragging = true; });
    el.addEventListener("pointerup", () => { dragging = false; });
    el.addEventListener("blur", () => { dragging = false; });
    el.addEventListener("input", () => {
      const val = Number(el.value) / s.scale;
      paintSlider(el, val, s.scale);
      $(s.label).textContent = s.fmt(val);
      saveField(s.path[0], s.path[1], val);
    });
  }

  // ---------- 状态渲染 ----------
  function setChip(text, tone) {
    const chip = $("gateChip");
    chip.className = `chip ${tone}`;
    $("gateText").textContent = text;
  }

  function renderState(s) {
    state = s;
    if (!cfg) applyConfig(s.config);

    const gate = s.gate || {};
    if (gate.active) setChip("哨兵生效", "ok");
    else if (s.armed) setChip("待机中", "warn");
    else setChip("看守未启用", "idle");
    $("armSwitch").checked = !!s.armed;

    const bt = s.bt || {};
    const matched = bt.matched;
    $("statPhone").textContent = matched ? matched.name.slice(0, 12) : "未找到";
    $("statPhone").style.color = matched && matched.connected ? "var(--success)" : "";
    $("statPhoneHint").textContent = matched
      ? (matched.connected ? "已与电脑连接" : "已配对 · 未连接")
      : (bt.error ? "蓝牙不可用" : "不在配对列表");

    const cam = s.camera || {};
    $("statCam").textContent = cam.open ? "在线" : "待机";
    $("statCamHint").textContent = cam.open
      ? `${cam.width}×${cam.height} · ${cam.fps} fps`
      : (cam.error || "空闲自动释放");

    $("statMode").textContent = s.armed ? (gate.active ? "生效" : "待机") : "关闭";
    $("statMode").textContent = s.armed ? (gate.active ? "生效" : "待机") : "关闭";
    $("statModeHint").textContent = gate.reason || "—";

    $("statAlerts").textContent = s.alerts ?? 0;
    const last = (s.events || []).find((e) => e.kind === "alert");
    $("statAlertsHint").textContent = last ? `${last.date} ${last.time}` : "暂无";

    const m = s.metrics;
    $("mChange").textContent = m ? `${(m.change_ratio * 100).toFixed(1)}%` : "0.0%";
    $("mSpeed").textContent = m ? `${m.speed.toFixed(1)}%/s` : "0.0%/s";
    $("mFaces").textContent = m ? m.face_count : 0;
    $("mMs").textContent = m ? `${m.ms.toFixed(0)} ms` : "—";
    $("mChange").style.color = m && m.change_ratio >= s.config.detect.change_ratio ? "var(--error)" : "";
    $("mSpeed").style.color = m && m.speed >= s.config.detect.motion_speed ? "var(--warning)" : "";

    $("btHint").textContent = bt.error
      ? `蓝牙读取失败：${bt.error}`
      : `配对 ${(bt.devices || []).length} 台 · 检查于 ${bt.checked_at ? new Date(bt.checked_at * 1000).toLocaleTimeString() : "—"}`;

    renderPresence(s);

    renderDevices(bt);
    renderEvents(s.events || []);
    renderTg(s);
    $("frameBadge").textContent = cam.open ? "LIVE" : "IDLE";
    $("frameBadge").classList.toggle("live", !!cam.open);
    $("logoutBtn").hidden = !(s.auth && s.auth.enabled);
    if (s.auth && s.auth.session_days) $("authDays").textContent = s.auth.session_days;
  }

  function renderPresence(s) {
    const p = s.presence || {};
    const pCfg = (s.config && s.config.presence) || {};
    const row = $("presenceRow");
    const tol = Number(pCfg.brightness_tolerance ?? 0);
    const br = p.brightness;
    const lo = p.brightness_min;
    let text;
    if (br != null) {
      text = `屏幕 ${p.brightness_pct ?? br}%（最低 ${lo ?? 0}%） · ${p.locked ? "已锁屏" : "未锁屏"}`;
    } else {
      text = `屏幕亮度读不到${p.error ? "：" + String(p.error).slice(0, 36) : ""} · ${p.locked ? "已锁屏" : "未锁屏"}`;
    }
    if (!pCfg.enabled) {
      row.className = "presence-row";
      text = "电脑在用检测已关闭";
    } else {
      const allowed = !!p.locked || (br != null && Number(br) <= Number(lo ?? 0) + tol);
      row.className = "presence-row " + (allowed ? "allow" : "suppress");
      text += allowed ? " · 允许报警" : " · 抑制报警";
    }
    $("presenceText").textContent = text;
  }

  function renderDevices(bt) {
    const target = cfg && cfg.bt.target_name;
    const sig = JSON.stringify([(bt.devices || []).map((d) => [d.addr, d.connected]), target]);
    if (sig === deviceSig) {
      document.querySelectorAll(".device").forEach((el) => {
        el.classList.toggle("active", el.dataset.name === target);
      });
      return;
    }
    deviceSig = sig;
    const box = $("deviceList");
    box.innerHTML = "";
    if (!(bt.devices || []).length) {
      box.innerHTML = '<p class="support dim" style="padding:8px 2px">没有读到已配对设备</p>';
      return;
    }
    const list = [...bt.devices].sort((a, b) => {
      const ta = a.name === target ? 0 : 1;
      const tb = b.name === target ? 0 : 1;
      if (ta !== tb) return ta - tb;
      if (a.connected !== b.connected) return a.connected ? -1 : 1;
      return (a.name || "").localeCompare(b.name || "");
    });
    for (const d of list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "device" + (d.connected ? " on" : "") + (d.name === target ? " active" : "");
      btn.dataset.name = d.name;
      btn.title = `点击设为监控目标：${d.name}`;
      btn.innerHTML = `<span class="dot"></span>
        <span class="name"></span>
        <span class="addr"></span>
        <span class="tag">${d.connected ? "已连接" : d.paired ? "已配对" : "未配对"}</span>`;
      btn.querySelector(".name").textContent = d.name || "(无名)";
      btn.querySelector(".addr").textContent = d.addr;
      btn.addEventListener("click", () => {
        $("btTarget").value = d.name;
        saveField("bt", "target_name", d.name);
        document.querySelectorAll(".device").forEach((el) => el.classList.remove("active"));
        btn.classList.add("active");
        toast(`监控目标已设为「${d.name}」`);
      });
      box.appendChild(btn);
    }
  }

  function renderEvents(events) {
    const sig = events.map((e) => e.id).join("|");
    if (sig === eventSig) return;
    eventSig = sig;
    const box = $("events");
    box.innerHTML = "";
    if (!events.length) {
      box.innerHTML = '<p class="empty">暂无事件</p>';
      return;
    }
    for (const e of events) {
      const row = document.createElement("div");
      row.className = `event ${e.kind}`;
      const detail = [e.detail, e.metrics && e.metrics.ms ? `${e.metrics.ms}ms` : ""].filter(Boolean).join(" · ");
      row.innerHTML = `
        <span class="event-time">${e.time}</span>
        <div class="event-main"><div class="event-title"></div><div class="event-detail"></div></div>
        ${e.snapshot ? `<img class="event-thumb" loading="lazy" src="/api/snapshots/${encodeURIComponent(e.snapshot)}" alt="">` : "<span></span>"}`;
      row.querySelector(".event-title").textContent = e.title;
      row.querySelector(".event-detail").textContent = detail || "—";
      box.appendChild(row);
    }
  }

  function renderTg(s) {
    const tg = s.tg || {};
    const el = $("tgStatus");
    if (!tg.last_at) {
      el.textContent = "尚未发送";
      return;
    }
    const t = new Date(tg.last_at * 1000).toLocaleTimeString();
    el.textContent = tg.last_ok ? `最近发送成功 ${t}` : `最近发送失败 ${t}：${(tg.last_error || "").slice(0, 80)}`;
    el.style.color = tg.last_ok ? "var(--success)" : "var(--error)";
  }

  // ---------- 轮询 ----------
  async function poll() {
    try {
      const s = await api("/api/state");
      renderState(s);
      if (cfg && s.config) {
        // 保持本地与服务器一致（配置可能被其它客户端修改）
        const changed = JSON.stringify(s.config) !== JSON.stringify(cfg);
        if (changed && !Object.keys(pending).length && !dragging) applyConfig(s.config);
      }
    } catch (err) {
      setChip("连接断开", "warn");
    }
  }
  poll();
  setInterval(() => { if (!document.hidden) poll(); }, 1300);

  // ---------- 预览 ----------
  function startPreview() {
    stopPreview();
    const img = $("preview");
    img.hidden = false;
    $("frameEmpty").hidden = true;
    const tick = () => {
      const url = `/api/snapshot.jpg?t=${Date.now()}` + (TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : "");
      const next = new Image();
      next.onload = () => {
        img.src = next.src;
        img.hidden = false;
        $("frameEmpty").hidden = true;
      };
      next.onerror = () => {
        img.hidden = true;
        $("frameEmpty").hidden = false;
        $("frameEmpty").querySelector("p").textContent = "摄像头暂时不可用，稍后自动重试…";
      };
      next.src = url;
    };
    tick();
    previewTimer = setInterval(tick, 850);
  }
  function stopPreview() {
    if (previewTimer) clearInterval(previewTimer);
    previewTimer = null;
    $("preview").hidden = true;
    $("frameEmpty").hidden = false;
    $("frameEmpty").querySelector("p").textContent = "打开右侧「实时画面」开关即可预览";
  }
  $("previewSwitch").addEventListener("change", (e) => {
    if (e.target.checked) startPreview(); else stopPreview();
  });

  if (PARAMS.get("preview") === "1") {
    $("previewSwitch").checked = true;
    startPreview();
  }

  // ---------- 操作 ----------
  $("armSwitch").addEventListener("change", async (e) => {
    const armed = e.target.checked;
    try {
      await api("/api/arm", { method: "POST", body: JSON.stringify({ armed }) });
      toast(armed ? "哨兵已启用" : "哨兵已停用");
      poll();
    } catch (err) {
      toast(`操作失败：${err.message}`, true);
      e.target.checked = !armed;
    }
  });

  $("btRefresh").addEventListener("click", async () => {
    const btn = $("btRefresh");
    btn.style.transform = "rotate(180deg)";
    try {
      const res = await api("/api/bt/refresh", { method: "POST" });
      toast(`蓝牙已刷新，配对 ${res.bt.devices.length} 台`);
      deviceSig = "";
      poll();
    } catch (err) {
      toast(`刷新失败：${err.message}`, true);
    } finally {
      setTimeout(() => { btn.style.transform = ""; }, 300);
    }
  });

  $("tgTest").addEventListener("click", async () => {
    const btn = $("tgTest");
    btn.classList.add("busy");
    try {
      const res = await api("/api/telegram/test", { method: "POST" });
      toast(res.ok ? "已发送测试消息，去 Telegram 看看" : `发送失败：${res.detail}`, !res.ok);
    } catch (err) {
      toast(`发送失败：${err.message}`, true);
    } finally {
      btn.classList.remove("busy");
      poll();
    }
  });

  $("drillBtn").addEventListener("click", async () => {
    const btn = $("drillBtn");
    btn.classList.add("busy");
    btn.textContent = "演练中…";
    toast("演练 10 秒：现在在画面里动一动");
    try {
      const res = await api("/api/drill", { method: "POST", body: JSON.stringify({ seconds: 10 }) });
      if (!res.ok) {
        toast(`演练失败：${res.error}`, true);
      } else {
        const hit = res.would_trigger.change || res.would_trigger.human;
        toast(`${hit ? "会触发" : "不会触发"} · ${res.detail}`, !hit);
      }
    } catch (err) {
      toast(`演练失败：${err.message}`, true);
    } finally {
      btn.classList.remove("busy");
      btn.textContent = "演练 10s";
      setTimeout(poll, 800);
    }
  });

  $("tgSimulate").addEventListener("click", async () => {    const btn = $("tgSimulate");
    btn.classList.add("busy");
    try {
      const res = await api("/api/telegram/simulate", { method: "POST", body: JSON.stringify({ reason: "human" }) });
      toast(res.ok ? "模拟告警已发出（带截图）" : `模拟失败：${res.detail}`, !res.ok);
    } catch (err) {
      toast(`模拟失败：${err.message}`, true);
    } finally {
      btn.classList.remove("busy");
      setTimeout(poll, 1500);
    }
  });

  $("camProbe").addEventListener("click", async () => {
    const idx = parseInt($("camIndex").value || "0", 10);
    toast(`正在探测摄像头 ${idx}…`);
    try {
      const res = await api("/api/camera/probe", { method: "POST", body: JSON.stringify({ index: idx }) });
      toast(res.ok ? `摄像头 ${idx} 可用：${res.backend.toUpperCase()} ${res.width}×${res.height}` : `摄像头 ${idx} 不可用`, !res.ok);
    } catch (err) {
      toast(`探测失败：${err.message}`, true);
    }
  });

  $("logoutBtn").addEventListener("click", async () => {
    try {
      await api("/api/auth/logout", { method: "POST", body: "{}" });
    } catch (_) { /* 忽略 */ }
    location.replace("/login");
  });

  $("clearEvents").addEventListener("click", async () => {    if (!confirm("确定清空事件记录？截图文件不会被删除。")) return;
    try {
      await api("/api/events/clear", { method: "POST" });
      eventSig = "";
      toast("事件已清空");
      poll();
    } catch (err) {
      toast(`清空失败：${err.message}`, true);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && $("previewSwitch").checked) stopPreview();
    else if (!document.hidden && $("previewSwitch").checked) startPreview();
  });
  if (PARAMS.get("debug") === "1") {
    setTimeout(() => {
      const wide = [...document.querySelectorAll("body *")]
        .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 8)
        .map((el) => `${el.tagName}.${el.className}:${Math.round(el.getBoundingClientRect().right)}`);
      document.title = `vw=${window.innerWidth} sw=${document.documentElement.scrollWidth} ${wide.join(" | ")}`;
    }, 1500);
  }
})();
