document.addEventListener("alpine:init", () => {
  Alpine.data("inventoryApp", () => ({
    devices: [],
    networks: {},
    defaultTargetOs: "5.16.0",
    networksOpen: false,
    networkRows: [],
    networkError: "",
    revision: "",
    query: "",
    sortKey: "hostname",
    sortDirection: "asc",
    loading: true,
    saving: false,
    error: "",
    message: "",
    formOpen: false,
    formError: "",
    editingHostname: null,
    form: { hostname: "", mgmt_ip: "", eth0_mac: "", serial: "", network: "", target_os: "" },

    progress: {},
    progressLoaded: false,
    progressBusy: false,
    progressError: "",
    progressUpdated: null,
    progressTimer: null,
    historyHostname: null,
    historyEvents: [],
    historyLoading: false,
    historyError: "",
    historyOffset: 0,
    historyMore: false,
    historyGeneration: 0,

    init() {
      this.refreshProgress();
      this.progressTimer = setInterval(() => {
        if (!document.hidden) this.refreshProgress();
      }, 5000);
    },

    destroy() {
      clearInterval(this.progressTimer);
      this.closeHistory();
    },

    progressFor(hostname) {
      return Object.hasOwn(this.progress, hostname) ? this.progress[hostname] : null;
    },

    stageLabel(stage) {
      return {
        upgrade_payload: "Upgrade script requested", reachability: "Checking server connection",
        os_install: "Installing OS", reboot: "Reboot", configuration_payload: "Configuration script requested",
        prepare: "Preparing switch", config_download: "Downloading configuration",
        config_apply: "Applying configuration", config_save: "Saving configuration",
        provisioning: "Configuration saved",
      }[stage] || stage;
    },

    progressLabel(status) {
      if (!status) {
        if (this.progressLoaded) return "No reports yet";
        return this.progressError ? "Unavailable" : "Loading…";
      }
      if (status.result === "failed") return "Failed";
      if (status.stage === "provisioning" && status.result === "succeeded") return "Completed";
      if (status.stage === "reboot") return "Reboot requested";
      if (status.stage.endsWith("_payload")) return "Awaiting switch reports";
      return "In progress";
    },

    progressClass(status) {
      if (!status) return "neutral";
      if (status.result === "failed") return "failed";
      if (status.stage === "provisioning" && status.result === "succeeded") return "ready";
      return "running";
    },

    formatTime(value) {
      return value ? new Date(value).toLocaleString() : "—";
    },

    async refreshProgress() {
      if (this.progressBusy) return;
      this.progressBusy = true;
      try {
        const statuses = [];
        let offset = 0;
        while (true) {
          const body = await this.api(`/api/v1/provisioning?limit=1000&offset=${offset}`, {
            signal: AbortSignal.timeout(8000),
          });
          statuses.push(...body.devices);
          if (body.devices.length < 1000) break;
          offset += body.devices.length;
        }
        this.progress = Object.fromEntries(statuses.map(status => [status.hostname, status]));
        this.progressLoaded = true;
        this.progressUpdated = new Date().toISOString();
        this.progressError = "";
      } catch (error) {
        this.progressError = `Progress unavailable: ${error.message}. ${this.progressLoaded ? "Showing last received statuses." : "No progress status could be loaded."}`;
      } finally {
        this.progressBusy = false;
      }
    },

    openHistory(device) {
      this.historyHostname = device.hostname;
      return this.loadHistory(true);
    },

    closeHistory() {
      this.historyHostname = null;
      this.historyGeneration += 1;
      this.historyLoading = false;
    },

    async loadHistory(reset = false) {
      if (!this.historyHostname || (this.historyLoading && !reset)) return;
      const hostname = this.historyHostname;
      const generation = ++this.historyGeneration;
      if (reset) {
        this.historyEvents = [];
        this.historyOffset = 0;
        this.historyMore = false;
      }
      this.historyLoading = true;
      this.historyError = "";
      try {
        const body = await this.api(`/api/v1/provisioning/${encodeURIComponent(hostname)}/events?limit=100&offset=${this.historyOffset}`, {
          signal: AbortSignal.timeout(8000),
        });
        if (generation !== this.historyGeneration) return;
        const seen = new Set(this.historyEvents.map(event => event.id));
        this.historyEvents.push(...body.events.filter(event => !seen.has(event.id)));
        this.historyOffset += body.events.length;
        this.historyMore = body.events.length === 100;
      } catch (error) {
        if (generation === this.historyGeneration) this.historyError = error.message;
      } finally {
        if (generation === this.historyGeneration) this.historyLoading = false;
      }
    },

    sortBy(key) {
      this.sortDirection = this.sortKey === key && this.sortDirection === "asc" ? "desc" : "asc";
      this.sortKey = key;
    },

    sortValue(device) {
      return this.sortKey === "provisioning"
        ? this.progressLabel(this.progressFor(device.hostname))
        : device[this.sortKey] || "";
    },

    get filteredDevices() {
      const needle = this.query.trim().toLowerCase();
      const devices = this.devices.filter((device) =>
        !needle || [device.hostname, device.mgmt_ip, device.eth0_mac, device.serial]
          .filter(Boolean)
          .some((value) => value.toLowerCase().includes(needle)),
      );
      return devices.sort((left, right) => {
        const a = this.sortValue(left);
        const b = this.sortValue(right);
        if (!a && b) return 1;
        if (a && !b) return -1;
        const order = a.localeCompare(b, undefined, { numeric: this.sortKey !== "eth0_mac", sensitivity: "base" })
          || left.hostname.localeCompare(right.hostname, undefined, { numeric: true });
        return this.sortDirection === "asc" ? order : -order;
      });
    },

    headers(includeRevision = false) {
      const headers = { "Content-Type": "application/json" };
      if (includeRevision) headers["If-Match"] = `"${this.revision}"`;
      return headers;
    },

    async api(path, options = {}) {
      const response = await fetch(path, options);
      let body = null;
      try {
        body = await response.json();
      } catch (_) {
        body = null;
      }
      if (!response.ok) {
        const detail = body?.detail;
        const message = Array.isArray(detail)
          ? detail.map((item) => item.msg).join("; ")
          : detail || `Request failed (${response.status})`;
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return body;
    },

    async load() {
      this.loading = true;
      this.error = "";
      this.refreshProgress();
      try {
        const body = await this.api("/api/v1/devices", { headers: this.headers() });
        this.devices = body.devices;
        this.networks = body.networks || {};
        this.defaultTargetOs = body.settings?.target_os || "5.16.0";
        this.revision = body.revision;
      } catch (error) {
        this.error = error.message;
      } finally {
        this.loading = false;
      }
    },

    openCreate() {
      this.editingHostname = null;
      this.form = { hostname: "", mgmt_ip: "", eth0_mac: "", serial: "", network: "", target_os: "" };
      this.formError = "";
      this.formOpen = true;
      this.$nextTick(() => this.$refs.hostname.focus());
    },

    openEdit(device) {
      this.editingHostname = device.hostname;
      this.form = {
        hostname: device.hostname,
        mgmt_ip: device.mgmt_ip || "",
        eth0_mac: device.eth0_mac || "",
        serial: device.serial || "",
        network: device.network || "",
        target_os: device.target_os || "",
      };
      this.formError = "";
      this.formOpen = true;
      this.$nextTick(() => this.$refs.hostname.focus());
    },

    closeForm() {
      if (!this.saving) this.formOpen = false;
    },

    payload() {
      return {
        hostname: this.form.hostname,
        network: this.form.network || null,
        target_os: this.form.target_os || null,
        mgmt_ip: this.form.mgmt_ip || null,
        eth0_mac: this.form.eth0_mac || null,
        serial: this.form.serial || null,
      };
    },

    async save() {
      this.saving = true;
      this.formError = "";
      this.message = "";
      const editing = this.editingHostname !== null;
      const path = editing
        ? `/api/v1/devices/${encodeURIComponent(this.editingHostname)}`
        : "/api/v1/devices";
      try {
        const body = await this.api(path, {
          method: editing ? "PUT" : "POST",
          headers: this.headers(true),
          body: JSON.stringify(this.payload()),
        });
        this.revision = body.revision;
        this.formOpen = false;
        this.message = editing ? "Device updated." : "Device added.";
        await this.load();
      } catch (error) {
        if (error.status === 412) {
          this.formError = `${error.message} Your unsaved values are still shown.`;
        } else {
          this.formError = error.message;
        }
      } finally {
        this.saving = false;
      }
    },

    openNetworks() {
      this.networkRows = Object.entries(this.networks).map(([name, network]) => ({ name, ...network }));
      this.networkError = "";
      this.networksOpen = true;
    },

    async saveNetworks() {
      const networks = {};
      for (const row of this.networkRows) {
        const name = row.name.trim();
        if (!name || Object.hasOwn(networks, name)) {
          this.networkError = "Each network needs a unique name.";
          return;
        }
        Object.defineProperty(networks, name, {
          value: { subnet: row.subnet.trim(), gateway: row.gateway.trim() }, enumerable: true,
        });
      }
      this.saving = true;
      this.networkError = "";
      try {
        await this.api("/api/v1/networks", {
          method: "PUT", headers: this.headers(true), body: JSON.stringify(networks),
        });
        this.networksOpen = false;
        this.message = "Networks saved. Changes are queued for dnsmasq validation and restart; check dnsmasq logs for the result.";
        await this.load();
      } catch (error) {
        this.networkError = error.message;
      } finally {
        this.saving = false;
      }
    },

    async remove(device) {
      if (!window.confirm(`Delete ${device.hostname}? This also removes its DHCP reservation.`)) return;
      this.error = "";
      this.message = "";
      try {
        const body = await this.api(
          `/api/v1/devices/${encodeURIComponent(device.hostname)}`,
          { method: "DELETE", headers: this.headers(true) },
        );
        this.revision = body.revision;
        this.message = "Device deleted.";
        await this.load();
      } catch (error) {
        this.error = error.message;
        if (error.status === 412) await this.load();
      }
    },
  }));
});
