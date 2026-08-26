import { r as __toESM } from "./rolldown-runtime-B-1-B7_t.js";
import { y as Pr } from "./RosLib-CPlaUgkF.js";
import { t as require_browser } from "./browser-4n8XU-5M.js";
//#region node_modules/roslib/dist/WsWebSocketTransport-6-v9C0gj.js
var import_browser = /* @__PURE__ */ __toESM(require_browser(), 1);
var i = class extends Pr {
	constructor(e) {
		super(), this.socket = e, this.registerEventListeners();
	}
	send(e) {
		this.socket.send(JSON.stringify(e));
	}
	close() {
		this.socket.close();
	}
	isConnecting() {
		return this.socket.readyState === import_browser.WebSocket.CONNECTING;
	}
	isOpen() {
		return this.socket.readyState === import_browser.WebSocket.OPEN;
	}
	isClosing() {
		return this.socket.readyState === import_browser.WebSocket.CLOSING;
	}
	isClosed() {
		return this.socket.readyState === import_browser.WebSocket.CLOSED;
	}
	registerEventListeners() {
		this.socket.onopen = (e) => {
			this.emit("open", e);
		}, this.socket.onclose = (e) => {
			this.emit("close", e);
		}, this.socket.onerror = (e) => {
			this.emit("error", e);
		}, this.socket.onmessage = (e) => {
			this.handleRawMessage(e.data);
		};
	}
};
//#endregion
export { i as WsWebSocketTransport };
