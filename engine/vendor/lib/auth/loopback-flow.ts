/**
 * Loopback OAuth session primitive.
 *
 * One typed session shared by the automatic-browser and manual-browser
 * choices: the listener is bound BEFORE any browser is opened, so the browser
 * choice cannot land on a redirect_uri nobody is listening on. Manual mode
 * (`openBrowser=false`) never opens a browser. If the listener cannot bind —
 * whether it reports itself unready or `startLocalOAuthServer` throws — the
 * browser is never opened and a typed `unavailable` lifecycle marker is
 * returned; the caller maps that marker to user-facing guidance (label names,
 * fallback instructions) because those belong at the auth-method boundary in
 * index.ts, not inside this primitive.
 *
 * A browser that will not launch is NOT a terminal condition. The listener is
 * already bound and its deadline already running, so the session comes back
 * ready with `browserOpened: false` and the caller prints the URL for the user
 * to open. Ending the session there would strand every host with no opener on
 * PATH, which is the ordinary case on Linux and WSL.
 *
 * A ready session starts its callback observation IMMEDIATELY, so the
 * listener's existing five-minute deadline runs from session creation rather
 * than from the host's callback. An authorization the host abandons after
 * taking the URL therefore still releases port 1455 instead of pinning it for
 * the lifetime of the process. That observation is normalized so it can never
 * reject: an unobserved rejecting promise would be an unhandled rejection in
 * the window before `waitAndExchange()` awaits it.
 *
 * `waitAndExchange()` remains the lazy token-exchange boundary - the host
 * stores the authorization and calls back separately - and awaits that shared
 * outcome before exchanging the code with the flow's own PKCE verifier and
 * `REDIRECT_URI`. Every terminal path (callback, timeout, external close,
 * opener failure, throwing wait, throwing exchange) runs through one
 * `closeOnce` gate, so `server.close()` happens exactly once. A null callback
 * surfaces as a typed `cancelled` lifecycle result, and a callback arriving
 * after expiry observes that already-settled cancellation.
 *
 * This module holds NO labels, persistence, or OpenCode method shapes. It
 * does not duplicate the URL parser, PKCE generator, callback server, or
 * token exchange - it composes the primitives that already own them.
 */
import type {
	OAuthServerInfo,
	AuthorizationFlow,
	TokenResult,
} from "../types.js";
import {
	createAuthorizationFlow as defaultCreateAuthorizationFlow,
	exchangeAuthorizationCode as defaultExchangeAuthorizationCode,
	REDIRECT_URI,
} from "./auth.js";
import { startLocalOAuthServer as defaultStartLocalOAuthServer } from "./server.js";
import { openBrowserUrl as defaultOpenBrowserUrl } from "./browser.js";

export interface LoopbackFlowDeps {
	createAuthorizationFlow: (
		opts?: { forceNewLogin?: boolean },
	) => Promise<AuthorizationFlow>;
	startLocalOAuthServer: (opts: { state: string }) => Promise<OAuthServerInfo>;
	openBrowserUrl: (url: string) => boolean;
	exchangeAuthorizationCode: (
		code: string,
		verifier: string,
		redirectUri?: string,
	) => Promise<TokenResult>;
}

export interface LoopbackFlowOptions {
	/**
	 * `true` opens the user's default browser AFTER the listener is ready.
	 * `false` (manual mode) never calls `openBrowserUrl`; the caller is
	 * expected to hand `url` back to the user directly.
	 */
	openBrowser: boolean;
	/**
	 * Forwarded verbatim to `createAuthorizationFlow`. When true, the
	 * authorize URL gets `prompt=login` so a cached browser session cannot
	 * silently reuse the previous account.
	 */
	forceNewLogin?: boolean;
	/**
	 * Test seam. Individual overrides merge onto the production defaults;
	 * production callers pass nothing.
	 */
	deps?: Partial<LoopbackFlowDeps>;
}

export interface LoopbackFlowReady {
	type: "ready";
	/**
	 * Authorize URL to hand to the user (manual mode) or that the primitive
	 * has already opened (automatic mode).
	 */
	url: string;
	/**
	 * `false` when `openBrowser` was asked for and the launch failed — no
	 * `xdg-open` on PATH, an opener that threw. The session stays live: the
	 * listener is bound and the deadline is running, so printing `url` is all
	 * the user needs to finish the login in a browser they open themselves.
	 * Always `true` in manual mode, where no browser was ever going to open.
	 */
	browserOpened: boolean;
	/**
	 * Idempotent listener close. Safe to call more than once; safe to call
	 * before `waitAndExchange()` starts, which then completes without a
	 * second close.
	 */
	close: () => void;
	/**
	 * Awaits the callback, exchanges the code with the same PKCE verifier
	 * and `REDIRECT_URI` used to construct the authorize URL, and closes
	 * the listener exactly once (whether the callback succeeded, timed
	 * out, or the wait/exchange threw). A null callback surfaces as a
	 * typed `cancelled` lifecycle marker rather than a `TokenResult`.
	 */
	waitAndExchange: () => Promise<LoopbackFlowWaitResult>;
}

export interface LoopbackFlowUnavailable {
	type: "unavailable";
	/**
	 * `listener_unavailable`: the callback port could not be bound, so no
	 * browser was opened and there is no URL worth handing back.
	 *
	 * A browser that will not launch is deliberately NOT one of these. The
	 * session is still usable then, and closing it would strand a user who was
	 * one paste away from finishing — see {@link LoopbackFlowReady.browserOpened}.
	 */
	lifecycle: "listener_unavailable";
}

export interface LoopbackFlowCancelled {
	type: "cancelled";
	lifecycle: "callback_timeout_or_cancelled";
}

export type LoopbackFlowSession = LoopbackFlowReady | LoopbackFlowUnavailable;
export type LoopbackFlowWaitResult = TokenResult | LoopbackFlowCancelled;

type CallbackObservation =
	| { type: "code"; code: string }
	| { type: "cancelled" }
	| { type: "wait_failed"; error: unknown };

const DEFAULT_DEPS: LoopbackFlowDeps = {
	createAuthorizationFlow: defaultCreateAuthorizationFlow,
	startLocalOAuthServer: defaultStartLocalOAuthServer,
	openBrowserUrl: defaultOpenBrowserUrl,
	exchangeAuthorizationCode: defaultExchangeAuthorizationCode,
};

/**
 * Start one loopback OAuth session for either the automatic or manual browser
 * choice. See the module docstring for the full lifecycle contract.
 */
export async function startLoopbackFlow(
	options: LoopbackFlowOptions,
): Promise<LoopbackFlowSession> {
	const deps: LoopbackFlowDeps = {
		...DEFAULT_DEPS,
		...(options.deps ?? {}),
	};

	const flow = await deps.createAuthorizationFlow(
		options.forceNewLogin ? { forceNewLogin: true } : undefined,
	);
	// A throw is the same outcome as a listener that never bound, and this
	// module's contract is a typed marker rather than an exception: without the
	// catch, an `http.createServer` failure escapes the OpenCode auth method as
	// an unhandled rejection instead of the "retry with Device Code / Manual URL
	// Paste" guidance the caller maps `listener_unavailable` to.
	let server: OAuthServerInfo | null = null;
	try {
		server = await deps.startLocalOAuthServer({ state: flow.state });
	} catch {
		server = null;
	}

	if (!server?.ready) {
		try {
			server?.close();
		} catch {
			// listener never bound: close is best-effort and non-fatal
		}
		return {
			type: "unavailable",
			lifecycle: "listener_unavailable",
		};
	}
	const readyServer = server;

	let closed = false;
	const closeOnce = (): void => {
		if (closed) return;
		closed = true;
		try {
			readyServer.close();
		} catch {
			// a listener that will not close must not mask the outcome the
			// caller is waiting on; the close contract is best-effort
		}
	};

	const callbackOutcome: Promise<CallbackObservation> = (async () => {
		try {
			const callback = await readyServer.waitForCode(flow.state);
			return callback
				? { type: "code", code: callback.code }
				: { type: "cancelled" };
		} catch (error) {
			return { type: "wait_failed", error };
		} finally {
			closeOnce();
		}
	})();

	// A failed launch does not end the session. The listener is bound and its
	// deadline is already running, so the user can still finish by opening the
	// URL themselves — which is exactly what happens on a Linux/WSL box with no
	// `xdg-open` on PATH, where `openBrowserUrl` returns false for every login.
	// Closing here would take that away and leave nothing to retry with.
	let browserOpened = true;
	if (options.openBrowser) {
		try {
			browserOpened = deps.openBrowserUrl(flow.url);
		} catch {
			browserOpened = false;
		}
	}

	const waitAndExchange = async (): Promise<LoopbackFlowWaitResult> => {
		const outcome = await callbackOutcome;
		if (outcome.type === "cancelled") {
			return {
				type: "cancelled",
				lifecycle: "callback_timeout_or_cancelled",
			};
		}
		if (outcome.type === "wait_failed") {
			throw outcome.error;
		}
		try {
			return await deps.exchangeAuthorizationCode(
				outcome.code,
				flow.pkce.verifier,
				REDIRECT_URI,
			);
		} finally {
			closeOnce();
		}
	};

	return {
		type: "ready",
		url: flow.url,
		browserOpened,
		close: closeOnce,
		waitAndExchange,
	};
}
