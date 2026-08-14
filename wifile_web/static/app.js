"use strict";

// ---------------------------------------------------------------------------
// WiFile Web SPA — talks to the Phase 2 backend:
//   GET  /api/state           -> snapshot
//   GET  /api/events          -> Server-Sent Events (polling fallback)
//   POST /api/start|stop|answer|upload
// ---------------------------------------------------------------------------

const MODE = { SERVER: "server", CLIENT: "client" };

const els = {
    server: {
        start: document.getElementById("server-start"),
        stop: document.getElementById("server-stop"),
        source: document.getElementById("server-source"),
        port: document.getElementById("server-port"),
        drop: document.getElementById("server-drop"),
        status: document.getElementById("server-status"),
        bar: document.getElementById("server-bar"),
        meta: document.getElementById("server-meta"),
        prompt: document.getElementById("server-prompt"),
        promptText: document.getElementById("server-prompt-text"),
        promptControls: document.getElementById("server-prompt-controls"),
        files: document.getElementById("server-files"),
        log: document.getElementById("server-log"),
    },
    client: {
        start: document.getElementById("client-start"),
        stop: document.getElementById("client-stop"),
        host: document.getElementById("client-host"),
        port: document.getElementById("client-port"),
        output: document.getElementById("client-output"),
        status: document.getElementById("client-status"),
        bar: document.getElementById("client-bar"),
        meta: document.getElementById("client-meta"),
        prompt: document.getElementById("client-prompt"),
        promptText: document.getElementById("client-prompt-text"),
        promptControls: document.getElementById("client-prompt-controls"),
        files: document.getElementById("client-files"),
        log: document.getElementById("client-log"),
    },
    dot: document.getElementById("conn-dot"),
    connLabel: document.getElementById("conn-label"),
    toast: document.getElementById("toast"),
    peers: {
        list: document.getElementById("peer-list"),
        empty: document.getElementById("peers-empty"),
        count: document.getElementById("peers-count"),
    },
    settings: {
        broadcast: document.getElementById("server-broadcast"),
        listen: document.getElementById("client-listen"),
    },
    broadcastHint: document.getElementById("server-broadcast-hint"),
};

let pollTimer = null;
let toastTimer = null;
const lastLogLen = { server: 0, client: 0 };
const failToasted = { server: false, client: false };

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function fmtBytes(value) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = value;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return v.toFixed(1) + " " + units[i];
}

function toast(message) {
    els.toast.textContent = message;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        els.toast.hidden = true;
    }, 3500);
}

async function api(path, payload) {
    const options = payload
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }
        : {};
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.error || "request failed (" + response.status + ")");
    }
    return data;
}

function setConn(kind, label) {
    els.dot.className = "dot " + kind;
    els.connLabel.textContent = label;
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function render(snapshot) {
    renderPane(MODE.SERVER, snapshot.server);
    renderPane(MODE.CLIENT, snapshot.client);
    renderSettings(snapshot.settings || {});
    renderPeers(
        snapshot.peers || [],
        snapshot.client.running,
        !!(snapshot.settings || {}).listen
    );
}

// The opt-in discovery toggles; mirror the server's settings in the checkboxes.
function renderSettings(settings) {
    els.settings.broadcast.checked = !!settings.broadcast;
    els.settings.listen.checked = !!settings.listen;
}

async function setSetting(key, value) {
    try {
        await api("/api/settings", { [key]: value });
    } catch (error) {
        toast(error.message);
        refresh(); // re-sync the toggle with the server state
    }
}

function bindSettings() {
    els.settings.broadcast.addEventListener("change", () =>
        setSetting("broadcast", els.settings.broadcast.checked)
    );
    els.settings.listen.addEventListener("change", () =>
        setSetting("listen", els.settings.listen.checked)
    );
}

// Senders found on the LAN via UDP broadcast announcements.
function renderPeers(peers, receiving, listening) {
    const list = els.peers.list;
    list.replaceChildren();
    list.hidden = !listening || peers.length === 0;
    els.peers.empty.hidden = listening && peers.length > 0;
    els.peers.empty.textContent = listening
        ? "No senders found yet. When someone starts sending from WiFile on this network, they appear here."
        : "Turn on \u201cListen for senders\u201d to discover WiFile senders on this network.";
    els.peers.count.textContent = listening
        ? peers.length
            ? peers.length + " found"
            : "listening…"
        : "off";

    for (const peer of peers) {
        const item = document.createElement("li");
        item.className = "peer";

        const info = document.createElement("div");
        info.className = "peer-info";

        const name = document.createElement("div");
        name.className = "peer-name";
        if (peer.web_port) {
            const link = document.createElement("a");
            link.href = "http://" + peer.host + ":" + peer.web_port + "/";
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = peer.name;
            name.append(link);
        } else {
            name.textContent = peer.name;
        }
        const endpoint = document.createElement("span");
        endpoint.className = "peer-endpoint";
        endpoint.textContent = peer.host + ":" + peer.port;
        name.append(endpoint);

        const source = document.createElement("div");
        source.className = "peer-source";
        source.textContent = peer.source || "sending files";
        info.append(name, source);

        const connect = document.createElement("button");
        connect.type = "button";
        connect.className = "btn primary peer-connect";
        connect.textContent = "Connect";
        connect.disabled = receiving;
        connect.addEventListener("click", () => connectPeer(peer));

        item.append(info, connect);
        list.append(item);
    }
}

function connectPeer(peer) {
    els.client.host.value = peer.host;
    els.client.port.value = String(peer.port);
    startClient();
}

function renderPane(mode, slot) {
    const ui = els[mode];

    ui.start.disabled = slot.running;
    ui.stop.disabled = !slot.running;
    const inputs =
        mode === MODE.SERVER
            ? [ui.source, ui.port]
            : [ui.host, ui.port, ui.output];
    inputs.forEach((input) => {
        input.disabled = slot.running;
    });
    document.querySelectorAll(
        mode === MODE.SERVER
            ? "#server-pick-files-btn, #server-pick-folder-btn"
            : 'input[name="conflict"]'
    ).forEach((node) => {
        node.disabled = slot.running;
    });
    if (mode === MODE.SERVER) {
        ui.drop.classList.toggle("disabled", slot.running);
        els.broadcastHint.hidden = !(
            slot.running && !els.settings.broadcast.checked
        );
    }
    watchForStartFailure(mode, slot);

    const last = slot.log.length ? slot.log[slot.log.length - 1] : "";
    ui.status.textContent = last || (slot.running ? "Starting…" : "Idle");
    ui.status.classList.toggle("active", slot.running);

    if (slot.progress) {
        const p = slot.progress;
        ui.bar.style.width = p.percent + "%";
        let meta = p.percent.toFixed(1) + "% · " + fmtBytes(p.current) + " / " + fmtBytes(p.total);
        if (p.speed > 0) meta += " · " + fmtBytes(p.speed) + "/s";
        if (p.eta > 0) meta += " · ETA " + Math.round(p.eta) + "s";
        ui.meta.textContent = meta;
    } else {
        ui.bar.style.width = "0%";
        ui.meta.textContent = "";
    }

    renderPrompt(mode, slot.prompt, ui);
    renderFiles(mode, slot.log, ui);
    renderLog(slot.log, ui);
}

// Surface engine startup failures (bad port in use, vanished source, …) as a
// toast once per engine round instead of leaving them buried in the log.
function watchForStartFailure(mode, slot) {
    const fresh = slot.log.slice(lastLogLen[mode]);
    lastLogLen[mode] = slot.log.length;
    if (slot.running) {
        failToasted[mode] = false; // a new engine round is active
        return;
    }
    if (!failToasted[mode]) {
        const failure = fresh.find((line) => /failed to start/i.test(line));
        if (failure) {
            failToasted[mode] = true;
            toast(failure);
        }
    }
}

function promptLabel(prompt) {
    const text = prompt.text;
    if (prompt.kind === "ask_text") return text.trim();
    if (/next action/i.test(text)) return "What would you like to do next?";
    if (/choose action/i.test(text)) return "This file already exists. What should we do?";
    return text.trim();
}

function promptButtons(options) {
    const set = new Set(options);
    if (set.has("overwrite")) {
        return [
            { label: "Overwrite", value: "o", cls: "primary" },
            { label: "Rename", value: "r", cls: "ghost" },
            { label: "Cancel", value: "c", cls: "danger" },
        ];
    }
    if (set.has("s")) {
        return [
            { label: "Send same", value: "s", cls: "primary" },
            { label: "New file/folder", value: "n", cls: "ghost" },
            { label: "Stop", value: "e", cls: "danger" },
        ];
    }
    return [
        { label: "Continue", value: "c", cls: "primary" },
        { label: "New location", value: "n", cls: "ghost" },
        { label: "Stop", value: "e", cls: "danger" },
    ];
}

function renderPrompt(mode, prompt, ui) {
    ui.promptControls.replaceChildren();
    if (!prompt) {
        ui.prompt.hidden = true;
        return;
    }
    ui.prompt.hidden = false;
    ui.promptText.textContent = promptLabel(prompt);

    if (prompt.kind === "ask_text") {
        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "Type here…";
        const ok = document.createElement("button");
        ok.type = "button";
        ok.className = "btn primary";
        ok.textContent = "OK";
        const submit = () => {
            if (input.value.trim()) answer(mode, prompt.id, input.value.trim());
        };
        ok.addEventListener("click", submit);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") submit();
        });
        ui.promptControls.append(input, ok);
        input.focus();
        return;
    }

    for (const { label, value, cls } of promptButtons(prompt.options)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn " + cls;
        button.textContent = label;
        button.addEventListener("click", () => answer(mode, prompt.id, value));
        ui.promptControls.append(button);
    }
}

const FILE_PATTERNS = {
    [MODE.SERVER]: [
        [/^Sending '(.+?)' \(/, "sending"],
        [/^File '(.+?)' sent successfully/, "sent"],
        [/^Client declined '(.+?)'/, "declined"],
        [/^Client reported a problem saving '(.+?)'/, "failed"],
    ],
    [MODE.CLIENT]: [
        [/^Receiving '(.+?)' \(/, "receiving"],
        [/^File '(.+?)' received and saved to/, "received"],
        [/^Warning: File '(.+?)' already exists/, "conflict"],
    ],
};

function parseFiles(log, mode) {
    const files = new Map();
    for (const line of log) {
        for (const [pattern, status] of FILE_PATTERNS[mode]) {
            const match = line.match(pattern);
            if (match) {
                files.set(match[1], status);
                break;
            }
        }
    }
    const last = log.length ? log[log.length - 1] : "";
    if (/Transfer stopped\.|Batch transfer cancelled\.|Transfer cancelled\./.test(last)) {
        for (const [name, status] of files) {
            if (status === "sending" || status === "receiving") {
                files.set(name, "cancelled");
            }
        }
    }
    return files;
}

function renderFiles(mode, log, ui) {
    const files = parseFiles(log, mode);
    ui.files.replaceChildren();
    ui.files.hidden = files.size === 0;
    for (const [name, status] of files) {
        const item = document.createElement("li");
        const fname = document.createElement("span");
        fname.className = "fname";
        fname.textContent = name;
        const chip = document.createElement("span");
        chip.className = "chip " + status;
        chip.textContent = status;
        item.append(fname, chip);
        ui.files.append(item);
    }
}

function renderLog(log, ui) {
    ui.log.replaceChildren();
    const start = Math.max(0, log.length - 80);
    for (let i = start; i < log.length; i += 1) {
        const line = document.createElement("div");
        line.textContent = log[i];
        ui.log.append(line);
    }
    ui.log.scrollTop = ui.log.scrollHeight;
}

// ---------------------------------------------------------------------------
// API actions
// ---------------------------------------------------------------------------

function parsePort(input) {
    const port = parseInt(input.value, 10);
    if (Number.isNaN(port) || port < 1 || port > 65535) {
        toast("Port must be between 1 and 65535");
        return null;
    }
    return port;
}

async function startServer() {
    const source = els.server.source.value.trim();
    if (!source) return toast("Choose a file or folder to send first");
    const port = parsePort(els.server.port);
    if (port === null) return;
    try {
        await api("/api/start", { mode: MODE.SERVER, port, source });
    } catch (error) {
        toast(error.message);
    }
}

async function stopServer() {
    try {
        await api("/api/stop", { mode: MODE.SERVER });
    } catch (error) {
        toast(error.message);
    }
}

function conflictChoice() {
    const selected = document.querySelector('input[name="conflict"]:checked');
    return selected ? selected.value : "ask";
}

async function startClient() {
    const host = els.client.host.value.trim();
    if (!host) return toast("Enter the server address first");
    const port = parsePort(els.client.port);
    if (port === null) return;
    const output = els.client.output.value.trim() || ".";
    try {
        await api("/api/start", {
            mode: MODE.CLIENT,
            host,
            port,
            output_dir: output,
            conflict: conflictChoice(),
        });
    } catch (error) {
        toast(error.message);
    }
}

async function stopClient() {
    try {
        await api("/api/stop", { mode: MODE.CLIENT });
    } catch (error) {
        toast(error.message);
    }
}

async function answer(mode, promptId, choice) {
    try {
        await api("/api/answer", { mode, prompt_id: promptId, choice });
    } catch (error) {
        toast(error.message);
    }
}

// ---------------------------------------------------------------------------
// uploads (drop zone + pickers)
// ---------------------------------------------------------------------------

function collectDrop(dataTransfer) {
    const items = Array.from(dataTransfer.items || []);
    const entries = items
        .map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry())
        .filter(Boolean);
    if (!entries.length) {
        // Fallback (Safari): no directory information available.
        return Promise.resolve(
            Array.from(dataTransfer.files || []).map((file) => ({
                file,
                path: file.name,
            }))
        );
    }
    const out = [];
    const walk = (entry, dir) =>
        new Promise((resolve) => {
            if (entry.isFile) {
                entry.file(
                    (file) => {
                        out.push({ file, path: dir + file.name });
                        resolve();
                    },
                    () => resolve()
                );
            } else if (entry.isDirectory) {
                const reader = entry.createReader();
                const readAll = (acc) =>
                    new Promise((done) =>
                        reader.readEntries(
                            (batch) => {
                                if (batch.length) readAll(acc.concat(batch)).then(done);
                                else done(acc);
                            },
                            () => done(acc)
                        )
                    );
                readAll([]).then((list) =>
                    Promise.all(list.map((child) => walk(child, dir + entry.name + "/"))).then(
                        resolve
                    )
                );
            } else {
                resolve();
            }
        });
    return Promise.all(entries.map((entry) => walk(entry, ""))).then(() => out);
}

async function uploadFiles(files) {
    const form = new FormData();
    for (const { file, path } of files) form.append(path, file, file.name);
    try {
        const response = await fetch("/api/upload", { method: "POST", body: form });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "upload failed");
        els.server.source.value = data.source;
        toast(data.count + " file(s) ready — press Start to send them");
    } catch (error) {
        toast(error.message);
    }
}

function bindDrop(zone) {
    ["dragenter", "dragover"].forEach((type) =>
        zone.addEventListener(type, (event) => {
            event.preventDefault();
            zone.classList.add("drag");
        })
    );
    ["dragleave", "drop"].forEach((type) =>
        zone.addEventListener(type, (event) => {
            event.preventDefault();
            zone.classList.remove("drag");
        })
    );
    zone.addEventListener("drop", async (event) => {
        const files = await collectDrop(event.dataTransfer);
        if (files.length) await uploadFiles(files);
    });
}

function bindPicker(button, input, pathOf) {
    button.addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
        const files = Array.from(input.files || []).map((file) => ({
            file,
            path: pathOf(file),
        }));
        input.value = "";
        if (files.length) await uploadFiles(files);
    });
}

// ---------------------------------------------------------------------------
// connection (SSE with polling fallback)
// ---------------------------------------------------------------------------

async function refresh() {
    try {
        render(await api("/api/state"));
    } catch (error) {
        setConn("off", "offline");
    }
}

function startPolling() {
    if (pollTimer) return;
    setConn("poll", "polling");
    pollTimer = setInterval(refresh, 1000);
    refresh();
}

function connect() {
    if (typeof EventSource === "undefined") {
        startPolling();
        return;
    }
    const source = new EventSource("/api/events");
    source.onmessage = (event) => {
        setConn("live", "live");
        render(JSON.parse(event.data));
    };
    source.onerror = () => {
        source.close();
        startPolling();
    };
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
    els.server.start.addEventListener("click", startServer);
    els.server.stop.addEventListener("click", stopServer);
    els.client.start.addEventListener("click", startClient);
    els.client.stop.addEventListener("click", stopClient);
    bindSettings();

    els.server.source.addEventListener("keydown", (event) => {
        if (event.key === "Enter") startServer();
    });
    els.client.host.addEventListener("keydown", (event) => {
        if (event.key === "Enter") startClient();
    });

    bindDrop(els.server.drop);
    bindPicker(
        document.getElementById("server-pick-files-btn"),
        document.getElementById("server-pick-files"),
        (file) => file.name
    );
    bindPicker(
        document.getElementById("server-pick-folder-btn"),
        document.getElementById("server-pick-folder"),
        (file) => file.webkitRelativePath || file.name
    );

    refresh();
    connect();
});
