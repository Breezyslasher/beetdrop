/* Trackpull UI logic. Vue 3 global build, no build step.
   The layout setting is a client preference and lives in localStorage;
   everything else round-trips through the API. */

const { createApp } = Vue;

const LS_PASSWORD = "trackpull.password";
const LS_LAYOUT = "trackpull.layout";
const MOBILE_QUERY = "(max-width: 700px)";

createApp({
  data() {
    return {
      query: "",
      results: [],
      searching: false,
      searched: false,
      grabbing: {},

      jobs: [],
      queueOpen: false,

      settings: null,
      health: null,
      settingsOpen: false,
      draft: {},

      password: localStorage.getItem(LS_PASSWORD) || "",
      passwordNeeded: false,
      passwordInput: "",

      layout: localStorage.getItem(LS_LAYOUT) || "auto",
      mediaMobile: window.matchMedia(MOBILE_QUERY).matches,

      toast: "",
      toastTimer: null,
      es: null,
      esDelay: 1000,
    };
  },

  computed: {
    layoutClass() {
      const mode = this.layout === "auto"
        ? (this.mediaMobile ? "mobile" : "desktop")
        : this.layout;
      return "layout-" + mode;
    },
    activeJobs() {
      return this.jobs.filter((j) => j.stage !== "done" && j.stage !== "failed");
    },
    sortedJobs() {
      return [...this.jobs].sort((a, b) => b.created_at - a.created_at);
    },
    healthClass() {
      if (!this.health) return "";
      return this.health.status === "ok" ? "ok" : "degraded";
    },
    healthTitle() {
      if (!this.health) return "Checking server";
      return this.health.status === "ok"
        ? "Server ok, inbox writable"
        : "Problem: " + (this.health.inbox_problem || "server degraded");
    },
  },

  methods: {
    async api(path, options = {}) {
      const headers = Object.assign({}, options.headers);
      if (this.password) headers["X-Trackpull-Password"] = this.password;
      if (options.body) headers["Content-Type"] = "application/json";
      const response = await fetch(path, Object.assign({}, options, { headers }));
      if (response.status === 401) {
        this.passwordNeeded = true;
        throw new Error("password required");
      }
      if (!response.ok) {
        let detail = response.statusText;
        try { detail = (await response.json()).detail || detail; } catch (e) { /* not json */ }
        throw new Error(detail);
      }
      return response.json();
    },

    showToast(message) {
      this.toast = message;
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => { this.toast = ""; }, 4000);
    },

    fmtDuration(seconds) {
      if (seconds == null) return "?:??";
      const m = Math.floor(seconds / 60);
      const s = String(Math.floor(seconds % 60)).padStart(2, "0");
      return m + ":" + s;
    },

    async search() {
      const q = this.query.trim();
      if (!q || this.searching) return;
      this.searching = true;
      try {
        const body = await this.api("/api/search?q=" + encodeURIComponent(q));
        this.results = body.results;
        this.searched = true;
      } catch (err) {
        if (err.message !== "password required") this.showToast("Search failed: " + err.message);
      } finally {
        this.searching = false;
      }
    },

    async grab(result) {
      this.grabbing[result.video_id] = true;
      try {
        const job = await this.api("/api/grab", {
          method: "POST",
          body: JSON.stringify({ video_id: result.video_id }),
        });
        this.upsertJob(job);
        this.showToast("Queued: " + result.title);
      } catch (err) {
        delete this.grabbing[result.video_id];
        if (err.message !== "password required") this.showToast("Grab failed: " + err.message);
      }
    },

    async retry(job) {
      try {
        const updated = await this.api("/api/jobs/" + job.id + "/retry", { method: "POST" });
        this.upsertJob(updated);
      } catch (err) {
        if (err.message !== "password required") this.showToast("Retry failed: " + err.message);
      }
    },

    upsertJob(job) {
      const index = this.jobs.findIndex((j) => j.id === job.id);
      if (index >= 0) {
        const previous = this.jobs[index];
        this.jobs[index] = job;
        // Success message says handed off, not imported: whether it
        // imports is beets' decision and Trackpull does not know.
        if (previous.stage !== "done" && job.stage === "done") {
          this.showToast("Handed off to inbox: " + (job.title || job.video_id));
        }
      } else {
        this.jobs.push(job);
      }
      if (job.stage === "done" || job.stage === "failed") {
        delete this.grabbing[job.video_id];
      }
    },

    async refreshJobs() {
      try {
        const body = await this.api("/api/jobs");
        this.jobs = body.jobs;
      } catch (err) { /* surfaced elsewhere */ }
    },

    async refreshHealth() {
      try {
        this.health = await (await fetch("/api/health")).json();
      } catch (err) { this.health = null; }
    },

    connectEvents() {
      if (this.es) this.es.close();
      const url = this.password
        ? "/events?password=" + encodeURIComponent(this.password)
        : "/events";
      this.es = new EventSource(url);
      this.es.addEventListener("job", (event) => {
        this.esDelay = 1000;
        this.upsertJob(JSON.parse(event.data));
      });
      this.es.onerror = () => {
        this.es.close();
        // Reconnect with backoff and refetch jobs to fill any gap.
        setTimeout(() => {
          this.connectEvents();
          this.refreshJobs();
        }, this.esDelay);
        this.esDelay = Math.min(this.esDelay * 2, 30000);
      };
    },

    async openSettings() {
      try {
        this.settings = await this.api("/api/settings");
        this.draft = {
          output_format: this.settings.output_format,
          bitrate: this.settings.bitrate,
          inbox: this.settings.inbox,
          new_password: "",
        };
        await this.refreshHealth();
        this.settingsOpen = true;
      } catch (err) {
        if (err.message !== "password required") this.showToast("Cannot load settings: " + err.message);
      }
    },

    async saveSettings() {
      const update = {
        output_format: this.draft.output_format,
        bitrate: this.draft.bitrate,
        inbox: this.draft.inbox,
      };
      if (this.draft.new_password) update.password = this.draft.new_password;
      try {
        this.settings = await this.api("/api/settings", {
          method: "PUT",
          body: JSON.stringify(update),
        });
        if (this.draft.new_password) {
          this.password = this.draft.new_password;
          localStorage.setItem(LS_PASSWORD, this.password);
          this.connectEvents();
        }
        this.settingsOpen = false;
        this.showToast("Settings saved");
        this.refreshHealth();
      } catch (err) {
        if (err.message !== "password required") this.showToast("Save failed: " + err.message);
      }
    },

    saveLayout() {
      localStorage.setItem(LS_LAYOUT, this.layout);
    },

    submitPassword() {
      this.password = this.passwordInput;
      localStorage.setItem(LS_PASSWORD, this.password);
      this.passwordInput = "";
      this.passwordNeeded = false;
      this.connectEvents();
      this.refreshJobs();
    },
  },

  mounted() {
    const media = window.matchMedia(MOBILE_QUERY);
    media.addEventListener("change", (event) => { this.mediaMobile = event.matches; });
    this.refreshHealth();
    this.refreshJobs();
    this.connectEvents();
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js");
    }
  },
}).mount("#app");
