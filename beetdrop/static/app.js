/* Beetdrop UI logic. Vue 3 global build, no build step.
   The layout setting is a client preference and lives in localStorage;
   everything else round-trips through the API. */

const { createApp } = Vue;

const LS_LAYOUT = "beetdrop.layout";
const MOBILE_QUERY = "(max-width: 700px)";

createApp({
  data() {
    return {
      query: "",
      searchType: "songs",
      grabFormat: "",
      results: [],
      resultsType: "songs",
      searching: false,
      searched: false,
      grabbing: {},
      updatingYtdlp: false,
      fetchingToken: false,

      jobs: [],
      queueOpen: false,

      settings: null,
      health: null,
      settingsOpen: false,
      draft: {},

      passwordNeeded: false,
      passwordInput: "",
      passwordError: "",

      layout: localStorage.getItem(LS_LAYOUT)
        || localStorage.getItem("trackpull.layout") || "auto",
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
        ? "Server ok, library writable"
        : "Problem: " + (this.health.library_problem || "server degraded");
    },
  },

  methods: {
    async api(path, options = {}) {
      const headers = Object.assign({}, options.headers);
      if (options.body) headers["Content-Type"] = "application/json";
      // Auth rides an HttpOnly session cookie set by /api/login; the
      // password itself is never kept in the browser.
      const response = await fetch(path, Object.assign({}, options, { headers }));
      if (response.status === 401) {
        this.passwordNeeded = true;
        throw new Error("password required");
      }
      if (!response.ok) {
        let detail = response.statusText;
        try { detail = (await response.json()).detail || detail; } catch (e) { /* not json */ }
        const err = new Error(typeof detail === "string" ? detail : (detail.message || "request failed"));
        err.status = response.status;
        err.detail = detail;
        throw err;
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

    setSearchType(type) {
      this.searchType = type;
      if (this.searched && this.query.trim()) this.search();
    },

    async search() {
      const q = this.query.trim();
      if (!q || this.searching) return;
      this.searching = true;
      try {
        const body = await this.api(
          "/api/search?q=" + encodeURIComponent(q) + "&type=" + this.searchType
        );
        this.results = body.results;
        this.resultsType = body.type || "songs";
        this.searched = true;
      } catch (err) {
        if (err.message !== "password required") this.showToast("Search failed: " + err.message);
      } finally {
        this.searching = false;
      }
    },

    async grab(result, kind, force) {
      const id = kind === "album" ? result.browse_id : result.video_id;
      this.grabbing[id] = true;
      const payload = { video_id: id, kind: kind };
      if (this.grabFormat) payload.format = this.grabFormat;
      if (force) payload.force = true;
      try {
        const job = await this.api("/api/grab", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        this.upsertJob(job);
        this.showToast("Queued: " + result.title);
      } catch (err) {
        delete this.grabbing[id];
        if (err.status === 409 && err.detail && err.detail.existing_job) {
          const existing = err.detail.existing_job;
          const when = new Date(existing.created_at * 1000).toLocaleString();
          if (window.confirm(err.detail.message + "\n(" +
              (existing.title || existing.id) + ", " + existing.stage + ", " + when +
              ")\n\nGrab it again anyway?")) {
            this.grab(result, kind, true);
          }
          return;
        }
        if (err.message !== "password required") this.showToast("Grab failed: " + err.message);
      }
    },

    async cancel(job) {
      try {
        await this.api("/api/jobs/" + job.id + "/cancel", { method: "POST" });
        this.showToast("Cancelling: " + (job.title || job.video_id));
      } catch (err) {
        if (err.message !== "password required") this.showToast("Cancel failed: " + err.message);
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
        if (previous.stage !== "done" && job.stage === "done") {
          this.showToast("Filed: " + (job.title || job.video_id));
        }
        if (previous.inbox_state !== "unverified" && job.inbox_state === "unverified") {
          this.showToast("No MusicBrainz match - in _review: " + (job.title || job.video_id));
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
      // Same-origin EventSource carries the session cookie by itself.
      this.es = new EventSource("/events");
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
          music_root: this.settings.music_root,
          output_format: this.settings.output_format,
          bitrate: this.settings.bitrate,
          concurrency: this.settings.concurrency,
          // Default-on unless the server explicitly says off, so a
          // partial/stale settings object never silently flips it.
          lyrics: this.settings.lyrics !== false,
          lyrics_provider: this.settings.lyrics_provider || "lrclib",
          video_root: this.settings.video_root,
          video_max_height: this.settings.video_max_height != null
            ? this.settings.video_max_height : 1080,
          cookies: "",
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
        concurrency: Number(this.draft.concurrency) || undefined,
        lyrics: !!this.draft.lyrics,
        lyrics_provider: this.draft.lyrics_provider,
        video_max_height: Number(this.draft.video_max_height),
      };
      // Only send the library paths when they are editable (not env-locked).
      if (this.settings && !this.settings.music_root_locked) {
        update.music_root = this.draft.music_root;
      }
      if (this.settings && !this.settings.video_root_locked) {
        update.video_root = this.draft.video_root;
      }
      if (this.draft.new_password) update.password = this.draft.new_password;
      if (this.draft.cookies && this.draft.cookies.trim()) update.cookies = this.draft.cookies;
      try {
        this.settings = await this.api("/api/settings", {
          method: "PUT",
          body: JSON.stringify(update),
        });
        if (this.draft.new_password) {
          // Changing the password invalidates every session including
          // this one; log straight back in with the new password.
          await this.api("/api/login", {
            method: "POST",
            body: JSON.stringify({ password: this.draft.new_password }),
          });
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

    async clearCookies() {
      try {
        this.settings = await this.api("/api/settings", {
          method: "PUT",
          body: JSON.stringify({ cookies: "" }),
        });
        this.showToast("Cookies cleared");
      } catch (err) {
        if (err.message !== "password required") this.showToast("Clear failed: " + err.message);
      }
    },

    async fetchMxmToken() {
      this.fetchingToken = true;
      try {
        await this.api("/api/lyrics/musixmatch-token", { method: "POST" });
        this.settings = await this.api("/api/settings");
        this.showToast("Musixmatch token saved");
      } catch (err) {
        if (err.message !== "password required") this.showToast("Token fetch failed: " + err.message);
      } finally {
        this.fetchingToken = false;
      }
    },

    async updateYtdlp() {
      this.updatingYtdlp = true;
      try {
        const body = await this.api("/api/ytdlp/update", { method: "POST" });
        if (body.installed_version !== body.loaded_version) {
          this.showToast("yt-dlp updated to " + body.installed_version +
            " and active now");
        } else {
          this.showToast("yt-dlp is already up to date (" + body.loaded_version + ")");
        }
        // The settings panel shows the resolved version; refresh it.
        this.settings = await this.api("/api/settings");
      } catch (err) {
        if (err.message !== "password required") this.showToast("Update failed: " + err.message);
      } finally {
        this.updatingYtdlp = false;
      }
    },

    async submitPassword() {
      this.passwordError = "";
      try {
        const response = await fetch("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: this.passwordInput }),
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          this.passwordError = body.detail || ("login failed (" + response.status + ")");
          return;
        }
        this.passwordInput = "";
        this.passwordNeeded = false;
        this.connectEvents();
        this.refreshJobs();
      } catch (err) {
        this.passwordError = "login failed: " + err.message;
      }
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
