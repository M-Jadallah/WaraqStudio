/**
 * Read the host's own `openai` OAuth credential from OpenCode's auth store.
 *
 * The plugin backfills that entry from the account pool (see
 * `backfillHostOpenAIAuthFromPool` in index.ts) and the host refreshes its
 * copy independently — so it can serve requests entirely outside pool
 * rotation, including while every pool account is disabled. Diagnostics
 * surface its presence so "traffic works but the pool says disabled" is
 * explainable instead of a paradox (#288).
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

import { PROVIDER_ID } from "./constants.js";

export interface HostOAuthEntry {
	access: string;
	refresh: string;
	expires: number;
	scope?: string;
}

export function hostAuthFilePath(): string {
	// The host reads its auth store from XDG_DATA_HOME when set — mirrored
	// from the backfill path in index.ts so both look at the same file.
	const xdgDataHome = process.env.XDG_DATA_HOME?.trim();
	return join(
		xdgDataHome && isAbsolute(xdgDataHome)
			? xdgDataHome
			: join(homedir(), ".local", "share"),
		"opencode",
		"auth.json",
	);
}

export async function readHostOpenAIOAuth(
	providerId: string = PROVIDER_ID,
): Promise<HostOAuthEntry | null> {
	let parsed: unknown;
	try {
		parsed = JSON.parse(await readFile(hostAuthFilePath(), "utf8"));
	} catch {
		return null;
	}
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		return null;
	}
	const entry = (parsed as Record<string, unknown>)[providerId];
	if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
		return null;
	}
	const record = entry as Record<string, unknown>;
	if (
		record.type !== "oauth" ||
		typeof record.access !== "string" ||
		record.access.trim().length === 0 ||
		typeof record.refresh !== "string" ||
		record.refresh.trim().length === 0 ||
		typeof record.expires !== "number" ||
		!Number.isFinite(record.expires)
	) {
		return null;
	}
	return {
		access: record.access,
		refresh: record.refresh,
		expires: record.expires,
		scope: typeof record.scope === "string" ? record.scope : undefined,
	};
}
