(function () {
    const RETRY_STEPS_MS = [2000, 4000, 6000, 8000, 10000];

    function parseIceServers(linkHeader) {
        if (!linkHeader) {
            return [];
        }

        return linkHeader
            .split(", ")
            .map((entry) => {
                const match = entry.match(
                    /^<(.+?)>; rel="ice-server"(; username="(.*?)"; credential="(.*?)"; credential-type="password")?/i,
                );
                if (!match) {
                    return null;
                }

                const iceServer = { urls: [match[1]] };
                if (match[3]) {
                    iceServer.username = JSON.parse(`"${match[3]}"`);
                    iceServer.credential = JSON.parse(`"${match[4]}"`);
                    iceServer.credentialType = "password";
                }
                return iceServer;
            })
            .filter(Boolean);
    }

    function parseOffer(sdp) {
        const offerData = {
            iceUfrag: "",
            icePwd: "",
            medias: [],
        };

        for (const line of sdp.split("\r\n")) {
            if (line.startsWith("m=")) {
                offerData.medias.push(line.slice(2));
            } else if (!offerData.iceUfrag && line.startsWith("a=ice-ufrag:")) {
                offerData.iceUfrag = line.slice("a=ice-ufrag:".length);
            } else if (!offerData.icePwd && line.startsWith("a=ice-pwd:")) {
                offerData.icePwd = line.slice("a=ice-pwd:".length);
            }
        }

        return offerData;
    }

    function generateCandidateFragment(offerData, candidates) {
        const grouped = new Map();
        for (const candidate of candidates) {
            const key = candidate.sdpMLineIndex;
            if (!grouped.has(key)) {
                grouped.set(key, []);
            }
            grouped.get(key).push(candidate);
        }

        let fragment = `a=ice-ufrag:${offerData.iceUfrag}\r\na=ice-pwd:${offerData.icePwd}\r\n`;
        offerData.medias.forEach((media, index) => {
            const entries = grouped.get(index);
            if (!entries || entries.length === 0) {
                return;
            }

            fragment += `m=${media}\r\na=mid:${index}\r\n`;
            entries.forEach((candidate) => {
                fragment += `a=${candidate.candidate}\r\n`;
            });
        });

        return fragment;
    }

    async function readError(response) {
        const contentType = response.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
            try {
                const payload = await response.json();
                return payload.error || payload.detail || `HTTP ${response.status}`;
            } catch (error) {
                return `HTTP ${response.status}`;
            }
        }

        const text = await response.text();
        return text || `HTTP ${response.status}`;
    }

    class MediaMtxWhepPlayer {
        constructor(options) {
            this.videoElement = options.videoElement;
            this.endpoint = options.endpoint;
            this.onStateChange = options.onStateChange || function () {};
            this.onTrack = options.onTrack || function () {};

            this.closed = true;
            this.peerConnection = null;
            this.sessionUrl = null;
            this.offerData = null;
            this.pendingCandidates = [];
            this.reconnectTimer = null;
            this.reconnectAttempts = 0;
            this.connectingPromise = null;
            this.reconnectReason = "";
            this.remoteStream = null;
        }

        async start() {
            this.closed = false;
            this.reconnectAttempts = 0;
            await this.connect("initial connection");
        }

        async reconnect(reason) {
            this.closed = false;
            this.reconnectAttempts = 0;
            this.cancelReconnectTimer();
            await this.connect(reason || "manual reconnect");
        }

        async stop() {
            this.closed = true;
            this.cancelReconnectTimer();
            await this.cleanup();
            this.emitState("stopped", "Stream stopped", "closed");
        }

        cancelReconnectTimer() {
            if (this.reconnectTimer) {
                window.clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
        }

        emitState(phase, detail, connectionState) {
            this.onStateChange({
                phase,
                detail,
                connectionState: connectionState || (this.peerConnection && this.peerConnection.connectionState) || "new",
            });
        }

        async connect(reason) {
            if (this.closed) {
                return;
            }

            if (this.connectingPromise) {
                return this.connectingPromise;
            }

            this.connectingPromise = this.connectInternal(reason).finally(() => {
                this.connectingPromise = null;
            });

            return this.connectingPromise;
        }

        async connectInternal(reason) {
            this.cancelReconnectTimer();
            await this.cleanup();
            this.emitState("connecting", reason || "Opening WebRTC session", "connecting");

            try {
                const iceServers = await this.requestIceServers();
                if (this.closed) {
                    return;
                }

                this.remoteStream = new MediaStream();
                this.videoElement.srcObject = this.remoteStream;

                const peerConnection = new RTCPeerConnection({
                    iceServers,
                    sdpSemantics: "unified-plan",
                });

                this.peerConnection = peerConnection;
                peerConnection.addTransceiver("video", { direction: "recvonly" });
                peerConnection.addTransceiver("audio", { direction: "recvonly" });

                peerConnection.onconnectionstatechange = () => {
                    this.handleConnectionStateChange();
                };
                peerConnection.onicecandidate = (event) => {
                    void this.handleIceCandidate(event);
                };
                peerConnection.ontrack = (event) => {
                    this.handleTrack(event);
                };

                const offer = await peerConnection.createOffer();
                await peerConnection.setLocalDescription(offer);
                this.offerData = parseOffer(peerConnection.localDescription.sdp);

                const answer = await this.postOffer(peerConnection.localDescription.sdp);
                if (this.closed || !this.peerConnection) {
                    return;
                }

                await peerConnection.setRemoteDescription({
                    type: "answer",
                    sdp: answer,
                });

                if (this.pendingCandidates.length > 0) {
                    await this.sendCandidates(this.pendingCandidates.splice(0));
                }
            } catch (error) {
                await this.scheduleReconnect(error instanceof Error ? error.message : String(error));
            }
        }

        async requestIceServers() {
            const response = await fetch(this.endpoint, {
                method: "OPTIONS",
            });

            if (!response.ok) {
                throw new Error(await readError(response));
            }

            return parseIceServers(response.headers.get("Link"));
        }

        async postOffer(offerSdp) {
            const response = await fetch(this.endpoint, {
                method: "POST",
                headers: {
                    "Content-Type": "application/sdp",
                },
                body: offerSdp,
            });

            if (response.status !== 201) {
                throw new Error(await readError(response));
            }

            const location = response.headers.get("Location");
            if (!location) {
                throw new Error("MediaMTX did not return a WHEP session URL");
            }

            this.sessionUrl = new URL(location, window.location.origin).toString();
            return response.text();
        }

        async handleIceCandidate(event) {
            if (!event.candidate || this.closed) {
                return;
            }

            if (!this.sessionUrl) {
                this.pendingCandidates.push(event.candidate);
                return;
            }

            try {
                await this.sendCandidates([event.candidate]);
            } catch (error) {
                await this.scheduleReconnect(error instanceof Error ? error.message : String(error));
            }
        }

        async sendCandidates(candidates) {
            if (!this.sessionUrl || !this.offerData || candidates.length === 0) {
                return;
            }

            const response = await fetch(this.sessionUrl, {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/trickle-ice-sdpfrag",
                    "If-Match": "*",
                },
                body: generateCandidateFragment(this.offerData, candidates),
            });

            if (response.status !== 204) {
                throw new Error(await readError(response));
            }
        }

        handleTrack(event) {
            const incomingStream = event.streams && event.streams[0];
            if (incomingStream) {
                this.videoElement.srcObject = incomingStream;
            } else {
                const alreadyAttached = this.remoteStream
                    .getTracks()
                    .some((track) => track.id === event.track.id);
                if (!alreadyAttached) {
                    this.remoteStream.addTrack(event.track);
                }
                this.videoElement.srcObject = this.remoteStream;
            }

            this.onTrack(event);
            this.emitState("connected", "Media track received", "connected");
            void this.videoElement.play().catch(() => {});
        }

        handleConnectionStateChange() {
            if (!this.peerConnection || this.closed) {
                return;
            }

            const { connectionState } = this.peerConnection;
            if (connectionState === "connected") {
                this.reconnectAttempts = 0;
                this.emitState("connected", "WebRTC peer connected", connectionState);
                return;
            }

            if (connectionState === "connecting" || connectionState === "new") {
                this.emitState("connecting", "WebRTC peer connecting", connectionState);
                return;
            }

            if (connectionState === "disconnected" || connectionState === "failed" || connectionState === "closed") {
                void this.scheduleReconnect(`Peer connection ${connectionState}`);
            }
        }

        async scheduleReconnect(reason) {
            if (this.closed || this.reconnectTimer) {
                return;
            }

            this.reconnectAttempts += 1;
            const delay = RETRY_STEPS_MS[Math.min(this.reconnectAttempts - 1, RETRY_STEPS_MS.length - 1)];
            this.reconnectReason = reason;

            await this.cleanup();
            this.emitState(
                "reconnecting",
                `${reason}. Retrying in ${(delay / 1000).toFixed(0)} seconds`,
                "reconnecting",
            );

            this.reconnectTimer = window.setTimeout(() => {
                this.reconnectTimer = null;
                void this.connect(this.reconnectReason || "retry");
            }, delay);
        }

        async cleanup() {
            const sessionUrl = this.sessionUrl;
            this.sessionUrl = null;
            this.offerData = null;
            this.pendingCandidates = [];

            if (this.peerConnection) {
                this.peerConnection.onconnectionstatechange = null;
                this.peerConnection.onicecandidate = null;
                this.peerConnection.ontrack = null;
                this.peerConnection.close();
                this.peerConnection = null;
            }

            if (this.videoElement.srcObject) {
                this.videoElement.srcObject = null;
            }

            if (sessionUrl) {
                try {
                    await fetch(sessionUrl, {
                        method: "DELETE",
                    });
                } catch (error) {
                    // Ignore cleanup failures because reconnect should still proceed.
                }
            }
        }
    }

    window.MediaMtxWhepPlayer = MediaMtxWhepPlayer;
})();
