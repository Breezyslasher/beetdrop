/* Beetdrop UI logic. Vue 3 global build, no build step.
   The layout setting is a client preference and lives in localStorage;
   everything else round-trips through the API. */

const { createApp } = Vue;

const LS_LAYOUT = "beetdrop.layout";
// The Workbench breakpoint: below this the sidebar becomes a bottom tab
// bar and the queue rail becomes the strip above it.
const MOBILE_QUERY = "(max-width: 900px)";

// Material Design Icons, as path data on a 24x24 viewBox - copied
// verbatim from @mdi/svg 7.4.47 (magnify, playlist-music, album,
// chart-bar, cog, wrench). MDI shapes are filled, not stroked, so the
// <svg> that draws them sets fill and no stroke.
//
// Inlined rather than linked: the app is one container with no build
// step and no CDN, and a webfont would be a second thing to cache and a
// row of empty boxes when it did not arrive.
const ICONS = {
  search: "M9.5,3A6.5,6.5 0 0,1 16,9.5C16,11.11 15.41,12.59 14.44,13.73L14.71,14H15.5L20.5,19L19,20.5L14,15.5V14.71L13.73,14.44C12.59,15.41 11.11,16 9.5,16A6.5,6.5 0 0,1 3,9.5A6.5,6.5 0 0,1 9.5,3M9.5,5C7,5 5,7 5,9.5C5,12 7,14 9.5,14C12,14 14,12 14,9.5C14,7 12,5 9.5,5Z",
  queue: "M15,6H3V8H15V6M15,10H3V12H15V10M3,16H11V14H3V16M17,6V14.18C16.69,14.07 16.35,14 16,14A3,3 0 0,0 13,17A3,3 0 0,0 16,20A3,3 0 0,0 19,17V8H22V6H17Z",
  library: "M12,11A1,1 0 0,0 11,12A1,1 0 0,0 12,13A1,1 0 0,0 13,12A1,1 0 0,0 12,11M12,16.5C9.5,16.5 7.5,14.5 7.5,12C7.5,9.5 9.5,7.5 12,7.5C14.5,7.5 16.5,9.5 16.5,12C16.5,14.5 14.5,16.5 12,16.5M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
  stats: "M22,21H2V3H4V19H6V10H10V19H12V6H16V19H18V14H22V21Z",
  settings: "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z",
  repair: "M22.7,19L13.6,9.9C14.5,7.6 14,4.9 12.1,3C10.1,1 7.1,0.6 4.7,1.7L9,6L6,9L1.6,4.7C0.4,7.1 0.9,10.1 2.9,12.1C4.8,14 7.5,14.5 9.8,13.6L18.9,22.7C19.3,23.1 19.9,23.1 20.3,22.7L22.6,20.4C23.1,20 23.1,19.3 22.7,19Z",
};

const app = createApp({
  data() {
    return {
      query: "",
      searchType: "songs",
      grabFormat: "",
      // Song grabs also fetch the music video and file it beside the
      // track for Plex. A per-device preference, like the layout.
      grabWithVideo: (() => {
        try { return localStorage.getItem("beetdrop.withVideo") === "1"; }
        catch (err) { return false; }
      })(),
      results: [],
      resultsType: "songs",
      searching: false,
      searched: false,
      grabbing: {},
      updatingYtdlp: false,
      fetchingToken: false,
      scanningLyrics: false,
      checkingLyrics: false,
      lyricsStats: null,
      reviews: [],
      reviewTotal: 0,
      decidingReview: "",
      clearingReviews: false,
      unmatched: [],
      unmatchedTotal: 0,
      loadingUnmatched: false,
      lyricsWordByWord: true,
      unverified: [],
      unverifiedTotal: 0,
      loadingUnverified: false,
      fixingMatch: "",
      libraryQuery: "",

      // Workbench shell: one ref is the whole router. The views are
      // search / queue / library / stats / repair / settings.
      view: "search",
      libraryItems: [],
      libraryTotal: 0,
      libraryCounts: {},
      libraryFormats: {},
      libraryFilter: "",
      libraryFilterText: "",
      librarySort: "added",
      loadingLibrary: false,
      selectedAlbum: null,
      // Albums whose cover.jpg would not load. Falling back to the
      // stripe placeholder beats a broken-image icon per row.
      coverBroken: {},
      albumTracks: [],
      stats: null,
      loadingStats: false,
      taggingSources: false,
      notifyOnDone: localStorage.getItem("beetdrop.notify") === "1",
      testingApple: false,
      testingPlex: false,
      plexStatus: null,
      // A Plex PIN sign-in in progress. The account token never reaches
      // the page; it only learns the code, then the servers.
      plexPin: { id: "", code: "", state: "", servers: null, busy: false, error: "" },
      plexPinTimer: null,
      appleStatus: "",
      appleOk: false,
      appleId: "",
      applePassword: "",
      appleCode: "",
      appleFlowId: "",
      signingInApple: false,

      jobs: [],

      settings: null,
      health: null,
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
    icons() {
      // The template draws icons by name; the paths themselves are a
      // constant and never change, so they are not reactive state.
      return ICONS;
    },
    navItems() {
      return [
        { view: "search", label: "Search" },
        { view: "queue", label: "Queue" },
        { view: "library", label: "Library" },
        { view: "stats", label: "Stats" },
        { view: "repair", label: "Repair" },
        { view: "settings", label: "Settings" },
      ];
    },
    tabItems() {
      // Repair is absent by design: it is reached from Library and from a
      // job card, and five items is what fits a phone without shrinking
      // the tap targets.
      return [
        { view: "search", label: "Search", icon: ICONS.search },
        { view: "queue", label: "Queue", icon: ICONS.queue },
        { view: "library", label: "Library", icon: ICONS.library },
        { view: "stats", label: "Stats", icon: ICONS.stats },
        { view: "settings", label: "Settings", icon: ICONS.settings },
      ];
    },
    pageTitle() {
      const item = this.navItems.find((entry) => entry.view === this.view);
      return item ? item.label : "Beetdrop";
    },
    railVisible() {
      // The rail is context beside the work, so it belongs where a grab
      // is being started or a library is being looked through. On the
      // Queue screen it would be the same list twice; on Stats and
      // Settings the page wants the width.
      return !this.mediaMobile && this.layout !== "mobile"
        && (this.view === "search" || this.view === "library");
    },
    untaggedCount() {
      const rows = (this.stats && this.stats.lyrics.by_source) || [];
      const found = rows.find((row) => row.source === "untagged");
      return found ? found.count : 0;
    },
    stripJob() {
      // The phone strip shows one job: whatever is running, or the most
      // recent one if nothing is.
      const running = this.sortedJobs.find(
        (j) => !["done", "failed", "cancelled"].includes(j.stage));
      return running || this.sortedJobs[0] || null;
    },
    libraryChips() {
      const counts = this.libraryCounts;
      const chips = [
        { key: "", label: "All", count: counts.all != null ? counts.all : null },
        { key: "missing_lyrics", label: "Missing lyrics", count: counts.missing_lyrics },
        { key: "line_only", label: "Line-level only", count: counts.line_only },
        { key: "unverified", label: "Unverified", count: counts.unverified, warn: true },
        { key: "incomplete", label: "Gaps in numbering", count: counts.incomplete, warn: true },
        { key: "junk", label: "Junk lyrics", count: counts.junk },
        { key: "backwards", label: "Backwards word timing", count: counts.backwards, warn: true },
        { key: "crowded", label: "Crowded word timing", count: counts.crowded, warn: true },
      ];
      Object.keys(this.libraryFormats || {}).forEach((ext) => {
        chips.push({ key: "format:" + ext, label: ext, count: this.libraryFormats[ext] });
      });
      return chips.filter((chip) => chip.count == null || chip.count > 0
                                    || chip.key === "" || chip.key === this.libraryFilter);
    },
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

    goTo(view) {
      this.view = view;
      // Each destination loads itself the first time it is opened, so
      // nothing is fetched for a screen nobody looked at.
      if (view === "settings" && !this.settings) this.openSettings();
      if (view === "library" && !this.libraryItems.length) this.loadLibrary();
      if (view === "stats" && !this.stats) this.loadStats();
      if (view === "repair" && !this.unverified.length) this.loadUnverified();
      // The work area is one scroller shared by every screen; without
      // this, opening Settings from halfway down Library starts halfway
      // down Settings.
      this.$nextTick(() => {
        const area = document.querySelector(".workarea");
        if (area) area.scrollTop = 0;
      });
    },

    jobTone(job) {
      if (job.stage === "failed" || job.inbox_state === "unverified") {
        return "needs-decision";
      }
      return ["done", "cancelled"].includes(job.stage) ? "settled" : "";
    },

    navBadge(view) {
      if (view === "queue") return this.activeJobs.length || "";
      if (view === "repair") return this.unverifiedTotal || "";
      if (view === "stats" && this.stats) {
        const l = this.stats.lyrics;
        const total = l.word + l.line + l.junk + l.none;
        return total ? Math.round(100 * (l.word + l.line) / total) + "%" : "";
      }
      return "";
    },

    navBadgeTone(view) {
      if (view === "queue") return "good";
      if (view === "repair") return "warn";
      return "";
    },

    fmtBytes(bytes) {
      if (!bytes) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = bytes;
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
      return (value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1))
        + " " + units[unit];
    },

    fmtWhen(seconds) {
      if (!seconds) return "-";
      const ago = Date.now() / 1000 - seconds;
      if (ago < 90) return "just now";
      if (ago < 5400) return Math.round(ago / 60) + "m ago";
      if (ago < 172800) return Math.round(ago / 3600) + "h ago";
      return Math.round(ago / 86400) + "d ago";
    },

    async loadLibrary() {
      this.loadingLibrary = true;
      this.selectedAlbum = null;
      this.coverBroken = {};
      try {
        const query = "?filter=" + encodeURIComponent(this.libraryFilter)
          + "&sort=" + encodeURIComponent(this.librarySort)
          + "&q=" + encodeURIComponent(this.libraryFilterText);
        const body = await this.api("/api/library" + query);
        this.libraryItems = body.items || [];
        this.libraryTotal = body.total || 0;
        const counts = await this.api("/api/library/counts");
        this.libraryCounts = counts.counts || {};
        this.libraryFormats = counts.formats || {};
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not read the library: " + err.message);
        }
      } finally {
        this.loadingLibrary = false;
      }
    },

    coverUrl(album) {
      // The album's mtime rides in the query so a replaced cover comes
      // back under a new URL; the response is then cached for a week
      // rather than revalidated once per row on every visit.
      return "/api/library/album/" + encodeURIComponent(album.id)
        + "/cover?v=" + Math.round(album.added_at || 0);
    },

    albumLyricState(album) {
      const l = album.lyrics || {};
      if (l.none && !l.word && !l.line) return "none";
      if (l.word && !l.line && !l.none) return "word";
      if (l.junk) return "junk";
      return l.word ? "word" : (l.line ? "line" : "none");
    },

    async selectAlbum(album) {
      if (this.selectedAlbum && this.selectedAlbum.id === album.id) {
        this.selectedAlbum = null;
        this.albumTracks = [];
        return;
      }
      this.selectedAlbum = album;
      this.albumTracks = [];
      try {
        const body = await this.api("/api/library/album/" + encodeURIComponent(album.id));
        this.albumTracks = body.tracks || [];
      } catch (err) {
        if (err.message !== "password required") this.showToast(err.message);
      }
    },

    fixFromAlbum(album) {
      // Straight into the repair flow with the album already searched
      // for, rather than making a person retype what they just clicked.
      this.libraryQuery = album.album;
      this.goTo("repair");
      this.searchLibrary();
    },

    async copyPath(path) {
      try {
        await navigator.clipboard.writeText(path);
        this.showToast("Path copied");
      } catch (err) {
        this.showToast(path);
      }
    },

    async loadStats() {
      this.loadingStats = true;
      try {
        this.stats = await this.api("/api/stats");
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not read stats: " + err.message);
        }
      } finally {
        this.loadingStats = false;
      }
    },

    async tagSources() {
      this.taggingSources = true;
      try {
        const found = await this.api("/api/lyrics/tag-sources",
                                     { method: "POST" });
        this.showToast(found.tagged
          ? "Tagged " + found.tagged + " as Apple; " + found.unknowable
            + " line-level left untagged"
          : "Nothing new could be established from the files themselves");
        // The numbers on this screen came from before the rewrite.
        await this.loadStats();
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not tag them: " + err.message);
        }
      } finally {
        this.taggingSources = false;
      }
    },

    sourceName(source) {
      // The tag stores the internal name; these are what the settings
      // page calls the same three sources.
      return {apple: "Apple Music", lrclib: "LRCLIB",
              musixmatch: "Musixmatch", untagged: "untagged"}[source] || source;
    },

    sourceShare(row) {
      const rows = (this.stats && this.stats.lyrics.by_source) || [];
      const most = rows.reduce((top, one) => Math.max(top, one.count), 0);
      return most ? (100 * row.count / most) : 0;
    },

    lyricShare(kind) {
      if (!this.stats) return 0;
      const l = this.stats.lyrics;
      const total = l.word + l.line + l.junk + l.none;
      return total ? (100 * l[kind] / total) : 0;
    },

    formatShare(row) {
      if (!this.stats || !this.stats.tracks) return 0;
      return 100 * row.count / this.stats.tracks;
    },

    activityHeight(count) {
      if (!count) return 0;
      const peak = Math.max(1, ...this.stats.reliability.activity.map(
        (d) => d.audio + d.video + d.failed));
      return Math.max(2, Math.round(96 * count / peak));
    },

    verdictFor(result) {
      // The third line on a result row, and the point of the redesign:
      // say what is questionable *before* the grab rather than after it
      // has been filed. Nothing is invented - when there is nothing to
      // say the line is omitted rather than padded with filler.
      const title = (result.raw_title || result.title || "").toLowerCase();
      const qualifier = /\b(live|remix|extended|sped ?up|slowed|cover|karaoke|instrumental)\b/;
      if (qualifier.test(title)) {
        return { text: "Live or remix qualifier - likely filed to _review",
                 warn: false };
      }
      const seconds = result.duration_seconds;
      if (seconds && seconds > 15 * 60) {
        return { text: "Over 15 minutes - probably a mix, not a single track",
                 warn: true };
      }
      if (seconds && seconds < 45) {
        return { text: "Under 45 seconds - probably a clip", warn: true };
      }
      return null;
    },

    async requestNotifications() {
      if (!("Notification" in window)) {
        this.showToast("This browser has no notifications");
        return;
      }
      const granted = await Notification.requestPermission();
      this.notifyOnDone = granted === "granted";
      localStorage.setItem("beetdrop.notify", this.notifyOnDone ? "1" : "0");
      if (!this.notifyOnDone) this.showToast("Notifications not permitted");
    },

    notifyFiled(job) {
      // Fired from the job stream when a grab finishes. Falls back to the
      // usual toast when permission was never granted, so the outcome is
      // never only in a notification the browser refused to show.
      const where = job.inbox_path || "your library";
      if (!this.notifyOnDone || !("Notification" in window)
          || Notification.permission !== "granted") {
        return;
      }
      try {
        new Notification(job.title || "Grab finished", { body: "Filed to " + where });
      } catch (err) { /* some browsers refuse outside a service worker */ }
    },

    hideImage(event) {
      // Plenty of releases have no cover in the archive, and Apple's art
      // occasionally 404s too. A broken-image icon is worse than none.
      if (event && event.target) event.target.style.display = "none";
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
        if (this.searchType === "lyrics") {
          // A different catalogue entirely: Apple, not YouTube Music, and
          // nothing here is downloaded into the library.
          const found = await this.api(
            "/api/lyrics/search?q=" + encodeURIComponent(q) + "&limit=15");
          this.results = (found.results || []).map((song) =>
            Object.assign({}, song, { lrc: "", loading: false, gotWords: false }));
          this.resultsType = "lyrics";
          this.searched = true;
          return;
        }
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

    lyricsFileName(song) {
      const name = [song.artist, song.title].filter(Boolean).join(" - ");
      return (name || "lyrics").replace(/[\\/:*?"<>|]+/g, "_") + ".lrc";
    },

    lyricsDownloadUrl(song) {
      // A plain link, served with Content-Disposition: a download started
      // by script is what mobile browsers are least reliable about, and
      // the session cookie rides along on its own.
      return "/api/lyrics/download?song_id=" + encodeURIComponent(song.id)
        + "&word=" + (this.lyricsWordByWord ? "true" : "false")
        + "&name=" + encodeURIComponent(this.lyricsFileName(song));
    },

    async previewLyrics(song) {
      if (song.lrc) { song.lrc = ""; return; }
      song.loading = true;
      try {
        const body = await this.api(
          "/api/lyrics/preview?song_id=" + encodeURIComponent(song.id)
          + "&word=" + (this.lyricsWordByWord ? "true" : "false"));
        song.lrc = body.lrc || "";
        song.gotWords = !!body.word_level;
      } catch (err) {
        if (err.message !== "password required") this.showToast(err.message);
      } finally {
        song.loading = false;
      }
    },

    async grab(result, kind, force) {
      const id = kind === "album" ? result.browse_id : result.video_id;
      this.grabbing[id] = true;
      const payload = { video_id: id, kind: kind };
      if (this.grabFormat) payload.format = this.grabFormat;
      if (kind === "track" && this.grabWithVideo) payload.with_video = true;
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
          this.notifyFiled(job);
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
      // Settings is a destination in the shell now, not a sheet floated
      // over whatever was underneath it.
      this.view = "settings";
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
          apple_token: "",
          apple_storefront: this.settings.apple_storefront || "us",
          word_lyrics: !!this.settings.word_lyrics,
          video_root: this.settings.video_root,
          video_max_height: this.settings.video_max_height != null
            ? this.settings.video_max_height : 1080,
          cookies: "",
          new_password: "",
          plex_url: this.settings.plex_url || "",
          plex_token: "",
          plex_music_path: this.settings.plex_music_path || "",
        };
        this.plexStatus = null;
        await this.refreshHealth();
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
        apple_storefront: this.draft.apple_storefront,
        word_lyrics: !!this.draft.word_lyrics,
        video_max_height: Number(this.draft.video_max_height),
        plex_url: (this.draft.plex_url || "").trim(),
        plex_music_path: (this.draft.plex_music_path || "").trim(),
      };
      // Only send the library paths when they are editable (not env-locked).
      if (this.settings && !this.settings.music_root_locked) {
        update.music_root = this.draft.music_root;
      }
      if (this.settings && !this.settings.video_root_locked) {
        update.video_root = this.draft.video_root;
      }
      // Only send the Apple token when one was typed; the field is left
      // blank on open so a saved token is never echoed back to the browser.
      if (this.draft.apple_token) update.apple_token = this.draft.apple_token;
      // Same for the Plex token: it is the whole Plex account.
      if (this.draft.plex_token) update.plex_token = this.draft.plex_token;
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
        this.showToast("Settings saved");
        this.refreshHealth();
        return true;
      } catch (err) {
        if (err.message !== "password required") this.showToast("Save failed: " + err.message);
        return false;
      }
    },

    async plexSignIn() {
      this.plexCancel();
      this.plexPin = { id: "", code: "", state: "", servers: null, busy: true, error: "" };
      try {
        const pin = await this.api("/api/plex/pin", { method: "POST" });
        this.plexPin.id = pin.id;
        this.plexPin.code = pin.code;
        this.plexPin.state = "waiting";
        const until = Date.now() + 1000 * (pin.expires_in || 900);
        // Every two seconds: quick enough that the servers appear as soon
        // as the code is entered, slow enough not to hammer plex.tv.
        this.plexPinTimer = setInterval(() => this.plexPoll(until), 2000);
      } catch (err) {
        if (err.message !== "password required") {
          this.plexPin.error = "Could not start a Plex sign-in: " + err.message;
        }
      } finally {
        this.plexPin.busy = false;
      }
    },

    async plexPoll(until) {
      if (!this.plexPin.id || this.plexPin.busy) return;
      if (Date.now() > until) {
        this.plexPin.state = "expired";
        this.plexStopPolling();
        return;
      }
      this.plexPin.busy = true;
      try {
        const found = await this.api("/api/plex/pin/" + encodeURIComponent(this.plexPin.id));
        this.plexPin.state = found.state;
        if (found.state === "linked") {
          this.plexStopPolling();
          this.plexPin.servers = found.servers || [];
        } else if (found.state === "expired" || found.state === "unknown") {
          this.plexPin.state = "expired";
          this.plexStopPolling();
        }
      } catch (err) {
        // One failed poll is a hiccup; the next one tries again. Only
        // say so, so a plex.tv outage is visible rather than silent.
        if (err.message !== "password required") {
          this.plexPin.error = "Checking with plex.tv: " + err.message;
        }
      } finally {
        this.plexPin.busy = false;
      }
    },

    async plexChoose(server) {
      this.plexPin.busy = true;
      this.plexPin.error = "";
      try {
        const result = await this.api(
          "/api/plex/pin/" + encodeURIComponent(this.plexPin.id) + "/server",
          { method: "POST", body: JSON.stringify({ server_id: server.id }) });
        this.plexStatus = result;
        this.settings = await this.api("/api/settings");
        this.draft.plex_url = this.settings.plex_url || "";
        this.plexPin = { id: "", code: "", state: "", servers: null, busy: false, error: "" };
        this.showToast("Signed in to Plex: " + (result.server || server.name));
      } catch (err) {
        if (err.message !== "password required") this.plexPin.error = err.message;
      } finally {
        this.plexPin.busy = false;
      }
    },

    plexStopPolling() {
      if (this.plexPinTimer) clearInterval(this.plexPinTimer);
      this.plexPinTimer = null;
    },

    plexCancel() {
      this.plexStopPolling();
      this.plexPin = { id: "", code: "", state: "", servers: null, busy: false, error: "" };
    },

    async plexSignOut() {
      if (!window.confirm("Stop asking Plex to scan after each grab?")) return;
      try {
        await this.api("/api/plex/sign-out", { method: "POST" });
        this.settings = await this.api("/api/settings");
        this.draft.plex_url = "";
        this.plexStatus = null;
        this.showToast("Signed out of Plex");
      } catch (err) {
        if (err.message !== "password required") this.showToast(err.message);
      }
    },

    async testPlex() {
      // Save first, so the test checks what is on the page rather than
      // what was saved before someone started typing.
      this.testingPlex = true;
      this.plexStatus = null;
      try {
        // A save that failed - a bad address, say - must not be followed
        // by a test of whatever was saved before, which would read as a
        // verdict on what is on the page.
        if (!(await this.saveSettings())) return;
        this.draft.plex_token = "";
        this.plexStatus = await this.api("/api/plex/test", { method: "POST" });
      } catch (err) {
        if (err.message !== "password required") {
          this.plexStatus = { ok: false, detail: err.message };
        }
      } finally {
        this.testingPlex = false;
      }
    },

    saveGrabWithVideo() {
      try {
        localStorage.setItem("beetdrop.withVideo", this.grabWithVideo ? "1" : "0");
      } catch (err) { /* a private window keeps it for this visit only */ }
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

    async loadReviews() {
      try {
        const body = await this.api("/api/lyrics/reviews");
        this.reviews = body.reviews || [];
        this.reviewTotal = body.total || 0;
      } catch (err) {
        if (err.message !== "password required") this.reviews = [];
      }
    },

    async chooseReview(item, songId) {
      this.decidingReview = item.path;
      try {
        const body = await this.api("/api/lyrics/reviews", {
          method: "POST",
          body: JSON.stringify({ path: item.path, song_id: songId }),
        });
        this.showToast(body.detail || "saved");
        this.reviews = this.reviews.filter((r) => r.path !== item.path);
        this.reviewTotal = Math.max(0, this.reviewTotal - 1);
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not save that: " + err.message);
        }
      } finally {
        this.decidingReview = "";
      }
    },

    async loadUnmatched() {
      this.loadingUnmatched = true;
      try {
        const body = await this.api("/api/lyrics/unmatched?limit=50");
        this.unmatched = (body.tracks || []).map((track) => Object.assign(
          {}, track, {
            // What we would have searched for, editable: a track nothing
            // was found for usually has tags that are the reason.
            query: [track.artist, track.title].filter(Boolean).join(" "),
            results: null,
            searching: false,
          }));
        this.unmatchedTotal = body.total || 0;
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not list them: " + err.message);
        }
      } finally {
        this.loadingUnmatched = false;
      }
    },

    async searchLyricsFor(track) {
      if (!track.query.trim()) return;
      track.searching = true;
      try {
        const body = await this.api(
          "/api/lyrics/search?q=" + encodeURIComponent(track.query));
        track.results = body.results || [];
        if (!track.results.length) this.showToast("Apple returned nothing");
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Search failed: " + err.message);
        }
      } finally {
        track.searching = false;
      }
    },

    async useSearchResult(track, songId) {
      this.decidingReview = track.path;
      try {
        const body = await this.api("/api/lyrics/reviews", {
          method: "POST",
          body: JSON.stringify({ path: track.path, song_id: songId }),
        });
        this.showToast(body.detail || "saved");
        this.unmatched = this.unmatched.filter((t) => t.path !== track.path);
        this.unmatchedTotal = Math.max(0, this.unmatchedTotal - 1);
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not save that: " + err.message);
        }
      } finally {
        this.decidingReview = "";
      }
    },

    async loadUnverified() {
      this.loadingUnverified = true;
      try {
        const body = await this.api("/api/match/unverified?limit=100");
        this.unverified = this._asFixable(body.tracks);
        this.unverifiedTotal = body.total || 0;
        if (!this.unverified.length) this.showToast("Nothing in _review");
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not list them: " + err.message);
        }
      } finally {
        this.loadingUnverified = false;
      }
    },

    _asFixable(tracks) {
      return (tracks || []).map((track) => Object.assign({}, track, {
        query: track.title || track.name.replace(/\.[^.]+$/, ""),
        queryArtist: track.artist || "",
        candidates: null,
        searching: false,
        move: true,
      }));
    },

    async searchLibrary() {
      if (!this.libraryQuery.trim()) return;
      this.loadingUnverified = true;
      try {
        const body = await this.api(
          "/api/match/tracks?q=" + encodeURIComponent(this.libraryQuery));
        this.unverified = this._asFixable(body.tracks);
        this.unverifiedTotal = body.total || 0;
        if (!this.unverified.length) this.showToast("No track matched that");
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Search failed: " + err.message);
        }
      } finally {
        this.loadingUnverified = false;
      }
    },

    async findMatches(track) {
      if (!track.query.trim()) return;
      track.searching = true;
      try {
        const body = await this.api(
          "/api/match/candidates?title=" + encodeURIComponent(track.query)
          + "&artist=" + encodeURIComponent(track.queryArtist));
        track.candidates = body.candidates || [];
        if (!track.candidates.length) {
          this.showToast("MusicBrainz returned nothing for that");
        }
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Lookup failed: " + err.message);
        }
      } finally {
        track.searching = false;
      }
    },

    async applyMatch(track, candidate) {
      this.fixingMatch = track.path;
      try {
        const body = await this.api("/api/match/apply", {
          method: "POST",
          body: JSON.stringify({
            path: track.path, recording_id: candidate.id,
            title: track.query, artist: track.queryArtist,
            move: track.move,
          }),
        });
        this.showToast(body.detail || "filed");
        this.unverified = this.unverified.filter((t) => t.path !== track.path);
        this.unverifiedTotal = Math.max(0, this.unverifiedTotal - 1);
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not file it: " + err.message);
        }
      } finally {
        this.fixingMatch = "";
      }
    },

    async clearReviews() {
      // Only the list goes. Saying so matters: the obvious fear is that
      // this throws away lyrics or the decisions already made, and it
      // does neither.
      if (!window.confirm(
          "Forget all " + this.reviewTotal + " track(s) waiting for a "
          + "decision?\n\nNo lyrics are deleted and choices you have "
          + "already made are kept. Tracks still refused will come back "
          + "on the next scan.")) return;
      this.clearingReviews = true;
      try {
        const body = await this.api("/api/lyrics/reviews", { method: "DELETE" });
        this.reviews = [];
        this.reviewTotal = 0;
        this.showToast("Cleared " + (body.removed || 0) + " from the review queue");
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Could not clear it: " + err.message);
        }
      } finally {
        this.clearingReviews = false;
      }
    },

    async checkLyrics() {
      this.checkingLyrics = true;
      this.loadReviews();
      try {
        this.lyricsStats = await this.api("/api/lyrics/stats");
      } catch (err) {
        if (err.message !== "password required") {
          this.showToast("Check failed: " + err.message);
        }
      } finally {
        this.checkingLyrics = false;
      }
    },

    async scanLyrics(refresh, upgrade, redoWords) {
      this.scanningLyrics = true;
      try {
        // upgrade was accepted here and then dropped: every button posted
        // a plain scan, so "Upgrade to word-by-word" silently ran the
        // fetch-missing pass instead. The server has always understood
        // the flag. Upgrade wins over refresh, as it does server-side.
        const query = redoWords ? "?redo_words=true"
          : upgrade ? "?upgrade=true"
            : refresh ? "?refresh=true" : "";
        const job = await this.api("/api/lyrics/scan" + query,
                                   { method: "POST" });
        this.upsertJob(job);
        this.goTo("queue");
        this.showToast(redoWords
          ? "Re-rendering every word-by-word sidecar"
          : upgrade
            ? "Upgrading existing lyrics to word-by-word"
            : refresh
              ? "Deleting bad lyrics and re-fetching"
              : "Library lyrics scan started");
      } catch (err) {
        if (err.message === "password required") return;
        this.showToast(err.status === 409
          ? "A library lyrics scan is already running"
          : "Scan failed: " + err.message);
      } finally {
        this.scanningLyrics = false;
      }
    },

    async appleSignIn() {
      this.signingInApple = true;
      this.appleStatus = "";
      try {
        const body = await this.api("/api/apple/signin", {
          method: "POST",
          body: JSON.stringify({
            apple_id: this.appleId, password: this.applePassword,
          }),
        });
        // Not needed past this point, so drop it immediately.
        this.applePassword = "";
        if (body.status === "needs_2fa") {
          this.appleFlowId = body.flow_id;
          this.appleStatus = body.detail || "Enter the code Apple sent you";
          return;
        }
        this.appleFlowId = "";
        this.appleOk = body.status === "ok";
        this.appleStatus = body.detail || "Signed in";
        this.settings = await this.api("/api/settings");
      } catch (err) {
        this.applePassword = "";
        if (err.message === "password required") return;
        this.appleOk = false;
        this.appleStatus = err.message;
      } finally {
        this.signingInApple = false;
      }
    },

    async appleVerify() {
      this.signingInApple = true;
      try {
        const body = await this.api("/api/apple/verify", {
          method: "POST",
          body: JSON.stringify({ flow_id: this.appleFlowId, code: this.appleCode }),
        });
        this.appleCode = "";
        this.appleFlowId = "";
        this.appleOk = body.status === "ok";
        this.appleStatus = body.detail || "Signed in";
        this.settings = await this.api("/api/settings");
      } catch (err) {
        if (err.message === "password required") return;
        this.appleOk = false;
        this.appleStatus = err.message;
      } finally {
        this.signingInApple = false;
      }
    },

    async appleSignOut() {
      try {
        const body = await this.api("/api/apple/signout", { method: "POST" });
        this.appleFlowId = "";
        this.appleOk = false;
        this.appleStatus = body.detail || "Signed out";
        this.settings = await this.api("/api/settings");
      } catch (err) {
        if (err.message !== "password required") this.appleStatus = err.message;
      }
    },

    async testAppleToken() {
      this.testingApple = true;
      this.appleStatus = "";
      try {
        const body = await this.api("/api/lyrics/apple-test", { method: "POST" });
        this.appleOk = !!body.ok;
        this.appleStatus = body.detail || (body.ok ? "Working" : "Not working");
      } catch (err) {
        if (err.message === "password required") return;
        this.appleOk = false;
        this.appleStatus = "Test failed: " + err.message;
      } finally {
        this.testingApple = false;
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

    // How much room the phone dock takes is not a constant: the queue
    // strip comes and goes. Measure it instead of guessing, so the last
    // row of a list is never left underneath the tab bar.
    const dock = this.$refs.dock;
    if (dock && window.ResizeObserver) {
      const watch = new ResizeObserver(() => {
        document.documentElement.style.setProperty(
          "--dock", dock.offsetHeight + "px");
      });
      watch.observe(dock);
    }

    this.refreshHealth();
    this.refreshJobs();
    this.connectEvents();
    if ("serviceWorker" in navigator) {
      // A new worker calls skipWaiting()/clients.claim() and takes over at
      // once, but the page carries on running whatever app.js it already
      // loaded - so a fresh build only appeared after a manual hard
      // refresh. Reload when control changes, which is the moment the new
      // version is actually live.
      //
      // Only when there was already a controller: on a first install
      // clients.claim() also fires controllerchange, and nothing is stale
      // then, so reloading would just be a pointless flash.
      // Tracked at event time, not at mount: on a first visit the worker is
      // not controlling yet, so a flag captured here would be false forever
      // and every later update would be ignored.
      let sawController = !!navigator.serviceWorker.controller;
      let reloading = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (!sawController) {
          // First claim of this page - it is already the newest build.
          sawController = true;
          return;
        }
        if (reloading) return;
        reloading = true;
        window.location.reload();
      });
      // updateViaCache "none" keeps the browser's HTTP cache off sw.js
      // itself, so a new worker is always noticed.
      navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" })
        .then((registration) => {
          registration.update();
          // An installed PWA can sit backgrounded for days; check again
          // whenever it comes back to the foreground.
          document.addEventListener("visibilitychange", () => {
            if (!document.hidden) registration.update();
          });
        })
        .catch(() => { /* no service worker is not fatal */ });
    }
  },
}).mount("#app");

// A handle on the running app. Useful from the browser console on a
// self-hosted box - "why did that row not flag?" is answerable with
// beetdrop.verdictFor({...}) - and it is what the page tests drive,
// rather than reaching into Vue's internals, which the production build
// does not promise to keep.
window.beetdrop = app;
