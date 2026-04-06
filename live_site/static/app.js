(function () {
    const dom = {
        connectionPill: document.getElementById("connection-pill"),
        telemetryPill: document.getElementById("telemetry-pill"),
        detailConnection: document.getElementById("detail-connection"),
        detailPlayback: document.getElementById("detail-playback"),
        detailPath: document.getElementById("detail-path"),
        detailEvent: document.getElementById("detail-event"),
        overlay: document.getElementById("video-overlay"),
        overlayTitle: document.getElementById("overlay-title"),
        overlayCopy: document.getElementById("overlay-copy"),
        reconnectButton: document.getElementById("reconnect-button"),
        video: document.getElementById("live-video"),
        telemetryLatitude: document.getElementById("telemetry-latitude"),
        telemetryLongitude: document.getElementById("telemetry-longitude"),
        telemetryPlate: document.getElementById("telemetry-plate"),
        telemetryConfidence: document.getElementById("telemetry-confidence"),
        telemetryTimestamp: document.getElementById("telemetry-timestamp"),
        telemetryStatus: document.getElementById("telemetry-status"),
        deviceIdValue: document.getElementById("device-id-value"),
        deviceOnlineValue: document.getElementById("device-online-value"),
        cameraStateValue: document.getElementById("camera-state-value"),
        pendingCommandsValue: document.getElementById("pending-commands-value"),
        cameraOnButton: document.getElementById("camera-on-button"),
        captureImageButton: document.getElementById("capture-image-button"),
        cameraOffButton: document.getElementById("camera-off-button"),
        commandFeedback: document.getElementById("command-feedback"),
    };

    const telemetryReconnectMs = [2000, 4000, 6000, 8000, 10000];
    let player = null;
    let telemetrySocket = null;
    let currentDeviceId = "jetson-01";
    let deviceStatusPollTimer = null;
    let connectionPhase = "connecting";
    let connectionDetail = "Starting player";
    let playbackState = "Loading";

    function setPill(element, label, tone) {
        element.textContent = label;
        element.dataset.tone = tone;
    }

    function setLastEvent(message) {
        const time = new Date().toLocaleTimeString();
        dom.detailEvent.textContent = `${time} - ${message}`;
    }

    function setCommandFeedback(message) {
        dom.commandFeedback.textContent = message;
    }

    function setCommandButtonsDisabled(disabled) {
        dom.cameraOnButton.disabled = disabled;
        dom.captureImageButton.disabled = disabled;
        dom.cameraOffButton.disabled = disabled;
    }

    function formatNumber(value, digits) {
        if (value === null || value === undefined || value === "") {
            return "--";
        }

        const numberValue = Number(value);
        if (Number.isNaN(numberValue)) {
            return value;
        }

        return numberValue.toFixed(digits);
    }

    function formatText(value) {
        return value === null || value === undefined || value === "" ? "--" : value;
    }

    function updateOverlay() {
        const shouldHide = connectionPhase === "connected" && playbackState === "Playing";
        dom.overlay.classList.toggle("hidden", shouldHide);

        if (shouldHide) {
            return;
        }

        if (connectionPhase === "reconnecting") {
            dom.overlayTitle.textContent = "Reconnecting stream";
            dom.overlayCopy.textContent = connectionDetail;
            return;
        }

        if (connectionPhase === "connected" && playbackState !== "Playing") {
            dom.overlayTitle.textContent = "Connected, waiting for frames";
            dom.overlayCopy.textContent = "The WebRTC session is up. Waiting for the video element to resume playback.";
            return;
        }

        if (connectionPhase === "failed") {
            dom.overlayTitle.textContent = "Stream failed";
            dom.overlayCopy.textContent = connectionDetail;
            return;
        }

        dom.overlayTitle.textContent = "Starting live stream";
        dom.overlayCopy.textContent = connectionDetail;
    }

    function updatePlaybackState(label, tone, eventMessage) {
        playbackState = label;
        dom.detailPlayback.textContent = label;
        if (eventMessage) {
            setLastEvent(eventMessage);
        }
        if (tone === "bad") {
            setPill(dom.connectionPill, "Playback issue", "bad");
        }
        updateOverlay();
    }

    function applyTelemetry(snapshot) {
        const confidenceValue = Number(snapshot.confidence);
        dom.telemetryLatitude.textContent = formatNumber(snapshot.latitude, 6);
        dom.telemetryLongitude.textContent = formatNumber(snapshot.longitude, 6);
        dom.telemetryPlate.textContent = formatText(snapshot.detected_plate);
        dom.telemetryConfidence.textContent =
            snapshot.confidence === null || snapshot.confidence === undefined || Number.isNaN(confidenceValue)
                ? "--"
                : `${(confidenceValue * 100).toFixed(1)}%`;
        dom.telemetryTimestamp.textContent = formatText(snapshot.timestamp || snapshot.received_at);
        dom.telemetryStatus.textContent = formatText(snapshot.robot_status);
    }

    function handlePlayerState(state) {
        connectionPhase = state.phase;
        connectionDetail = state.detail;
        dom.detailConnection.textContent = state.connectionState;

        if (state.phase === "connected") {
            setPill(dom.connectionPill, "Stream connected", "good");
        } else if (state.phase === "failed") {
            setPill(dom.connectionPill, "Stream failed", "bad");
        } else if (state.phase === "reconnecting") {
            setPill(dom.connectionPill, "Reconnecting", "loading");
        } else if (state.phase === "stopped") {
            setPill(dom.connectionPill, "Stopped", "idle");
        } else {
            setPill(dom.connectionPill, "Connecting", "loading");
        }

        setLastEvent(state.detail);
        updateOverlay();
    }

    function updateDeviceStatus(snapshot) {
        dom.deviceIdValue.textContent = snapshot.device_id || currentDeviceId;
        dom.deviceOnlineValue.textContent = snapshot.is_online ? "Online" : "Offline";
        dom.cameraStateValue.textContent = snapshot.camera_on ? "On" : "Off";
        dom.pendingCommandsValue.textContent = String(snapshot.pending_command_count ?? 0);
    }

    async function refreshDeviceStatus() {
        try {
            const response = await fetch(`/api/devices/${encodeURIComponent(currentDeviceId)}/status`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            updateDeviceStatus(await response.json());
        } catch (error) {
            dom.deviceOnlineValue.textContent = "Unavailable";
            dom.cameraStateValue.textContent = "Unknown";
            dom.pendingCommandsValue.textContent = "--";
        }
    }

    async function queueCommand(commandName) {
        setCommandButtonsDisabled(true);
        setCommandFeedback(`Queueing ${commandName}...`);

        try {
            const response = await fetch(`/api/devices/${encodeURIComponent(currentDeviceId)}/commands`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    command: commandName,
                    payload: {
                        requested_at: new Date().toISOString(),
                        source: "website-demo",
                    },
                }),
            });

            if (!response.ok) {
                throw new Error(`Command failed with HTTP ${response.status}`);
            }

            const payload = await response.json();
            const commandId = payload.command && payload.command.id ? ` #${payload.command.id}` : "";
            setCommandFeedback(`Queued ${commandName}${commandId}.`);
            setLastEvent(`Queued ${commandName}${commandId}`);
            await refreshDeviceStatus();
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setCommandFeedback(`Unable to queue ${commandName}: ${message}`);
            setLastEvent(`Command error: ${message}`);
        } finally {
            setCommandButtonsDisabled(false);
        }
    }

    function bindVideoEvents() {
        dom.video.addEventListener("playing", () => {
            updatePlaybackState("Playing", "good", "Video playing");
        });
        dom.video.addEventListener("waiting", () => {
            updatePlaybackState("Buffering", "loading", "Video waiting for more data");
        });
        dom.video.addEventListener("stalled", () => {
            updatePlaybackState("Stalled", "loading", "Video stalled");
        });
        dom.video.addEventListener("pause", () => {
            if (!dom.video.ended) {
                updatePlaybackState("Paused", "idle", "Video paused");
            }
        });
        dom.video.addEventListener("ended", () => {
            updatePlaybackState("Ended", "bad", "Video ended unexpectedly");
            if (player) {
                void player.reconnect("Video element ended");
            }
        });
        dom.video.addEventListener("error", () => {
            updatePlaybackState("Error", "bad", "Browser video element reported an error");
            if (player) {
                void player.reconnect("Video element error");
            }
        });
        dom.video.addEventListener("loadstart", () => {
            updatePlaybackState("Loading", "loading");
        });
        dom.video.addEventListener("emptied", () => {
            updatePlaybackState("Resetting", "loading");
        });
    }

    function buildWebSocketUrl(path) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        return new URL(path, `${protocol}//${window.location.host}`).toString();
    }

    class TelemetrySocket {
        constructor(path, handlers) {
            this.path = path;
            this.handlers = handlers;
            this.socket = null;
            this.closed = false;
            this.reconnectAttempt = 0;
            this.reconnectTimer = null;
        }

        start() {
            this.closed = false;
            this.open();
        }

        stop() {
            this.closed = true;
            if (this.reconnectTimer) {
                window.clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            if (this.socket) {
                this.socket.close();
                this.socket = null;
            }
        }

        open() {
            if (this.closed) {
                return;
            }

            this.handlers.onState("Telemetry connecting", "loading");
            this.socket = new WebSocket(buildWebSocketUrl(this.path));

            this.socket.onopen = () => {
                this.reconnectAttempt = 0;
                this.handlers.onState("Telemetry live", "good");
            };

            this.socket.onmessage = (event) => {
                try {
                    const payload = JSON.parse(event.data);
                    this.handlers.onMessage(payload);
                } catch (error) {
                    this.handlers.onState("Telemetry parse error", "bad");
                }
            };

            this.socket.onerror = () => {
                this.handlers.onState("Telemetry error", "bad");
            };

            this.socket.onclose = () => {
                this.socket = null;
                if (this.closed) {
                    return;
                }
                this.scheduleReconnect();
            };
        }

        scheduleReconnect() {
            if (this.closed || this.reconnectTimer) {
                return;
            }

            const delay = telemetryReconnectMs[Math.min(this.reconnectAttempt, telemetryReconnectMs.length - 1)];
            this.reconnectAttempt += 1;
            this.handlers.onState(`Telemetry reconnecting in ${(delay / 1000).toFixed(0)}s`, "loading");
            this.reconnectTimer = window.setTimeout(() => {
                this.reconnectTimer = null;
                this.open();
            }, delay);
        }
    }

    async function init() {
        bindVideoEvents();

        let config;
        try {
            const response = await fetch("/api/config");
            if (!response.ok) {
                throw new Error(`Config request failed with HTTP ${response.status}`);
            }
            config = await response.json();
        } catch (error) {
            connectionPhase = "failed";
            connectionDetail = error instanceof Error ? error.message : String(error);
            setPill(dom.connectionPill, "Config failed", "bad");
            dom.detailConnection.textContent = "failed";
            updateOverlay();
            setLastEvent(connectionDetail);
            return;
        }

        dom.detailPath.textContent = config.streamPath;
        currentDeviceId = config.deviceId || currentDeviceId;
        dom.deviceIdValue.textContent = currentDeviceId;

        player = new window.MediaMtxWhepPlayer({
            videoElement: dom.video,
            endpoint: config.whepEndpoint,
            onStateChange: handlePlayerState,
        });

        dom.reconnectButton.addEventListener("click", () => {
            setLastEvent("Manual reconnect requested");
            void player.reconnect("Operator requested reconnect");
        });
        dom.cameraOnButton.addEventListener("click", () => {
            void queueCommand("camera_on");
        });
        dom.captureImageButton.addEventListener("click", () => {
            void queueCommand("capture_image");
        });
        dom.cameraOffButton.addEventListener("click", () => {
            void queueCommand("camera_off");
        });

        telemetrySocket = new TelemetrySocket(config.telemetryWebSocketPath, {
            onState: (label, tone) => setPill(dom.telemetryPill, label, tone),
            onMessage: (payload) => {
                applyTelemetry(payload);
                setLastEvent("Telemetry updated");
            },
        });

        await refreshDeviceStatus();
        deviceStatusPollTimer = window.setInterval(() => {
            void refreshDeviceStatus();
        }, 5000);
        telemetrySocket.start();
        await player.start();
    }

    window.addEventListener("beforeunload", () => {
        if (deviceStatusPollTimer) {
            window.clearInterval(deviceStatusPollTimer);
            deviceStatusPollTimer = null;
        }
        if (telemetrySocket) {
            telemetrySocket.stop();
        }
        if (player) {
            void player.stop();
        }
    });

    void init();
})();
