/**
 * Where this plugin is running from, and where it has run from before.
 *
 * OpenCode can load the plugin from the published package or from a checkout
 * a developer points it at, and the two are indistinguishable at runtime
 * unless the plugin asks. Knowing which one is live decides whether offering
 * an update makes sense, and a record of previous origins is what turns "my
 * edits stopped taking effect" into a diagnosable event.
 */

import { existsSync, readFileSync } from "node:fs";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { lock } from "proper-lockfile";
import { createLogger } from "./logger.js";
import { renameWithWindowsRetry } from "./storage/atomic-write.js";

const log = createLogger("plugin-origin");

/**
 * The origin history file name, shared with the installer.
 *
 * The installer is plain JS that must run before anything is built, so it
 * cannot import this module and declares the same name itself. A test pins the
 * two together, because a silent disagreement would leave each side reporting
 * confidently about a file the other never writes.
 */
export const HISTORY_FILE_NAME = "oc-codex-multi-auth-origin.json";
const HISTORY_VERSION = 1;
const MAX_SIGHTINGS = 10;
const PACKAGE_ROOT_LOOKUP_DEPTH = 3;

/** The lease covers one small read and one small write, so it is short. */
const HISTORY_LOCK_STALE_MS = 10_000;
const HISTORY_LOCK_UPDATE_MS = 2_000;
const HISTORY_LOCK_RETRIES = {
	retries: 10,
	factor: 1.8,
	minTimeout: 50,
	maxTimeout: 500,
	randomize: true,
} as const;

const MANAGED_PACKAGE_NAMES = new Set(["oc-codex-multi-auth", "oc-chatgpt-multi-auth"]);

function isManagedPackageName(name: string): boolean {
	return MANAGED_PACKAGE_NAMES.has(name.toLowerCase());
}

function defaultCacheDirectory(home: string = homedir()): string {
	return join(home, ".cache", "opencode");
}

export interface PluginOrigin {
	name: string;
	version: string;
	root: string;
	isLocalCheckout: boolean;
}

export interface PluginOriginSighting extends PluginOrigin {
	firstSeen: string;
	lastSeen: string;
}

export interface PluginOriginHistory {
	version: number;
	sightings: PluginOriginSighting[];
}

interface PackageManifest {
	name?: unknown;
	version?: unknown;
}

function emptyHistory(): PluginOriginHistory {
	return { version: HISTORY_VERSION, sightings: [] };
}

function readPackageManifest(directory: string): PackageManifest | null {
	try {
		const parsed: unknown = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
		return parsed as PackageManifest;
	} catch {
		return null;
	}
}

function manifestName(manifest: PackageManifest | null): string | null {
	const name = manifest?.name;
	return typeof name === "string" && name.trim() ? name.trim() : null;
}

/** Built output lives inside the package, so the root is at or above it. */
export function findPackageRoot(startDirectory: string): string | null {
	let current = resolve(startDirectory);
	for (let depth = 0; depth <= PACKAGE_ROOT_LOOKUP_DEPTH; depth += 1) {
		if (manifestName(readPackageManifest(current))) return current;
		const parent = dirname(current);
		if (parent === current) return null;
		current = parent;
	}
	return null;
}

function pathSegments(path: string): string[] {
	return path.replaceAll("\\", "/").replace(/\/+$/, "").split("/").filter(Boolean);
}

function foldPathCase(value: string, platform: NodeJS.Platform): string {
	return platform === "win32" || platform === "darwin" ? value.toLowerCase() : value;
}

function isInsideDirectory(
	candidate: string,
	directory: string,
	platform: NodeJS.Platform,
): boolean {
	const relativePath = relative(
		foldPathCase(resolve(directory), platform),
		foldPathCase(resolve(candidate), platform),
	);
	return relativePath !== "" && !relativePath.startsWith("..") && !isAbsolute(relativePath);
}

/**
 * How two spellings of a root are told apart. Windows reaches one directory
 * under several of them, and so does a default-case-insensitive APFS volume,
 * so comparing verbatim there would let `C:\Repo` and `c:\repo` - or `/Repo`
 * and `/repo` - occupy two slots in a bounded history and evict a genuinely
 * different origin between them.
 */
function rootComparisonKey(root: string, platform: NodeJS.Platform = process.platform): string {
	const normalized = root.replaceAll("\\", "/").replace(/\/+$/, "");
	return foldPathCase(normalized, platform);
}

/**
 * A root a package manager chose, as opposed to one a human did. The version
 * suffix is what separates OpenCode's plugin cache from a monorepo that merely
 * keeps its packages in a `packages/` directory.
 */
export function isPackageManagerRoot(
	root: string,
	platform: NodeJS.Platform = process.platform,
	cacheDirectory: string = defaultCacheDirectory(),
): boolean {
	const segments = pathSegments(root).map((segment) => foldPathCase(segment, platform));
	if (
		segments.some(
			(segment, index) =>
				segment === "node_modules" ||
				(segments[index - 1] === "packages" && segment.includes("@")),
		)
	) {
		return true;
	}
	return isInsideDirectory(root, cacheDirectory, platform);
}

export function resolvePluginOrigin(moduleUrl: string): PluginOrigin | null {
	let moduleDirectory: string;
	try {
		moduleDirectory = dirname(fileURLToPath(moduleUrl));
	} catch {
		return null;
	}

	const root = findPackageRoot(moduleDirectory);
	if (!root) return null;

	const manifest = readPackageManifest(root);
	const name = manifestName(manifest);
	if (!name || !isManagedPackageName(name)) return null;
	const version = typeof manifest?.version === "string" ? manifest.version : "0.0.0";

	return { name, version, root, isLocalCheckout: !isPackageManagerRoot(root) };
}

let cachedOrigin: PluginOrigin | null | undefined;

/** The origin of this running build. Fixed for the life of the process. */
export function getPluginOrigin(): PluginOrigin | null {
	if (cachedOrigin === undefined) cachedOrigin = resolvePluginOrigin(import.meta.url);
	return cachedOrigin;
}

export function getPluginOriginHistoryPath(home: string = homedir()): string {
	return join(home, ".opencode", HISTORY_FILE_NAME);
}

function isSighting(value: unknown): value is PluginOriginSighting {
	if (!value || typeof value !== "object" || Array.isArray(value)) return false;
	const candidate = value as Record<string, unknown>;
	return (
		typeof candidate.name === "string" &&
		typeof candidate.version === "string" &&
		typeof candidate.root === "string" &&
		typeof candidate.isLocalCheckout === "boolean" &&
		typeof candidate.firstSeen === "string" &&
		typeof candidate.lastSeen === "string"
	);
}

export function readPluginOriginHistory(
	historyPath: string = getPluginOriginHistoryPath(),
): PluginOriginHistory {
	try {
		const parsed: unknown = JSON.parse(readFileSync(historyPath, "utf8"));
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return emptyHistory();
		const sightings = (parsed as { sightings?: unknown }).sightings;
		if (!Array.isArray(sightings)) return emptyHistory();
		return { version: HISTORY_VERSION, sightings: sightings.filter(isSighting) };
	} catch {
		return emptyHistory();
	}
}

function byLastSeen(left: PluginOriginSighting, right: PluginOriginSighting): number {
	return (Date.parse(left.lastSeen) || 0) - (Date.parse(right.lastSeen) || 0);
}

/**
 * History is appended to rather than overwritten, so one origin replacing
 * another leaves both on record. A last-writer-wins marker would instead
 * confirm whatever state a clobber left behind.
 */
export function withSighting(
	history: PluginOriginHistory,
	origin: PluginOrigin,
	seenAt: string,
	platform: NodeJS.Platform = process.platform,
): PluginOriginHistory {
	// Matched on the comparison key, recorded as the origin spells it: a reader
	// is shown the path they configured, not a folded version of it.
	const key = rootComparisonKey(origin.root, platform);
	const sameRoot = (sighting: PluginOriginSighting) =>
		rootComparisonKey(sighting.root, platform) === key;
	const previous = history.sightings.find(sameRoot);
	const others = history.sightings.filter((sighting) => !sameRoot(sighting));
	const current: PluginOriginSighting = {
		...origin,
		firstSeen: previous?.firstSeen ?? seenAt,
		lastSeen: seenAt,
	};
	return {
		version: HISTORY_VERSION,
		sightings: [...others, current].slice(-MAX_SIGHTINGS),
	};
}

/**
 * Replaces the history, unless ownership was lost while the new copy was being
 * written. Only the rename is visible to anybody else, so that is where the
 * question has to be asked: checking earlier leaves the whole of `writeFile`
 * as a window in which the lease can be reclaimed and newer history written,
 * which this rename would then replace with an older snapshot.
 *
 * Returns whether the replacement happened.
 */
async function writeHistory(
	historyPath: string,
	history: PluginOriginHistory,
	stillOwned: () => boolean,
): Promise<boolean> {
	const temporaryPath = `${historyPath}.${process.pid}.tmp`;
	await writeFile(temporaryPath, `${JSON.stringify(history, null, 2)}\n`, "utf-8");
	try {
		if (!stillOwned()) return false;
		await renameWithWindowsRetry(temporaryPath, historyPath);
		return true;
	} finally {
		if (existsSync(temporaryPath)) {
			await rm(temporaryPath, { force: true }).catch(() => undefined);
		}
	}
}

/**
 * Records this origin without dropping anybody else's.
 *
 * Every OpenCode process records at startup, and a machine running many of
 * them starts several at once, so read-modify-write on a shared file is a real
 * race rather than a theoretical one. The reader that matters here asks which
 * origins have been seen, so a lost sighting is a lost answer: the very
 * handover this file exists to report - a checkout replaced by the installed
 * package - is two different origins written at close to the same time.
 *
 * The history is therefore re-read INSIDE the lease, so each writer merges
 * into what is actually on disk. A writer that cannot take the lease records
 * nothing rather than overwriting blind; it is about to be started again, and
 * a missing sighting costs a later startup while a clobbered one costs the
 * only evidence there was. A lease reclaimed mid-write is the same situation
 * arriving later, and is answered the same way.
 */
export async function recordPluginOrigin(
	origin: PluginOrigin,
	historyPath: string = getPluginOriginHistoryPath(),
	now: () => Date = () => new Date(),
): Promise<PluginOriginHistory> {
	await mkdir(dirname(historyPath), { recursive: true });

	let compromised: Error | undefined;
	let release: (() => Promise<void>) | null = null;
	try {
		release = await lock(historyPath, {
			realpath: false,
			lockfilePath: `${historyPath}.lock`,
			stale: HISTORY_LOCK_STALE_MS,
			update: HISTORY_LOCK_UPDATE_MS,
			retries: HISTORY_LOCK_RETRIES,
			// proper-lockfile's default rethrows from the timer that refreshes the
			// lease, so it lands outside every promise chain and ends the process
			// hosting this plugin. A stalled event loop is enough to trigger it.
			// Nothing here is worth an editor closing, so the loss is recorded and
			// the write is abandoned instead.
			onCompromised: (error: Error) => {
				compromised = error;
				log.warn("The plugin origin history lease was reclaimed", {
					path: `${historyPath}.lock`,
					error: error.message,
				});
			},
		});
	} catch (error) {
		log.debug("Skipped recording the plugin origin; another process holds the history", {
			error: error instanceof Error ? error.message : String(error),
		});
		return readPluginOriginHistory(historyPath);
	}

	try {
		const next = withSighting(readPluginOriginHistory(historyPath), origin, now().toISOString());
		// Whoever reclaimed the lease owns the file now, so writing what we read
		// before they did would drop their sighting - the clobber the lease
		// exists to prevent. Asked again at the rename, since the lease can be
		// lost at any point up to it.
		if (compromised) return readPluginOriginHistory(historyPath);
		const written = await writeHistory(historyPath, next, () => !compromised);
		return written ? next : readPluginOriginHistory(historyPath);
	} finally {
		await release().catch(() => undefined);
	}
}

/**
 * The checkout this plugin used to run from and no longer does. Reported only
 * when the current origin is package-manager output, since a move between two
 * checkouts is an ordinary thing to do deliberately.
 */
export function findReplacedLocalCheckout(
	origin: PluginOrigin,
	history: PluginOriginHistory,
	platform: NodeJS.Platform = process.platform,
): PluginOriginSighting | null {
	if (origin.isLocalCheckout) return null;
	const currentKey = rootComparisonKey(origin.root, platform);
	return (
		history.sightings
			.filter(
				(sighting) =>
					isManagedPackageName(sighting.name) &&
					sighting.isLocalCheckout &&
					rootComparisonKey(sighting.root, platform) !== currentKey,
			)
			.sort(byLastSeen)
			.at(-1) ?? null
	);
}

export function describePluginOrigin(origin: PluginOrigin | null): string {
	if (!origin) return "unknown";
	return origin.isLocalCheckout
		? `local checkout at ${origin.root} (v${origin.version})`
		: `installed package v${origin.version}`;
}
