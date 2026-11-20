"use strict";
const byId = id => document.getElementById(id);
let tuners = [], servers = [], revision = "", pending = false, connected = false, searched = false;
let setupState = {scan: {state: "idle"}, candidates: []};
let serverSelectionFailed = false;
let lineupOrder = [], manuallySelected = new Set(), manuallyRemoved = new Set(), preferencesLoaded = false;
let inputDevices = [];
let channelStep = "select", selectionFrozen = false, drag = null;
const autoSelectedServers = new Set();
const activeStates = new Set(["starting", "pausing", "running", "stopping"]);
function needsRecovery() {
  return setupState.recovery_pending && !activeStates.has(setupState.scan.state);
}
function api(path, payload) {
  return receiverRequest(`/api/setup/${path}`, payload === undefined ? "GET" : "POST", payload,
                         new AbortController(), path === "status" ? 5000 : 30000);
}
function options(id, entries) {
  const previous = byId(id).value, key = JSON.stringify(entries);
  if (byId(id).dataset.options === key) return;
  byId(id).dataset.options = key;
  byId(id).replaceChildren(...entries.map(([value, text]) => {
    const option = document.createElement("option"); option.value = value; option.textContent = text; return option;
  }));
  if (entries.some(([value]) => value === previous)) byId(id).value = previous;
}
function controls() {
  const enigma = setupState.source === "enigma2" || window.sourceSetupKind === "enigma2";
  if (byId("setup-satip")) byId("setup-satip").hidden = enigma;
  if (setupState.source) window.sourceSetupKind = setupState.source;
  window.sourceSetupLocked = !!setupState.selection_locked;
  if (setupState.source === "enigma2" && byId("source-kind")) byId("source-kind").value = "enigma2";
  const busy = activeStates.has(setupState.scan.state), blocked = !connected || pending || setupState.completion_pending;
  const registering = setupState.registration?.state === "connecting";
  const ready = setupState.registration?.state === "ready" && byId("server").value === setupState.selected_server?.udn;
  byId("save-input").disabled = blocked || !byId("input-device").value;
  const input = inputDevices.find(d => d.id === byId("input-device").value);
  byId("save-input").hidden = !!(input?.keyboard || input?.remote);
  byId("save-preferences").disabled = blocked || !preferencesLoaded;
  byId("restore-config").disabled = blocked;
  byId("discover").disabled = blocked || busy || setupState.discovery?.state === "searching";
  byId("discover").hidden = !!setupState.selection_locked;
  byId("discovery-status").hidden = !!setupState.selection_locked;
  byId("manual-server").hidden = !!setupState.selection_locked;
  for (const id of ["server", "server-url", "add-server"])
    byId(id).disabled = blocked || busy || registering || setupState.selection_locked || needsRecovery();
  byId("retry-server").hidden = !serverSelectionFailed && setupState.registration?.state !== "error";
  byId("retry-server").disabled = blocked || busy || needsRecovery() || !byId("server").value;
  byId("server-field").hidden = !!setupState.selection_locked;
  byId("server-fixed").hidden = !setupState.selection_locked;
  byId("server-fixed").textContent = setupState.selected_server ?
    `Server: ${setupState.selected_server.name} — ${setupState.selected_server.address}. The selected server is kept while completing setup.` : "";
  byId("scan").disabled = blocked || busy || !ready || needsRecovery() || !tuners.length ||
    !setupState.scanfiles?.length;
  byId("setup-channels-restore").disabled = byId("scan").disabled;
  byId("cancel").hidden = !busy;
  byId("recover").hidden = !needsRecovery();
  byId("cancel").disabled = blocked || !busy || setupState.scan.state === "stopping";
  byId("recover").disabled = blocked || busy || !needsRecovery();
  const lineupBlocked = blocked || busy || needsRecovery() || !setupState.prepared ||
    (setupState.frontend_uuid && byId("tuner").value !== setupState.frontend_uuid);
  byId("confirm-favourites").disabled = lineupBlocked || channelStep !== "select" ||
    !byId("channels").querySelectorAll("input:checked").length;
  byId("finish").disabled = lineupBlocked || channelStep !== "sort" || !!drag || !lineupOrder.length;
  byId("change-selection").disabled = blocked || !!drag;
  byId("setup-search").hidden = byId("setup-selection").hidden = channelStep === "sort";
  byId("setup-sorting").hidden = channelStep !== "sort";
  for (const handle of byId("setup-favourites").querySelectorAll("button")) handle.disabled = lineupBlocked;
  byId("tuner").disabled = blocked || busy || !ready || needsRecovery();
  byId("tuner-hint").textContent = setupState.frontend_uuid && byId("tuner").value !== setupState.frontend_uuid ?
    "Click Scan to use the selected tuner. Channels already found will be kept." :
    "Choose the tuner to use for the next search. It can be changed while the search is stopped.";
}
function action(id, callback, event = "onclick") {
  byId(id)[event] = async () => {
    if (byId(id).disabled || pending) return;
    pending = true; byId("message").textContent = ""; controls();
    try { await callback(); }
    catch (error) { byId("message").textContent = error.message; }
    finally { pending = false; controls(); }
  };
}
action("discover", async () => {
  await api("discover", {});
  setupState.discovery = {...setupState.discovery, state: "searching"};
  byId("discovery-status").textContent = "Searching the local network…";
});
action("save-input", async () => {
  const data = await api("input", {id: byId("input-device").value});
  byId("input-status").textContent = data.message;
});
action("save-preferences", async () => {
  const data = await api("preferences", {hostname: byId("receiver-name").value, timezone: byId("receiver-timezone").value});
  byId("preferences-status").textContent = data.message;
});
action("add-server", async () => {
  const server = await api("server/add", {url: byId("server-url").value.trim()});
  servers = [...servers.filter(s => s.udn !== server.udn), server];
  renderServers(); byId("server").value = server.udn;
  await selectServer();
});
async function selectServer() {
  const server = servers.find(s => s.udn === byId("server").value);
  if (!server) throw new Error("Select a server first");
  serverSelectionFailed = false;
  setupState.registration = {state: "connecting"};
  tuners = []; options("tuner", []);
  byId("registration-status").textContent = "Connecting to the selected server…";
  try { await api("server/select", {url: server.url, udn: server.udn}); }
  catch (error) { serverSelectionFailed = true; throw error; }
}
action("server", selectServer, "onchange");
action("retry-server", selectServer);
function renderServers() {
  options("server", [["", "Choose a server…"], ...servers.map(s => [s.udn, `${s.name} — ${s.address} — ${s.tuners} DVB-S/S2 tuners`])]);
}
action("scan", async () => {
  const tuner = tuners.find(t => t.frontend_uuid === byId("tuner").value);
  if (!tuner) throw new Error("Discover and select a tuner first");
  await api("scan", {server_uuid: tuner.server_uuid, frontend_uuid: tuner.frontend_uuid});
  setupState.scan = {state: "starting"};
});
byId("setup-channels-restore").onclick = () => {
  if (pending || byId("setup-channels-restore").disabled) return;
  byId("setup-channels-file").value = ""; byId("setup-channels-file").click();
};
byId("setup-channels-file").onchange = async () => {
  const file = byId("setup-channels-file").files?.[0];
  if (!file || pending || byId("setup-channels-restore").disabled) return;
  const message = text => { byId("setup-channels-message").textContent = text; };
  if (!file.size || file.size > 32 * 1024**2) { message("Choose a non-empty channel backup up to 32 MiB."); return; }
  pending = true; controls();
  try {
    const tuner = tuners.find(t => t.frontend_uuid === byId("tuner").value);
    if (!tuner) throw new Error("Discover and select a tuner first");
    message("Preparing the selected tuner…");
    await api("channels/prepare", {server_uuid: tuner.server_uuid, frontend_uuid: tuner.frontend_uuid});
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
    message("Uploading and validating channel backup…");
    const response = await fetch(`/api/device/channels/upload/${id}`, {
      method: "POST", headers: {"X-Satip-Action": "1", "Content-Type": "application/octet-stream"},
      body: file, signal: AbortSignal.timeout(120000)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Channel backup validation failed");
    if (!window.confirm(`Restore ${result.available} channels and ${result.favourites} favourites from ${result.created_at}? Setup will finish using your selected tuner. Receiver settings and passwords stay unchanged.`)) {
      message("Restore cancelled. No channels imported. You can scan or choose another backup."); return;
    }
    await receiverRequest("/api/device", "POST", {action: "backup_channels_restore", args: {id}});
    message("Restoring channels and starting TV… The page reconnects automatically.");
    const deadline = Date.now() + 420000;
    while (Date.now() < deadline) {
      let status;
      try { status = await receiverRequest("/api/device", "POST", {action: "backup_status", args: {id}}, undefined, 10000); }
      catch (_) { /* The web service briefly stops while the archive is activated. */ }
      if (status?.state === "restored") { window.location.replace("/#status"); window.location.reload(); return; }
      if (["error", "missing"].includes(status?.state)) throw new Error(status.message || "Restore expired. Choose the backup again.");
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    throw new Error("Restore is taking longer than expected. Check receiver status before retrying.");
  } catch (error) { message(error.message); }
  finally { pending = false; byId("setup-channels-file").value = ""; controls(); }
};
for (const id of ["cancel", "recover"]) action(id, async () => {
  if (id === "cancel") {
    setupState.scan.state = "stopping";
    byId("status").textContent = "Stopping search and restoring tuner settings…";
    controls();
  }
  await api(id, {});
  byId("message").textContent = id === "cancel" ? "Search stopped. Discovered channels were kept." : "Tuner settings restored.";
});
action("confirm-favourites", async () => {
  const selected = [...byId("channels").querySelectorAll("input:checked")].map(input => input.value);
  lineupOrder = [...new Set([...lineupOrder, ...(setupState.starter || []), ...selected])].filter(id => selected.includes(id));
  manuallySelected = new Set(selected); selectionFrozen = true;
  channelStep = "sort"; renderFavourites(); controls();
  byId("sorting-heading").focus();
});
action("change-selection", async () => {
  channelStep = "select"; renderChannels(setupState); controls();
  byId("selection-heading").focus();
});
action("finish", async () => {
  const data = await api("finish", {services: [...lineupOrder]});
  setupState.completion_pending = true;
  byId("message").textContent = data.message;
});
action("restore-config", async () => {
  byId("restore-config-status").textContent = "Restoring the image template…";
  try {
    await api("config/restore", {});
    byId("restore-config-status").textContent = "Default satip-config.txt restored. Saved settings, credentials and channels are unchanged.";
  } catch (error) {
    byId("restore-config-status").textContent = "Restore was not confirmed. You can retry.";
    throw error;
  }
});
byId("channels").onchange = event => {
  const input = event.target;
  if (input.checked) { manuallySelected.add(input.value); manuallyRemoved.delete(input.value); }
  else { manuallyRemoved.add(input.value); manuallySelected.delete(input.value); }
  controls();
};
byId("tuner").onchange = controls;
byId("input-device").onchange = controls;
function renderChannels(data) {
  if (channelStep !== "select") return;
  const starter = selectionFrozen ? [] : data.starter || [];
  const selected = new Set([...starter.filter(id => !manuallyRemoved.has(id)), ...manuallySelected]);
  byId("channels").replaceChildren(...data.candidates.map(channel => {
    const row = document.createElement("p"), label = document.createElement("label"), input = document.createElement("input");
    input.type = "checkbox"; input.value = channel.service_uuid; input.checked = selected.has(input.value);
    label.append(input, document.createTextNode(channel.name)); row.append(label);
    return row;
  }));
}
function sortBlocked() {
  return channelStep !== "sort" || pending || !connected || setupState.completion_pending || needsRecovery();
}
function renderFavourites() {
  if (drag) return;
  const channels = new Map(setupState.candidates.map(c => [c.service_uuid, c]));
  byId("setup-favourites").replaceChildren(...lineupOrder.map((id, index) => {
    const row = document.createElement("li"), label = document.createElement("span"), handle = document.createElement("button");
    const name = channels.get(id)?.name || "Selected channel";
    row.dataset.service = id; label.textContent = `${index + 1}. ${name}`;
    handle.type = "button"; handle.textContent = "⋮⋮"; handle.className = "drag-handle";
    handle.setAttribute("aria-label", `Drag ${name} to reorder`);
    handle.onpointerdown = event => {
      if (event.button !== 0 || drag || sortBlocked()) return;
      const list = byId("setup-favourites");
      drag = {node: row, pointer: event.pointerId, startY: event.clientY, lastY: event.clientY, moved: false};
      list.setPointerCapture(event.pointerId);
      drag.timer = setInterval(() => {
        if (drag?.moved) moveDrag({pointerId: drag.pointer, clientY: drag.lastY, preventDefault() {}});
      }, 50);
      controls();
    };
    handle.onkeydown = event => {
      if (sortBlocked() || drag || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const target = index + (event.key === "ArrowUp" ? -1 : 1);
      if (target < 0 || target >= lineupOrder.length) return;
      [lineupOrder[index], lineupOrder[target]] = [lineupOrder[target], lineupOrder[index]];
      renderFavourites();
      byId("setup-favourites").children[target].querySelector("button").focus();
      byId("sort-status").textContent = `${name} moved to position ${target + 1}.`;
    };
    row.append(handle, label); return row;
  }));
  controls();
}
function moveDrag(event) {
  if (!drag || event.pointerId !== drag.pointer) return;
  drag.lastY = event.clientY;
  if (!drag.moved && Math.abs(event.clientY - drag.startY) < 5) return;
  drag.moved = true; drag.node.classList.add("dragging"); event.preventDefault();
  const list = byId("setup-favourites"), area = list.getBoundingClientRect();
  if (event.clientY < area.top + 35) list.scrollTop -= 12;
  else if (event.clientY > area.bottom - 35) list.scrollTop += 12;
  for (const target of list.children) {
    if (target === drag.node) continue;
    const bounds = target.getBoundingClientRect();
    if (event.clientY < bounds.top + bounds.height / 2) { list.insertBefore(drag.node, target); return; }
  }
  list.append(drag.node);
}
function endDrag(event, cancelled = false) {
  if (!drag || event.pointerId !== drag.pointer) return;
  const current = drag, list = byId("setup-favourites"); drag = null; clearInterval(current.timer);
  if (list.hasPointerCapture(current.pointer)) list.releasePointerCapture(current.pointer);
  if (!cancelled && current.moved) {
    lineupOrder = [...list.children].map(row => row.dataset.service);
    byId("sort-status").textContent = "Order updated. Save channels when you are ready.";
  }
  renderFavourites();
}
byId("setup-favourites").onpointermove = moveDrag;
byId("setup-favourites").onpointerup = event => endDrag(event);
byId("setup-favourites").onpointercancel = event => endDrag(event, true);
byId("setup-favourites").onlostpointercapture = event => endDrag(event, true);
async function devicePoll() {
  try {
    const data = await api("inputs");
    inputDevices = data.devices;
    const previous = byId("input-device").value;
    options("input-device", data.devices.map(d => [d.id, d.name]));
    if (!previous && data.devices.some(d => d.id === data.selected)) byId("input-device").value = data.selected;
    const keys = {1: "Back", 28: "OK", 103: "Up", 108: "Down", 105: "Left", 106: "Right", 113: "OK", 164: "Play / pause", 352: "OK"};
    if (data.test) byId("input-test").textContent = `Button received: ${keys[data.test.code] || data.test.code} — ${data.test.device}`;
    if (!preferencesLoaded) {
      const prefs = await api("preferences");
      byId("receiver-name").value = prefs.hostname;
      options("receiver-timezone", prefs.timezones.map(z => [z, z.replaceAll("_", " ")]));
      const local = Intl.DateTimeFormat().resolvedOptions().timeZone;
      byId("receiver-timezone").value = prefs.timezone === "Etc/UTC" && prefs.timezones.includes(local) ? local : prefs.timezone;
      preferencesLoaded = true;
    }
  } catch (_) { /* Device helper may be applying settings; retry independently. */ }
  controls(); setTimeout(devicePoll, 2000);
}
async function poll() {
  try {
    const data = await api("status");
    if (data.state === "configured") { window.location.replace("/#status"); window.location.reload(); return; }
    connected = true; setupState = data;
    const discovery = data.discovery || {state: "idle", servers: []};
    const previousServer = byId("server").value;
    servers = discovery.servers; renderServers();
    if (!previousServer && data.selected_server) byId("server").value = data.selected_server.udn;
    tuners = data.tuners || [];
    const previousTuner = byId("tuner").value;
    options("tuner", tuners.map(t => [t.frontend_uuid, `${t.server} — tuner ${t.tuner_number}`]));
    if (!previousTuner || activeStates.has(data.scan.state)) {
      const preferred = tuners.find(t => t.tuner_number === data.preferred_tuner_number);
      if (data.frontend_uuid || preferred) byId("tuner").value = data.frontend_uuid || preferred.frontend_uuid;
    }
    const discoveryText = discovery.state === "searching" ? "Searching the local network…" :
      discovery.state === "error" ? discovery.error : discovery.state === "complete" ?
        (servers.length ? `${servers.length} server(s) available. Select the one to use.` :
          discovery.interfaces?.length ? "No SAT-IP server replied. Search again or add its description URL below." :
            "No active LAN interface. Wait for Ethernet or Wi-Fi, then search again.") : "Ready to search.";
    byId("discovery-status").textContent = [discoveryText, ...(discovery.warnings || [])].join(" ");
    const registration = data.registration || {state: "idle"};
    byId("registration-status").textContent = registration.state === "connecting" ?
      "Connecting to your TV tuner…" : registration.state === "error" ? registration.error :
      registration.state === "ready" ? `${data.selected_server.name} is ready for the Astra channel search.` : "";
    const scan = data.scan;
    const labels = {idle: "Ready to set up your receiver.", starting: "Starting channel search…",
      pausing: "Waiting for exclusive tuner access…", running: `${scan.completed || 0} / ${scan.total || 0} transponders scanned — ${scan.found || 0} supported channels found`,
      stopping: "Stopping search and restoring tuner settings…", complete: scan.found ? `Search complete. ${scan.found} supported channels found. Select your initial favourites.` : "Search complete, but no supported channels were found. Check the satellite signal and selected tuner, then retry.",
      cancelled: "Search stopped. Channels found so far are available.", error: "Search failed. Check the tuner and retry."};
    byId("status").textContent = data.completion_pending ? "Starting TV. Your receiver page will open automatically…" :
      needsRecovery() ? "The previous search was interrupted or its settings could not be restored. Choose Restore tuner settings before retrying. Channels already found are kept." : labels[scan.state] || "Checking receiver…";
    const progress = byId("scan-progress");
    progress.hidden = !activeStates.has(scan.state) && !scan.total;
    if (scan.total) { progress.max = scan.total; progress.value = scan.completed || 0; }
    else progress.removeAttribute("value");
    const details = [];
    if (scan.state === "running" && scan.multiplex) details.push(`Current transponder: ${scan.multiplex}${scan.mux_elapsed !== undefined ? ` — ${scan.mux_elapsed}s elapsed` : ""}.`);
    if (scan.total && scan.state !== "running") details.push(`${scan.completed || 0} of ${scan.total} transponders processed.`);
    if (scan.failed_muxes) details.push(`${scan.failed_muxes} transponder(s) unsuccessful; these may have no signal or be unavailable.`);
    byId("scan-detail").textContent = details.join(" ");
    const next = JSON.stringify([data.candidates, data.starter]);
    if (next !== revision) {
      renderChannels(data); revision = next;
    }
  } catch (error) {
    connected = false;
    if (drag) endDrag({pointerId: drag.pointer}, true);
    byId("status").textContent = "Connection unavailable. Retrying automatically…";
  }
  controls(); setTimeout(poll, 2000);
  if (window.sourceSetupKind !== "enigma2" && connected && !pending && !setupState.selected_server && !setupState.selection_locked &&
      setupState.registration?.state === "idle" && setupState.discovery?.state === "complete" && servers.length === 1 &&
      !autoSelectedServers.has(servers[0].udn) && !byId("server").disabled) {
    autoSelectedServers.add(servers[0].udn);
    byId("server").value = servers[0].udn;
    byId("server").onchange();
  }
  if (connected && !searched && window.sourceSetupKind !== "enigma2") {
    searched = true;
    if (!setupState.selection_locked && !setupState.selected_server && setupState.discovery?.state !== "searching") byId("discover").click();
  }
}
window.sourceSetupControls = controls;
controls(); poll(); devicePoll();
