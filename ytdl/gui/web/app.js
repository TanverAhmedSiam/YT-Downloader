/* ==========================================================================
   ytdl studio — paste → inspect → choose → keep
   ========================================================================== */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const PREF_KEY = "ytdl-studio-prefs-v2";
  const HINT = "Playlists and channels open in a picker, so you choose exactly what downloads.";
  const BITRATES = [128, 192, 256, 320];
  let prefTimer = 0;

  const state = {
    ready: false,
    app: { version: "" },
    catalog: { browsers: [] },
    defaults: {},

    directory: "",
    browser: "",
    cookies: "",
    proxy: "",
    concurrent: 3,
    retries: 10,

    inspected: null,
    inspectedUrl: "",
    qualities: [],
    hasVideo: true,
    hasAudio: true,
    playlist: null,
    playlistError: "",
    selected: new Set(),
    expanded: new Map(),
    pickerFilter: "",
    pickerSort: "default",
    activeSection: "",
    format: "mp4",
    videoQuality: "",
    audioBitrate: 320,

    jobs: [],
    eventSource: null,
    inspectToken: 0,
    queueTimer: 0,
  };

  /* ------------------------------------------------------------------ */
  /* Utilities                                                          */
  /* ------------------------------------------------------------------ */
  function formatBytes(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    let size = number;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    const text = index === 0 || size >= 100 ? Math.round(size) : size.toFixed(1);
    return `${text} ${units[index]}`;
  }

  function formatSpeed(value) {
    const text = formatBytes(value);
    return text ? `${text}/s` : "";
  }

  function formatClock(value) {
    const total = Math.round(Number(value));
    if (!Number.isFinite(total) || total <= 0) return "";
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    const pad = (part) => String(part).padStart(2, "0");
    return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
  }

  function shortPath(path, max = 46) {
    const value = String(path || "");
    if (value.length <= max) return value;
    const separator = value.includes("\\") ? "\\" : "/";
    const parts = value.split(/[\\/]+/).filter(Boolean);
    let tail = parts.pop() || "";
    while (parts.length) {
      const next = `${parts[parts.length - 1]}${separator}${tail}`;
      if (next.length > max - 2) break;
      tail = next;
      parts.pop();
    }
    return `…${separator}${tail}`;
  }

  function clamp(value, low, high) {
    const number = Number(value);
    if (!Number.isFinite(number)) return low;
    return Math.min(high, Math.max(low, number));
  }

  function capitalize(value) {
    const text = String(value || "");
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
  }

  function isTyping() {
    const element = document.activeElement;
    if (!element) return false;
    return (
      ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName) ||
      element.isContentEditable
    );
  }

  function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), wait);
    };
  }

  /* ------------------------------------------------------------------ */
  /* Toasts                                                             */
  /* ------------------------------------------------------------------ */
  function toast(message, level = "info", detail = "") {
    if (!message) return;
    const host = $("#toasts");
    const template = $("#toastTemplate");
    if (!host || !template) return;

    const node = template.content.firstElementChild.cloneNode(true);
    if (level === "success") node.classList.add("is-success");
    else if (level === "error") node.classList.add("is-error");

    $(".toast-msg", node).textContent = String(message);
    if (detail) {
      const detailNode = $(".toast-detail", node);
      detailNode.textContent = String(detail);
      detailNode.hidden = false;
    }

    let closed = false;
    const dismiss = () => {
      if (closed) return;
      closed = true;
      node.classList.add("is-leaving");
      window.setTimeout(() => node.remove(), 210);
    };
    $(".toast-x", node).addEventListener("click", dismiss);
    host.appendChild(node);
    while (host.children.length > 3) host.firstElementChild.remove();
    window.setTimeout(dismiss, level === "error" ? 8500 : 4600);
  }

  /* ------------------------------------------------------------------ */
  /* API                                                                */
  /* ------------------------------------------------------------------ */
  async function api(path, { method = "GET", body = null, timeout = 0 } = {}) {
    const options = {
      method,
      credentials: "same-origin",
      headers: { "X-Ytdl-Request": "studio" },
    };
    if (body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let timer = null;
    if (timeout) {
      const controller = new AbortController();
      options.signal = controller.signal;
      timer = window.setTimeout(() => controller.abort(), timeout);
    }
    try {
      const response = await fetch(path, options);
      let payload = null;
      try {
        payload = await response.json();
      } catch (error) {
        payload = null;
      }
      if (!response.ok) {
        const failure = new Error((payload && payload.error) || `Request failed (${response.status})`);
        failure.hint = payload && payload.hint;
        throw failure;
      }
      return payload || {};
    } finally {
      if (timer) window.clearTimeout(timer);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Preferences                                                        */
  /* ------------------------------------------------------------------ */
  function readPrefs() {
    try {
      return JSON.parse(window.localStorage.getItem(PREF_KEY) || "{}") || {};
    } catch (error) {
      return {};
    }
  }

  function prefsPayload() {
    return {
      directory: state.directory,
      browser: state.browser,
      cookies: state.cookies,
      proxy: state.proxy,
      concurrent: state.concurrent,
      retries: state.retries,
      audioBitrate: state.audioBitrate,
    };
  }

  function savePrefs() {
    const prefs = prefsPayload();
    try {
      window.localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch (error) {
      /* private mode — browser storage is unavailable */
    }
    window.clearTimeout(prefTimer);
    prefTimer = window.setTimeout(() => {
      prefTimer = 0;
      api("/api/prefs", { method: "POST", body: prefs, timeout: 8000 }).catch(() => {
        /* the local cache still has the values */
      });
    }, 250);
  }

  function flushPrefs() {
    if (!prefTimer) return;
    window.clearTimeout(prefTimer);
    prefTimer = 0;
    try {
      fetch("/api/prefs", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: {
          "X-Ytdl-Request": "studio",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(prefsPayload()),
      });
    } catch (error) {
      /* closing anyway */
    }
  }

  /* ------------------------------------------------------------------ */
  /* Bootstrap                                                          */
  /* ------------------------------------------------------------------ */
  async function boot() {
    bindEvents();
    connect();

    try {
      const payload = await api("/api/bootstrap", { timeout: 20000 });
      state.app = payload.app || state.app;
      state.catalog = payload.catalog || state.catalog;
      state.defaults = payload.defaults || {};

      const remote =
        payload.prefs && typeof payload.prefs === "object" ? payload.prefs : {};
      const prefs = { ...readPrefs(), ...remote };
      const browsers = state.catalog.browsers || [];
      const savedBrowser = String(prefs.browser || "").trim();
      const fallbackBrowser = browsers.includes(state.defaults.cookies_from_browser)
        ? state.defaults.cookies_from_browser
        : "auto";
      state.directory = prefs.directory || state.defaults.directory || "";
      state.browser = browsers.includes(savedBrowser) ? savedBrowser : fallbackBrowser;
      state.cookies = "cookies" in prefs ? prefs.cookies : state.defaults.cookies || "";
      state.proxy = "proxy" in prefs ? prefs.proxy : state.defaults.proxy || "";
      state.concurrent = clamp(prefs.concurrent ?? state.defaults.concurrent ?? 3, 1, 8);
      state.retries = clamp(prefs.retries ?? state.defaults.retries ?? 10, 0, 100);
      state.audioBitrate = BITRATES.includes(Number(prefs.audioBitrate))
        ? Number(prefs.audioBitrate)
        : 320;

      $("#versionLabel").textContent = state.app.version ? `v${state.app.version}` : "";
      renderBrowsers();
      renderConcurrent();
      syncSettings();
      renderDestination();
      state.ready = true;
    } catch (error) {
      toast("Could not load studio settings", "error", error.hint || error.message);
      renderDestination();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Static events                                                      */
  /* ------------------------------------------------------------------ */
  function bindEvents() {
    $("#urlForm").addEventListener("submit", (event) => {
      event.preventDefault();
      inspect();
    });

    $("#urlInput").addEventListener("input", () => {
      clearFormError();
      if (state.inspected && $("#urlInput").value.trim() !== state.inspectedUrl) {
        resetInspection({ clearInput: false, focus: false });
      }
    });

    $("#resetBtn").addEventListener("click", () => resetInspection());
    $("#brandHome").addEventListener("click", (event) => {
      event.preventDefault();
      resetInspection();
    });

    $("#fmtMp4").addEventListener("click", () => switchFormat("mp4"));
    $("#fmtMp3").addEventListener("click", () => switchFormat("mp3"));
    $("#downloadBtn").addEventListener("click", (event) => startDownload(event.currentTarget));
    $("#destBtn").addEventListener("click", browseDestination);
    $("#clearQueueBtn").addEventListener("click", clearFinished);

    $("#pickerToggle").addEventListener("click", togglePicker);
    $("#pickAll").addEventListener("click", () => {
      if (!state.playlist) return;
      visibleEntries().forEach((entry) => {
        const key = entryKey(entry);
        clearChildSelections(key);
        state.selected.add(key);
      });
      syncSelectionUi();
    });
    $("#pickNone").addEventListener("click", () => {
      state.selected.clear();
      syncSelectionUi();
    });
    $("#pickerSearch").addEventListener(
      "input",
      debounce(() => {
        state.pickerFilter = $("#pickerSearch").value;
        renderPicker();
      }, 130)
    );
    $("#pickerSort").addEventListener("change", (event) => {
      state.pickerSort = event.target.value || "default";
      renderPicker();
    });

    $("#settingsBtn").addEventListener("click", () => {
      syncSettings();
      $("#settingsDialog").showModal();
    });
    $("#closeSettingsBtn").addEventListener("click", () => $("#settingsDialog").close());
    $("#settingsDialog").addEventListener("click", (event) => {
      if (event.target === $("#settingsDialog")) $("#settingsDialog").close();
    });

    $("#browseDestBtn").addEventListener("click", browseDestination);
    $("#browseCookiesBtn").addEventListener("click", browseCookieFile);
    $("#checkBtn").addEventListener("click", runDoctor);

    $("#destInput").addEventListener("input", (event) => {
      setDirectory(event.target.value.trim(), { syncInput: false });
    });
    $("#browserSelect").addEventListener("change", (event) => {
      state.browser = event.target.value;
      savePrefs();
    });
    $("#cookiesInput").addEventListener("input", (event) => {
      state.cookies = event.target.value.trim();
      savePrefs();
    });
    $("#proxyInput").addEventListener("input", (event) => {
      state.proxy = event.target.value.trim();
      savePrefs();
    });
    $("#concurrentSelect").addEventListener("change", (event) => {
      state.concurrent = clamp(event.target.value, 1, 8);
      savePrefs();
    });
    $("#retriesInput").addEventListener("input", (event) => {
      state.retries = clamp(event.target.value, 0, 100);
      savePrefs();
    });

    $("#queueList").addEventListener("click", handleQueueClick);

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !isTyping() && !$("#settingsDialog").open) {
        event.preventDefault();
        $("#urlInput").focus();
      } else if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        if (state.inspected) {
          event.preventDefault();
          startDownload($("#downloadBtn"));
        }
      }
    });

    window.addEventListener("beforeunload", () => {
      flushPrefs();
      if (state.eventSource) state.eventSource.close();
    });
  }

  function renderBrowsers() {
    const select = $("#browserSelect");
    select.replaceChildren();
    const browsers = state.catalog.browsers || [];
    browsers.forEach((id) => {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = id === "auto" ? "Automatic (recommended)" : capitalize(id);
      select.appendChild(option);
    });
    select.value = browsers.includes(state.browser) ? state.browser : "auto";
  }

  function renderConcurrent() {
    const select = $("#concurrentSelect");
    select.replaceChildren();
    for (let value = 1; value <= 8; value += 1) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(value);
      select.appendChild(option);
    }
    select.value = String(state.concurrent);
  }

  function syncSettings() {
    $("#destInput").value = state.directory || "";
    $("#cookiesInput").value = state.cookies || "";
    $("#proxyInput").value = state.proxy || "";
    $("#retriesInput").value = String(state.retries);
    $("#concurrentSelect").value = String(state.concurrent);
    $("#browserSelect").value = state.browser || "auto";
  }

  /* ------------------------------------------------------------------ */
  /* Destination                                                        */
  /* ------------------------------------------------------------------ */
  function setDirectory(value, { syncInput = true } = {}) {
    state.directory = String(value || "");
    renderDestination();
    if (syncInput) $("#destInput").value = state.directory;
    savePrefs();
  }

  function renderDestination() {
    const path = state.directory || "Choose a download folder";
    $("#destPath").textContent = shortPath(path);
    $("#destBtn").title = state.directory || "Choose a download folder";
  }

  async function browseDestination() {
    try {
      const payload = await api("/api/choose-folder", {
        method: "POST",
        body: {
          initial: state.directory,
          title: "Choose a download folder",
        },
      });
      if (payload.path) setDirectory(payload.path);
    } catch (error) {
      toast("Could not open the folder picker", "error", error.hint || error.message);
    }
  }

  async function browseCookieFile() {
    try {
      const payload = await api("/api/choose-file", {
        method: "POST",
        body: {
          initial: state.cookies,
          title: "Choose a cookie file",
        },
      });
      if (payload.path) {
        state.cookies = payload.path;
        $("#cookiesInput").value = payload.path;
        savePrefs();
      }
    } catch (error) {
      toast("Could not open the file picker", "error", error.hint || error.message);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Inspection                                                         */
  /* ------------------------------------------------------------------ */
  function showFormError(message) {
    const node = $("#underbar");
    node.classList.add("is-error");
    node.textContent = message;
  }

  function clearFormError() {
    const node = $("#underbar");
    node.classList.remove("is-error");
    node.textContent = HINT;
  }

  function flashInvalid() {
    const bar = $("#urlForm");
    bar.classList.add("is-invalid");
    window.setTimeout(() => bar.classList.remove("is-invalid"), 400);
    $("#urlInput").focus();
  }

  function setInspectLoading(loading) {
    const button = $("#inspectBtn");
    button.disabled = loading;
    $("#inspectText").textContent = loading ? "Reading" : "Inspect";
  }

  async function inspect() {
    const url = $("#urlInput").value.trim();
    if (!url) {
      flashInvalid();
      return;
    }

    clearFormError();
    setInspectLoading(true);
    const token = ++state.inspectToken;
    try {
      const payload = await api("/api/inspect", {
        method: "POST",
        timeout: 120000,
        body: {
          url,
          cookies_from_browser: state.browser,
          cookies: state.cookies,
          proxy: state.proxy,
        },
      });
      if (token !== state.inspectToken) return;
      applyInspection(payload, url);
    } catch (error) {
      if (token !== state.inspectToken) return;
      showFormError(error.hint ? `${error.message} — ${error.hint}` : error.message);
      toast("Could not inspect that link", "error", error.hint || error.message);
    } finally {
      if (token === state.inspectToken) setInspectLoading(false);
    }
  }

  function applyInspection(payload, url) {
    if (!payload || !payload.media) {
      showFormError("Nothing usable came back for that link.");
      return;
    }
    state.inspected = payload.media;
    state.qualities = Array.isArray(payload.qualities)
      ? payload.qualities.filter((row) => Number(row.height) > 0)
      : [];
    state.hasVideo = payload.has_video !== false;
    state.hasAudio = payload.has_audio !== false;
    state.playlist = payload.playlist || null;
    state.playlistError = payload.playlist_error || "";
    state.inspectedUrl = url;
    state.selected.clear();
    state.expanded.clear();
    state.pickerFilter = "";
    state.pickerSort = "default";
    const sections = playlistSections();
    state.activeSection = sections.length ? sections[0].id : "";
    state.format = state.hasVideo ? "mp4" : "mp3";
    state.videoQuality = state.qualities.length
      ? state.qualities[state.qualities.length - 1].id
      : "best";
    state.audioBitrate = BITRATES.includes(Number(state.audioBitrate))
      ? Number(state.audioBitrate)
      : 320;

    $("#urlInput").value = url;
    $("#urlInput").blur();
    $("#pickerSearch").value = "";
    $("#picker").hidden = true;
    $("#pickerToggle").setAttribute("aria-expanded", "false");
    $("#result").hidden = false;
    document.body.classList.add("has-result");
    clearFormError();

    renderSource();
    renderPlaylistLine();
    renderFormat();
    renderQuality();
    renderDestination();
    renderCta();
  }

  function resetInspection({ clearInput = true, focus = true } = {}) {
    state.inspectToken += 1;
    state.inspected = null;
    state.inspectedUrl = "";
    state.qualities = [];
    state.hasVideo = true;
    state.hasAudio = true;
    state.playlist = null;
    state.playlistError = "";
    state.selected.clear();
    state.expanded.clear();
    state.pickerFilter = "";
    state.pickerSort = "default";
    state.activeSection = "";
    state.videoQuality = "";
    state.format = "mp4";

    document.body.classList.remove("has-result");
    $("#result").hidden = true;
    $("#picker").hidden = true;
    $("#pickerList").replaceChildren();
    $("#pickerSections").replaceChildren();
    $("#pickerSections").hidden = true;
    $("#pickerSort").value = "default";
    $("#pickerToggle").setAttribute("aria-expanded", "false");
    $("#pickerToggleText").textContent = "Choose from list";
    if (clearInput) $("#urlInput").value = "";
    setInspectLoading(false);
    clearFormError();
    if (focus) $("#urlInput").focus();
  }

  /* ------------------------------------------------------------------ */
  /* Result rendering                                                   */
  /* ------------------------------------------------------------------ */
  function renderSource() {
    const media = state.inspected;
    if (!media) return;

    $("#mediaTitle").textContent = media.title || "Untitled";

    const meta = $("#mediaMeta");
    meta.replaceChildren();
    const parts = [];
    if (media.channel) parts.push({ text: media.channel });
    else if (media.artist) parts.push({ text: media.artist });
    if (media.duration) parts.push({ text: formatClock(media.duration) });
    if (media.playlist_kind === "channel" && media.playlist_count) {
      const sectionCount = Number(media.playlist_sections) || 0;
      parts.push({
        text: `${media.playlist_count} item${media.playlist_count === 1 ? "" : "s"}${
          sectionCount > 1 ? ` · ${sectionCount} sections` : ""
        }`,
      });
    } else if (media.playlist_kind && media.playlist_count) {
      parts.push({ text: `${media.playlist_count} tracks` });
    }
    if (state.hasVideo) {
      const top = state.qualities[state.qualities.length - 1];
      parts.push({ text: top ? `up to ${top.label}` : "video" });
    } else {
      parts.push({ text: "audio only" });
    }
    if (media.is_live) parts.push({ text: "live", hot: true });

    parts.forEach((part, index) => {
      if (index > 0) {
        const dot = document.createElement("span");
        dot.className = "sep";
        meta.appendChild(dot);
      }
      const span = document.createElement("span");
      span.textContent = part.text;
      if (part.hot) span.className = "hot";
      meta.appendChild(span);
    });

    const wrap = $("#thumbWrap");
    wrap.replaceChildren();
    if (media.thumbnail) {
      const image = document.createElement("img");
      image.src = media.thumbnail;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => {
        wrap.replaceChildren(thumbFallback());
      });
      wrap.appendChild(image);
    } else {
      wrap.appendChild(thumbFallback());
    }
  }

  function thumbFallback() {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-down");
    icon.appendChild(use);
    return icon;
  }

  function isCollection() {
    return Boolean(state.inspected && state.inspected.playlist_kind);
  }

  function isChannel() {
    return Boolean(state.inspected && state.inspected.playlist_kind === "channel");
  }

  function playlistSections() {
    return state.playlist && Array.isArray(state.playlist.sections)
      ? state.playlist.sections
      : [];
  }

  function allEntries() {
    return (state.playlist && state.playlist.entries) || [];
  }

  function entryKey(entry) {
    return entry.key || `${entry.section || "all"}:${entry.index}`;
  }

  function entryMatchesFilter(entry, needle) {
    if (!needle) return true;
    return `${entry.title || ""} ${entry.uploader || ""}`.toLowerCase().includes(needle);
  }

  function findEntry(key) {
    const direct = allEntries().find((entry) => entryKey(entry) === key);
    if (direct) return direct;
    for (const record of state.expanded.values()) {
      const child = (record.entries || []).find((entry) => entry.key === key);
      if (child) return child;
    }
    return null;
  }

  function isExpandableEntry(entry) {
    if (entry.expandable === true) return true;
    if (entry.expandable === false) return false;
    const value = String(entry.url || "");
    if (!value) return false;
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.replace(/^www\./, "");
      if (
        !["youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"].includes(
          host
        )
      ) {
        return false;
      }
      if (parsed.pathname === "/playlist" || parsed.pathname.startsWith("/browse/")) {
        return true;
      }
      return Boolean(parsed.searchParams.get("list")) && parsed.pathname !== "/watch";
    } catch (error) {
      return false;
    }
  }

  function sectionPosition(entry) {
    if (entry.section) {
      const section = playlistSections().find((item) => item.id === entry.section);
      if (section && Array.isArray(section.entries)) {
        const index = section.entries.findIndex((item) => entryKey(item) === entryKey(entry));
        if (index >= 0) return index + 1;
      }
    }
    return entry.position || entry.index || 0;
  }

  function childPrefix(parentKey) {
    return `${parentKey}::`;
  }

  function expandedEntries(parentKey) {
    const record = state.expanded.get(parentKey);
    return (record && record.entries) || [];
  }

  function selectedChildCount(parentKey) {
    return expandedEntries(parentKey).filter((entry) => state.selected.has(entry.key)).length;
  }

  function clearChildSelections(parentKey) {
    const prefix = childPrefix(parentKey);
    for (const key of [...state.selected]) {
      if (key.startsWith(prefix)) state.selected.delete(key);
    }
  }

  function selectAllChildren(parentKey) {
    state.selected.delete(parentKey);
    clearChildSelections(parentKey);
    expandedEntries(parentKey).forEach((entry) => state.selected.add(entry.key));
  }

  function clearCollectionSelection(parentKey) {
    state.selected.delete(parentKey);
    clearChildSelections(parentKey);
  }

  function syncSelectionUi() {
    const list = $("#pickerList");
    if (!list) return;
    $$(".pl-row", list).forEach((node) => {
      const key = node.dataset.index || "";
      const child = node.classList.contains("is-child");
      const selected = state.selected.has(key);
      const check = $("input", node);
      if (check) {
        check.checked = selected;
        check.indeterminate = !child && !selected && selectedChildCount(key) > 0;
      }
      node.classList.toggle("is-selected", selected);
    });
    $$(".pl-children", list).forEach((panel) => {
      const key = panel.dataset.parent || "";
      const note = $(".pl-children-note.is-selection", panel);
      if (!note) return;
      note.textContent = state.selected.has(key)
        ? "Whole collection selected. Tick a track to switch to individual tracks."
        : "Select the collection for every track, or tick individual tracks.";
    });
    updatePickerNote();
    renderCta();
  }

  async function toggleExpanded(entry) {
    const key = entryKey(entry);
    const record = state.expanded.get(key);
    if (record && record.open) {
      record.open = false;
      renderPicker();
      return;
    }
    if (record && expandedEntries(key).length && !record.error) {
      record.open = true;
      renderPicker();
      return;
    }

    const token = state.inspectToken;
    state.expanded.set(key, {
      open: true,
      loading: true,
      entries: expandedEntries(key),
      error: "",
    });
    renderPicker();

    try {
      const payload = await api("/api/expand", {
        method: "POST",
        timeout: 120000,
        body: {
          url: entry.url,
          cookies_from_browser: state.browser,
          cookies: state.cookies,
          proxy: state.proxy,
        },
      });
      if (token !== state.inspectToken) return;
      const current = state.expanded.get(key) || {};
      const children = (Array.isArray(payload.entries) ? payload.entries : [])
        .filter((child) => child && child.url)
        .map((child, index) => ({
          ...child,
          key: `${childPrefix(key)}${child.index || index + 1}`,
          parentKey: key,
        }));
      state.expanded.set(key, {
        open: current.open !== false,
        loading: false,
        entries: children,
        error: "",
      });
    } catch (error) {
      if (token !== state.inspectToken) return;
      const current = state.expanded.get(key) || {};
      state.expanded.set(key, {
        open: current.open !== false,
        loading: false,
        entries: current.entries || [],
        error: error.hint || error.message || "Could not read this collection.",
      });
    }
    renderPicker();
  }

  function renderExpandedPanel(entry) {
    const key = entryKey(entry);
    const record = state.expanded.get(key);
    if (!record || !record.open) return null;

    const panel = document.createElement("div");
    panel.className = "pl-children";
    panel.dataset.parent = key;

    const head = document.createElement("div");
    head.className = "pl-children-head";
    const title = document.createElement("span");
    title.className = "pl-children-title";
    const count = expandedEntries(key).length;
    title.textContent = record.loading
      ? "Reading tracks…"
      : `${count} track${count === 1 ? "" : "s"}`;
    head.appendChild(title);

    if (!record.loading && !record.error && count) {
      const actions = document.createElement("div");
      actions.className = "pl-children-actions";
      const all = document.createElement("button");
      all.type = "button";
      all.className = "text-btn";
      all.textContent = "All tracks";
      all.addEventListener("click", () => {
        selectAllChildren(key);
        syncSelectionUi();
      });
      const none = document.createElement("button");
      none.type = "button";
      none.className = "text-btn";
      none.textContent = "None";
      none.addEventListener("click", () => {
        clearCollectionSelection(key);
        syncSelectionUi();
      });
      actions.append(all, none);
      head.appendChild(actions);
    }
    panel.appendChild(head);

    if (record.loading) {
      const note = document.createElement("p");
      note.className = "pl-children-note";
      note.textContent = "Fetching the track list…";
      panel.appendChild(note);
      return panel;
    }

    if (record.error) {
      const note = document.createElement("p");
      note.className = "pl-children-note is-error";
      note.textContent = record.error;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "text-btn";
      retry.textContent = "Retry";
      retry.addEventListener("click", () => {
        state.expanded.delete(key);
        toggleExpanded(entry);
      });
      note.append(" ", retry);
      panel.appendChild(note);
      return panel;
    }

    const needle = state.pickerFilter.trim().toLowerCase();
    const children = sortEntries(
      expandedEntries(key).filter((child) => entryMatchesFilter(child, needle))
    );
    if (!children.length) {
      const note = document.createElement("p");
      note.className = "pl-children-note";
      note.textContent = count
        ? "No tracks match that filter."
        : "No tracks were found in this collection.";
      panel.appendChild(note);
      return panel;
    }

    const list = document.createElement("div");
    list.className = "pl-children-list";
    children.forEach((child) => {
      list.appendChild(renderPickerRow(child, { child: true, parentKey: key }));
    });
    panel.appendChild(list);

    const note = document.createElement("p");
    note.className = "pl-children-note is-selection";
    note.textContent = state.selected.has(key)
      ? "Whole collection selected. Tick a track to switch to individual tracks."
      : "Select the collection for every track, or tick individual tracks.";
    panel.appendChild(note);
    return panel;
  }

  function renderPickerRow(entry, { child = false, parentKey = "" } = {}) {
    const node = $("#pickerRowTemplate").content.firstElementChild.cloneNode(true);
    const check = $("input", node);
    const key = entryKey(entry);
    const selected = state.selected.has(key);
    node.dataset.index = key;
    node.classList.toggle("is-selected", selected);
    node.classList.toggle("is-child", child);
    check.checked = selected;
    if (!child) {
      check.indeterminate = !selected && selectedChildCount(key) > 0;
    }

    $(".pl-index", node).textContent = String(child ? entry.index : sectionPosition(entry));
    $(".pl-title", node).textContent = entry.title || "Untitled";
    const duration = $(".pl-dur", node);
    if (entry.duration) {
      duration.textContent = formatClock(entry.duration);
    } else if (!child && entry.track_count) {
      const count = Number(entry.track_count);
      duration.textContent = `${count} track${count === 1 ? "" : "s"}`;
    } else {
      duration.textContent = "";
    }

    const image = $(".pl-thumb", node);
    if (entry.thumbnail) {
      image.src = entry.thumbnail;
      image.addEventListener("error", () => image.remove());
    } else {
      image.remove();
    }

    const expand = $(".pl-expand", node);
    const expandable = !child && isExpandableEntry(entry);
    expand.hidden = !expandable;
    if (expandable) {
      const record = state.expanded.get(key);
      const open = Boolean(record && record.open);
      node.classList.toggle("is-expanded", open);
      expand.setAttribute("aria-expanded", String(open));
      expand.title = open ? "Hide tracks" : "Show tracks";
      expand.setAttribute("aria-label", expand.title);
      expand.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleExpanded(entry);
      });
    }

    check.addEventListener("change", () => {
      if (child) {
        if (check.checked) {
          state.selected.delete(parentKey);
          state.selected.add(key);
        } else {
          state.selected.delete(key);
        }
      } else if (check.checked) {
        clearChildSelections(key);
        state.selected.add(key);
      } else {
        state.selected.delete(key);
      }
      syncSelectionUi();
    });

    return node;
  }

  function entrySortNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function sortEntries(entries) {
    const sorted = entries.slice();
    const sort = state.pickerSort || "default";
    const byTitle = (left, right) =>
      String(left.title || "").localeCompare(String(right.title || ""), undefined, {
        numeric: true,
        sensitivity: "base",
      });
    if (sort === "title") sorted.sort(byTitle);
    else if (sort === "title-desc") sorted.sort((left, right) => byTitle(right, left));
    else if (sort === "duration") {
      sorted.sort(
        (left, right) =>
          (entrySortNumber(left.duration) || Number.MAX_SAFE_INTEGER) -
          (entrySortNumber(right.duration) || Number.MAX_SAFE_INTEGER)
      );
    } else if (sort === "duration-desc") {
      sorted.sort(
        (left, right) => entrySortNumber(right.duration) - entrySortNumber(left.duration)
      );
    } else if (sort === "uploader") {
      sorted.sort((left, right) =>
        String(left.uploader || "").localeCompare(String(right.uploader || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        })
      );
    } else {
      sorted.sort(
        (left, right) =>
          entrySortNumber(left.position || left.index) -
          entrySortNumber(right.position || right.index)
      );
    }
    return sorted;
  }

  function sectionCountLabel(count) {
    const value = Number(count) || 0;
    return String(value);
  }

  function renderPlaylistLine() {
    const line = $("#playlistLine");
    const toggle = $("#pickerToggle");
    const toggleText = $("#pickerToggleText");
    const kicker = $("#playlistKicker");
    const text = $("#playlistText");
    const hint = $("#playlistHint");

    if (!state.playlist) {
      if (state.playlistError) {
        line.hidden = false;
        line.title = state.playlistError;
        kicker.textContent = "List unavailable";
        text.textContent = "This link belongs to a list that could not be read.";
        hint.textContent = "It will download as a single video.";
        toggle.hidden = true;
      } else {
        line.hidden = true;
        line.title = "";
        toggle.hidden = false;
        toggleText.textContent = "Choose from list";
      }
      return;
    }

    line.hidden = false;
    line.title = "";
    toggle.hidden = false;
    const open = !$("#picker").hidden;
    toggle.setAttribute("aria-expanded", String(open));

    const count = state.playlist.count || (state.playlist.entries || []).length;
    const sections = playlistSections();
    if (isChannel()) {
      const source = state.playlist.source === "youtube-music" ? "YouTube Music" : "YouTube";
      const sectionCount = sections.length || 1;
      kicker.textContent = "Channel collection";
      text.textContent = `${count} item${count === 1 ? "" : "s"} · ${sectionCount} section${
        sectionCount === 1 ? "" : "s"
      }`;
      hint.textContent = open
        ? "Pick a section, then tick the videos, albums, or tracks you want."
        : `${source} channel. Open the list to choose a section or individual items.`;
      toggleText.textContent = open
        ? "Hide list"
        : count
          ? `Browse ${count} item${count === 1 ? "" : "s"}`
          : "Browse list";
      return;
    }

    if (isCollection()) {
      const kind = capitalize(state.playlist.kind || "list");
      kicker.textContent = `${kind} detected`;
      text.textContent = `“${state.playlist.title || "This list"}” · ${count} track${
        count === 1 ? "" : "s"
      }`;
      hint.textContent = open
        ? "Tick tracks below, or select the whole collection."
        : "This link contains a full track list. Open it to choose only the tracks you want.";
      toggleText.textContent = open
        ? "Hide list"
        : count
          ? `Choose from ${count} track${count === 1 ? "" : "s"}`
          : "Choose tracks";
      return;
    }

    kicker.textContent = "Playlist detected";
    text.textContent = `“${state.playlist.title || "This list"}” · ${count} track${
      count === 1 ? "" : "s"
    }`;
    hint.textContent = open
      ? "Tick any track from the list, or keep just this video."
      : "This video belongs to a playlist. Open it to choose more tracks.";
    toggleText.textContent = open ? "Hide list" : "Open playlist";
  }

  function switchFormat(format) {
    if (format === "mp4" && !state.hasVideo) return;
    if (format === "mp3" && !state.hasAudio) return;
    if (state.format === format) return;
    state.format = format;
    renderFormat();
    renderQuality();
    renderCta();
  }

  function renderFormat() {
    if (!state.hasVideo && state.format === "mp4") state.format = "mp3";
    if (!state.hasAudio && state.format === "mp3") state.format = "mp4";

    const mp4 = $("#fmtMp4");
    const mp3 = $("#fmtMp3");
    mp4.disabled = !state.hasVideo;
    mp3.disabled = !state.hasAudio;
    mp4.setAttribute("aria-pressed", String(state.format === "mp4"));
    mp3.setAttribute("aria-pressed", String(state.format === "mp3"));

    const note = $("#formatNote");
    if (!state.hasVideo) note.textContent = "no video stream";
    else if (!state.hasAudio) note.textContent = "no audio stream";
    else if (state.format === "mp3") note.textContent = "converted · tagged";
    else note.textContent = state.qualities.length > 1 ? "source resolutions" : "best available";
  }

  function audioEstimate(bitrate) {
    const duration = Number(state.inspected && state.inspected.duration);
    if (!Number.isFinite(duration) || duration <= 0) return null;
    return Math.round((bitrate * 1000 * duration) / 8);
  }

  function currentVideoLabel() {
    if (state.videoQuality === "best") return "best quality";
    const row = state.qualities.find((item) => item.id === state.videoQuality);
    return row ? row.label : state.videoQuality || "best quality";
  }

  function renderQuality() {
    const host = $("#qualityList");
    host.replaceChildren();
    const label = $("#qualityLabel");
    const note = $("#qualityNote");
    let rows = [];

    if (state.format === "mp3") {
      label.textContent = "Bitrate";
      note.textContent = "128 → 320 kbps";
      rows = BITRATES.map((bitrate) => ({
        value: String(bitrate),
        title: `${bitrate} kbps`,
        size: audioEstimate(bitrate),
        fps: null,
        selected: state.audioBitrate === bitrate,
      }));
      if (!rows.some((row) => row.selected)) {
        state.audioBitrate = 320;
        rows[rows.length - 1].selected = true;
      }
    } else {
      label.textContent = "Quality";
      note.textContent = state.qualities.length > 1 ? "lowest → best" : "";
      rows = (state.qualities.length
        ? state.qualities
        : [{ id: "best", label: "Best available", size: null, fps: null }]
      ).map((item) => ({
        value: item.id === "best" ? "best" : item.id,
        title: item.label || (item.height ? `${item.height}p` : "Best available"),
        size: item.size || null,
        fps: item.fps || null,
        selected: state.videoQuality === item.id,
      }));
      if (!rows.some((row) => row.selected)) {
        state.videoQuality = rows[rows.length - 1].value;
        rows[rows.length - 1].selected = true;
      }
    }

    rows.forEach((row) => {
      const node = $("#qualityRowTemplate").content.firstElementChild.cloneNode(true);
      node.dataset.value = row.value;
      node.classList.toggle("is-selected", row.selected);

      const input = $("input", node);
      input.value = row.value;
      input.checked = row.selected;

      $(".q-name", node).textContent = row.title;

      const fps = $(".q-fps", node);
      if (row.fps && Number(row.fps) > 30) {
        fps.hidden = false;
        fps.textContent = String(Math.round(Number(row.fps)));
        fps.title = `${Math.round(Number(row.fps))} fps`;
      }

      $(".q-size", node).textContent = row.size ? `~${formatBytes(row.size)}` : "";

      input.addEventListener("change", () => {
        if (!input.checked) return;
        if (state.format === "mp3") {
          state.audioBitrate = Number(row.value);
          savePrefs();
        } else {
          state.videoQuality = row.value;
        }
        renderQuality();
        renderCta();
      });

      host.appendChild(node);
    });
  }

  function renderCta() {
    const button = $("#downloadBtn");
    const selectedCount = state.selected.size;
    const collection = isCollection();
    const itemWord = isChannel() ? "item" : "track";
    const total = state.playlist
      ? state.playlist.count || (state.playlist.entries || []).length
      : 0;

    let prefix = "";
    if (state.playlist && selectedCount) {
      const selectedEntries = [...state.selected].map(findEntry).filter(Boolean);
      const collections = selectedEntries.filter(
        (entry) => !entry.parentKey && isExpandableEntry(entry)
      ).length;
      const tracks = selectedEntries.filter((entry) => entry.parentKey).length;
      const items = selectedEntries.length - collections - tracks;
      const parts = [];
      if (collections) {
        parts.push(`${collections} collection${collections === 1 ? "" : "s"}`);
      }
      if (tracks) {
        parts.push(`${tracks} track${tracks === 1 ? "" : "s"}`);
      }
      if (items) {
        parts.push(`${items} ${itemWord}${items === 1 ? "" : "s"}`);
      }
      prefix = `${parts.join(" + ") || `${selectedCount} selected`} · `;
    } else if (collection && total) {
      prefix = `${total} ${itemWord}${total === 1 ? "" : "s"} · `;
    }

    if (state.format === "mp3") {
      $("#ctaText").textContent = `Download ${prefix}MP3 · ${state.audioBitrate} kbps`;
    } else {
      $("#ctaText").textContent = `Download ${prefix}${currentVideoLabel()} · MP4`;
    }
    button.disabled = !state.inspected;

    const note = $("#ctaNote");
    if (!state.inspected) {
      note.textContent = "";
    } else if (state.playlist && selectedCount) {
      note.textContent = isChannel()
        ? `${selectedCount} selected — every file keeps its own title.`
        : `${selectedCount} of ${total} selected — every file keeps its own title.`;
    } else if (state.playlist && collection) {
      note.textContent = isChannel()
        ? "Every section is included — playlists and albums expand into their items."
        : "All tracks are included — every file keeps its own title.";
    } else if (state.playlist) {
      note.textContent = "This video is part of a playlist — use Open playlist to choose more.";
    } else {
      const extension = state.format === "mp3" ? "mp3" : "mp4";
      note.textContent = `Saves as “${state.inspected.title || "title"}.${extension}”`;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Playlist picker                                                    */
  /* ------------------------------------------------------------------ */
  function togglePicker() {
    if (!state.playlist) return;
    const picker = $("#picker");
    const open = picker.hidden;
    picker.hidden = !open;
    renderPlaylistLine();
    if (open) {
      renderPicker();
      $("#pickerSearch").focus();
    }
  }

  function visibleEntries() {
    const sections = playlistSections();
    let entries = allEntries();
    if (sections.length && state.activeSection) {
      entries = entries.filter((entry) => (entry.section || "") === state.activeSection);
    }
    const needle = state.pickerFilter.trim().toLowerCase();
    if (needle) {
      entries = entries.filter((entry) => {
        if (entryMatchesFilter(entry, needle)) return true;
        const record = state.expanded.get(entryKey(entry));
        return Boolean(
          record &&
            record.entries &&
            record.entries.some((child) => entryMatchesFilter(child, needle))
        );
      });
    }
    return sortEntries(entries);
  }

  function renderPickerSections() {
    const host = $("#pickerSections");
    const sections = playlistSections();
    host.replaceChildren();
    host.hidden = sections.length < 2;
    if (!sections.length) return;

    sections.forEach((section) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "section-tab";
      button.dataset.section = section.id;
      button.setAttribute("aria-pressed", String(section.id === state.activeSection));
      button.title = `${section.title || section.id} · ${sectionCountLabel(
        section.count || (section.entries || []).length
      )} items`;

      const name = document.createElement("span");
      name.className = "section-name";
      name.textContent = section.title || section.id;
      const count = document.createElement("span");
      count.className = "section-count";
      count.textContent = sectionCountLabel(
        section.count || (section.entries || []).length
      );
      button.append(name, count);
      button.addEventListener("click", () => {
        state.activeSection = section.id;
        renderPicker();
      });
      host.appendChild(button);
    });
  }

  function renderPicker() {
    if (!state.playlist) return;
    const list = $("#pickerList");
    list.replaceChildren();
    const sections = playlistSections();
    if (sections.length && !sections.some((section) => section.id === state.activeSection)) {
      state.activeSection = sections[0].id;
    }
    renderPickerSections();
    const visible = visibleEntries();

    if (!visible.length) {
      const empty = document.createElement("p");
      empty.className = "picker-note";
      empty.textContent = allEntries().length
        ? "No items match that filter."
        : "This list is empty.";
      list.appendChild(empty);
    }

    visible.forEach((entry) => {
      list.appendChild(renderPickerRow(entry));
      const panel = renderExpandedPanel(entry);
      if (panel) list.appendChild(panel);
    });

    updatePickerNote();
  }

  function updatePickerNote() {
    if (!state.playlist) return;
    const total = state.playlist.count || (state.playlist.entries || []).length;
    const sections = playlistSections();
    const active = sections.find((section) => section.id === state.activeSection);
    const activeTotal = active
      ? Number(active.count) || (active.entries || []).length
      : total;
    const note = $("#pickerNote");
    if (!state.selected.size) {
      if (isChannel()) {
        note.textContent = `Nothing selected — every section (${total} items) will download.`;
      } else if (isCollection()) {
        note.textContent = `Nothing selected — all ${activeTotal} will download.`;
      } else {
        note.textContent = "Nothing selected — just this video downloads.";
      }
      return;
    }
    if (isChannel()) {
      note.textContent = `${state.selected.size} selected — the rest are skipped.`;
    } else {
      note.textContent = `${state.selected.size} of ${total} selected — the rest are skipped.`;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Download                                                           */
  /* ------------------------------------------------------------------ */
  function downloadTargets() {
    const selected = [...state.selected];
    if (selected.length) {
      const urls = selected
        .map((key) => findEntry(key))
        .filter(Boolean)
        .map((entry) => entry.url)
        .filter(Boolean);
      if (urls.length) return [...new Set(urls)];
    }

    // A channel is made of tab URLs.  Sending the root back would make yt-dlp
    // choose one tab; send the individual entries so every visible section is
    // included.  Playlist and album entries are expanded by playlist_mode=auto.
    const entries = allEntries();
    if (playlistSections().length && entries.length) {
      return [...new Set(entries.map((entry) => entry.url).filter(Boolean))];
    }
    return state.inspectedUrl;
  }

  function buildPayload() {
    const audio = state.format === "mp3";
    const payload = {
      urls: downloadTargets(),
      mode: audio ? "audio" : "video",
      quality: audio ? "best" : state.videoQuality || "best",
      container: "auto",
      audio_format: "mp3",
      bitrate: state.audioBitrate,
      music: false,
      directory: state.directory,
      template_mode: "title",
      template: "",
      subtitles: false,
      subtitle_langs: "",
      thumbnail: true,
      metadata: true,
      info_json: false,
      chapters: false,
      sponsorblock: [],
      playlist_mode: "auto",
      concurrent: state.concurrent,
      retries: state.retries,
      cookies_from_browser: state.browser,
      cookies: state.cookies,
      proxy: state.proxy,
      keep_video: false,
      dry_run: false,
    };
    return payload;
  }

  async function startDownload(button) {
    if (!state.inspected || !state.inspectedUrl) return;
    if (!state.directory) {
      toast("Choose a download folder first", "warning", "Open Settings to set one.");
      return;
    }

    const payload = buildPayload();
    button.disabled = true;
    try {
      const response = await api("/api/download", {
        method: "POST",
        body: payload,
        timeout: 30000,
      });
      if (response.job) {
        upsertJob(response.job);
        scheduleQueue();
      }
      const count = response.job && response.job.count ? response.job.count : 1;
      toast(
        "Added to downloads",
        "success",
        count > 1
          ? `${count} ${isChannel() ? "items" : "tracks"} · ${response.job.format || ""}`
          : "Starting now"
      );
    } catch (error) {
      toast("Could not start the download", "error", error.hint || error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function clearFinished() {
    try {
      const response = await api("/api/jobs/clear", { method: "POST", body: {} });
      if (Array.isArray(response.jobs)) {
        state.jobs = response.jobs;
        scheduleQueue();
      }
    } catch (error) {
      toast("Could not clear the list", "error", error.hint || error.message);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Queue                                                              */
  /* ------------------------------------------------------------------ */
  function findJob(id) {
    return state.jobs.find((job) => job.id === id) || null;
  }

  function upsertJob(job) {
    if (!job || !job.id) return;
    const index = state.jobs.findIndex((item) => item.id === job.id);
    if (index === -1) state.jobs.push(job);
    else state.jobs[index] = job;
  }

  function patchTask(jobId, task, summary) {
    const job = findJob(jobId);
    if (!job) return;
    job.tasks = job.tasks || [];
    const index = job.tasks.findIndex((item) => Number(item.id) === Number(task.id));
    if (index === -1) job.tasks.push(task);
    else job.tasks[index] = task;
    if (summary) job.summary = summary;
  }

  function scheduleQueue() {
    if (state.queueTimer) return;
    state.queueTimer = window.setTimeout(() => {
      state.queueTimer = 0;
      renderQueue();
    }, 40);
  }

  function renderQueue() {
    const jobs = state.jobs;
    const section = $("#queue");
    section.hidden = jobs.length === 0;
    $("#queueCount").textContent = jobs.length ? String(jobs.length) : "";
    const list = $("#queueList");
    list.replaceChildren();
    jobs.forEach((job) => list.appendChild(buildJob(job)));
  }

  function taskPercent(task) {
    if (!task) return null;
    const total = Number(task.total);
    const downloaded = Number(task.downloaded);
    if (total > 0 && Number.isFinite(downloaded)) {
      return Math.round(Math.min(1, downloaded / total) * 100);
    }
    if (task.status === "completed") return 100;
    return null;
  }

  function jobProgress(job) {
    const tasks = job.tasks || [];
    const total = Math.max(Number(job.count) || 0, tasks.length || 1);
    const activeTasks = tasks.filter((task) => task.status === "active");
    const active = activeTasks[0] || null;
    const settled = tasks.filter((task) => task.status && task.status !== "active").length;
    let fraction = 0;
    activeTasks.forEach((task) => {
      const percent = taskPercent(task);
      fraction += percent == null ? 0.02 : percent / 100;
    });
    let overall = total ? (settled + fraction) / total : fraction;
    if (job.status === "completed") overall = 1;
    if (job.status === "partial" && total) overall = Math.max(overall, settled / total);
    return {
      percent: Math.max(0, Math.min(100, Math.round(overall * 100))),
      active,
      total,
      settled,
    };
  }

  function jobState(job, progress) {
    if (job.status === "running") {
      const stage = capitalize((progress.active && progress.active.stage) || "downloading");
      return { label: stage, className: "is-running" };
    }
    if (job.status === "queued") return { label: "Queued", className: "is-queued" };
    if (job.status === "cancelling") return { label: "Stopping", className: "is-running" };
    if (job.status === "completed") return { label: "Done", className: "is-done" };
    if (job.status === "partial") return { label: "Partly done", className: "is-partial" };
    if (job.status === "failed") return { label: "Failed", className: "is-failed" };
    if (job.status === "cancelled") return { label: "Cancelled", className: "is-cancelled" };
    return { label: job.status || "", className: "" };
  }

  function buildJob(job) {
    const node = $("#jobTemplate").content.firstElementChild.cloneNode(true);
    node.dataset.jobId = job.id;

    const tasks = job.tasks || [];
    const summary = job.summary || {};
    const multi = (Number(job.count) || 1) > 1;
    const progress = jobProgress(job);
    const active = progress.active;
    const stateInfo = jobState(job, progress);

    node.classList.add(`is-${job.status}`);
    node.classList.toggle("is-done", job.status === "completed" || job.status === "partial");
    node.classList.toggle("is-failed", job.status === "failed");

    $(".job-title", node).textContent = multi
      ? `${job.count || progress.total} tracks`
      : (tasks[0] && tasks[0].title) || job.format || "Download";
    $(".job-format", node).textContent = job.format || "";

    const stateNode = $(".job-state", node);
    stateNode.textContent = stateInfo.label;
    stateNode.className = `job-state ${stateInfo.className}`;

    $(".job-track i", node).style.width = `${progress.percent}%`;
    $(".job-pct", node).textContent =
      job.status === "queued" ? "" : `${progress.percent}%`;

    const detail = $(".job-detail", node);
    if (job.status === "running" && active) {
      const bits = [];
      if (active.total && active.downloaded != null) {
        bits.push(`${formatBytes(active.downloaded)} of ${formatBytes(active.total)}`);
      } else if (active.downloaded) {
        bits.push(formatBytes(active.downloaded));
      }
      if (active.speed) bits.push(formatSpeed(active.speed));
      if (active.eta) bits.push(`${formatClock(active.eta)} left`);
      if (multi) bits.push(`${progress.settled} of ${progress.total} done`);
      detail.textContent = bits.join(" · ") || (active.stage || "Downloading");
    } else if (job.status === "queued") {
      detail.textContent = "Waiting for the current download";
    } else if (job.status === "completed" || job.status === "partial") {
      const bits = [];
      if (summary.size) bits.push(`${formatBytes(summary.size)} saved`);
      if (summary.elapsed) bits.push(formatClock(summary.elapsed));
      if (multi) {
        const done = summary.completed || tasks.length;
        bits.push(`${done} of ${job.count || progress.total} files`);
      }
      detail.textContent = bits.join(" · ") || "Finished";
    } else if (job.status === "failed") {
      detail.textContent = job.error || "Download failed";
    } else if (job.status === "cancelled") {
      detail.textContent = "Cancelled";
    } else {
      detail.textContent = "";
    }

    const error = $(".job-error", node);
    const failedTask = tasks.find((task) => task.status === "failed" && task.error);
    if (job.error || failedTask) {
      error.hidden = false;
      error.textContent = job.error || failedTask.error;
    }

    const action = $(".job-action", node);
    if (["queued", "running", "cancelling"].includes(job.status)) {
      action.hidden = false;
      action.dataset.action = "cancel";
      action.textContent = job.status === "cancelling" ? "Cancelling…" : "Cancel";
      action.disabled = job.status === "cancelling";
    } else if (["completed", "partial"].includes(job.status)) {
      action.hidden = false;
      action.dataset.action = "folder";
      action.textContent = "Open folder";
      action.disabled = false;
    }

    if (multi && tasks.length > 1) {
      const host = $(".task-list", node);
      host.hidden = false;
      tasks.slice(-4).forEach((task) => {
        const row = $("#taskRowTemplate").content.firstElementChild.cloneNode(true);
        $(".task-title", row).textContent = task.title || "Untitled";
        const percent = taskPercent(task);
        let label = percent == null ? capitalize(task.stage || task.status || "") : `${percent}%`;
        if (task.status === "failed") label = "failed";
        else if (task.status === "completed") label = "done";
        else if (task.status === "cancelled") label = "cancelled";
        $(".task-pct", row).textContent = label;
        host.appendChild(row);
      });
    }

    return node;
  }

  async function handleQueueClick(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const jobNode = button.closest(".job");
    const job = jobNode ? findJob(jobNode.dataset.jobId) : null;
    if (!job) return;

    if (button.dataset.action === "cancel") {
      button.disabled = true;
      button.textContent = "Cancelling…";
      try {
        await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {
          method: "POST",
          body: {},
        });
      } catch (error) {
        toast("Could not cancel that download", "error", error.hint || error.message);
        button.disabled = false;
      }
      return;
    }

    if (button.dataset.action === "folder") {
      try {
        await api("/api/open-path", {
          method: "POST",
          body: { path: job.destination || state.directory, select: false },
        });
      } catch (error) {
        toast("Could not open that folder", "error", error.hint || error.message);
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /* Server-sent events                                                 */
  /* ------------------------------------------------------------------ */
  function setConnection(online) {
    const node = $("#connection");
    node.classList.toggle("is-online", online);
    node.classList.toggle("is-offline", !online);
    $("#connectionText").textContent = online ? "online" : "reconnecting";
  }

  function connect() {
    if (state.eventSource) state.eventSource.close();
    try {
      const source = new EventSource("/api/events");
      state.eventSource = source;
      source.addEventListener("open", () => setConnection(true));
      source.addEventListener("error", () => setConnection(false));
      source.addEventListener("message", (event) => {
        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        handleEvent(payload);
      });
    } catch (error) {
      setConnection(false);
    }
  }

  function handleEvent(payload) {
    if (!payload) return;
    switch (payload.type) {
      case "snapshot":
        state.jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
        scheduleQueue();
        break;
      case "job_created":
      case "job_state":
      case "job_finished":
        upsertJob(payload.job);
        scheduleQueue();
        break;
      case "task_added":
      case "task":
        patchTask(payload.job_id, payload.task, payload.summary);
        scheduleQueue();
        break;
      case "jobs_removed":
        state.jobs = state.jobs.filter((job) => !(payload.ids || []).includes(job.id));
        scheduleQueue();
        break;
      case "toast":
        toast(payload.message, payload.level || "info", payload.detail || "");
        break;
      default:
        break;
    }
  }

  /* ------------------------------------------------------------------ */
  /* System check                                                       */
  /* ------------------------------------------------------------------ */
  async function runDoctor() {
    const button = $("#checkBtn");
    button.disabled = true;
    button.textContent = "Checking…";
    try {
      const payload = await api("/api/doctor", { timeout: 60000 });
      renderDoctor(payload.report);
    } catch (error) {
      toast("System check failed", "error", error.hint || error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Run system check";
    }
  }

  function renderDoctor(report) {
    const host = $("#systemResult");
    host.hidden = false;
    host.replaceChildren();
    const checks = (report && report.checks) || [];
    if (!checks.length) {
      host.textContent = "No checks were returned.";
      return;
    }
    checks.forEach((check) => {
      const row = document.createElement("div");
      row.className = `check-row ${check.status || ""}`;
      const dot = document.createElement("span");
      dot.className = "check-dot";
      const text = document.createElement("span");
      const name = document.createElement("b");
      name.textContent = check.name || "Check";
      text.appendChild(name);
      if (check.detail) {
        text.append(document.createTextNode(` — ${check.detail}`));
      }
      row.append(dot, text);
      if (String(check.name || "").toLowerCase() === "aria2c" && check.status !== "ok") {
        const get = document.createElement("button");
        get.type = "button";
        get.className = "text-btn";
        get.textContent = "Download";
        get.addEventListener("click", async () => {
          get.disabled = true;
          get.textContent = "Downloading…";
          try {
            const result = await api("/api/aria2/install", {
              method: "POST",
              timeout: 120000,
            });
            toast("aria2c installed", "success", result.path || "");
            if (result.report) renderDoctor(result.report);
          } catch (error) {
            toast("Could not install aria2c", "error", error.hint || error.message);
            get.disabled = false;
            get.textContent = "Download";
          }
        });
        row.appendChild(get);
      }
      host.appendChild(row);
    });
  }

  boot();
})();
