/**
 * Shared account-display helpers.
 *
 * Account identity is rendered across several UI surfaces (interactive auth
 * menu, command output, runtime/log messages, standalone CLI login menu, TUI
 * quota status). Historically each surface formatted the email independently
 * and only the TUI quota path honored the `maskEmail` config, so full emails
 * could still leak into screenshots, screen shares, and terminal recordings.
 *
 * These helpers centralize the privacy behavior so every human-facing surface
 * masks the email consistently when `maskEmail` is enabled, while preferring a
 * user-defined account label when one exists.
 */

import { createHash } from "node:crypto";

/**
 * Mask an email for display while preserving the domain so collisions between
 * accounts on the same provider remain distinguishable.
 *
 * `user@example.com` -> `us***@example.com`
 * `a@example.org`    -> `a***@example.org`
 * `not-an-email`     -> `*****`
 *
 * Returns `undefined` for empty/whitespace input so callers can fall back to
 * other identity fields.
 */
export function maskEmailForDisplay(email: string | undefined): string | undefined {
	const trimmed = email?.trim();
	if (!trimmed) return undefined;
	const atIndex = trimmed.indexOf("@");
	if (atIndex <= 0) return "*****";
	// Slice by code points, not UTF-16 units: an astral character at the
	// boundary would otherwise split into a lone surrogate.
	const prefix = Array.from(trimmed.slice(0, atIndex)).slice(0, 2).join("");
	const domain = trimmed.slice(atIndex);
	return `${prefix}***${domain}`;
}

/**
 * Resolve the email value to display for an account, applying masking when
 * requested. Returns `undefined` when there is no email to show.
 */
export function resolveDisplayEmail(
	email: string | undefined,
	maskEmail: boolean,
): string | undefined {
	const trimmed = email?.trim();
	if (!trimmed) return undefined;
	return maskEmail ? maskEmailForDisplay(trimmed) : trimmed;
}

/**
 * Head/tail mask for a stored identity value that is not an email, so a JSON
 * surface can carry a member id without disclosing more of it than the
 * `accountId` field beside it. Eight characters stay visible, which conceals
 * nothing below thirteen - `"me@x.io12"` would print all but its middle
 * character - so anything shorter is replaced outright. Input is trimmed
 * first, or `" abc "` would clear the cutoff on padding alone.
 *
 * The standalone CLI keeps its own copy as `maskValue` in
 * `scripts/install-oc-codex-multi-auth-core.js`; both surfaces disclose the
 * same fields, so keep the two in step.
 */
const IDENTITY_MASK_MIN_LENGTH = 13;
const IDENTITY_MASKED_VALUE = "*****";

export function maskIdentityValue(
	value: string | undefined,
	includeSensitive: boolean,
): string | undefined {
	if (includeSensitive) return value;
	const trimmed = value?.trim();
	if (!trimmed) return trimmed;
	if (trimmed.length < IDENTITY_MASK_MIN_LENGTH) return IDENTITY_MASKED_VALUE;
	return `${trimmed.slice(0, 4)}...${trimmed.slice(-4)}`;
}

const SEAT_SUFFIX_MIN_LENGTH = 6;
/**
 * Hard ceiling on a rendered seat, independent of how long the member id is.
 *
 * A seat sits in a table column beside the account, so its width may not be a
 * function of how long the backend's ids happen to be. Member ids measured in
 * a real multi-seat Business pool are 67 characters with no shared tail, so a
 * search that stops when the ids are separated rather than when it runs out of
 * room prints most of the id in every row.
 */
const SEAT_RENDER_MAX_LENGTH = 12;
/**
 * Hash prefix lengths for the last-resort renderer. 32 hex characters is 128
 * bits of SHA-256, so the list is exhausted only by a collision that cannot be
 * reached with ids a backend hands out.
 */
const SEAT_HASH_LENGTHS: readonly number[] = [8, 12, 16, 24, 32];
/** Marks the gap between two excerpts, as `accountId` already elides with `...`. */
const SEAT_WINDOW_SEPARATOR = "..";

function normalizeSeatIdentity(accountUserId: string | undefined): string | undefined {
	const trimmed = accountUserId?.trim();
	return trimmed && trimmed.length > 0 ? trimmed : undefined;
}

function sliceSeatSuffix(accountUserId: string, length: number): string {
	return accountUserId.length > length ? accountUserId.slice(-length) : accountUserId;
}

/**
 * `length` characters starting at `start`, slid left when the id is too short
 * to hold that window whole. Never padded: a short id renders as itself.
 */
function sliceSeatWindow(accountUserId: string, start: number, length: number): string {
	if (accountUserId.length <= length) return accountUserId;
	const begin = Math.max(0, Math.min(start, accountUserId.length - length));
	return accountUserId.slice(begin, begin + length);
}

function hashSeatIdentity(accountUserId: string, length: number): string {
	return createHash("sha256").update(accountUserId).digest("hex").slice(0, length);
}

/** Index of the first character at which the given ids are not all equal. */
function commonPrefixLength(values: readonly string[]): number {
	const [first] = values;
	if (first === undefined) return 0;
	let shared = first.length;
	for (const value of values) {
		let index = 0;
		while (index < shared && index < value.length && first[index] === value[index]) {
			index += 1;
		}
		shared = index;
		if (shared === 0) break;
	}
	return shared;
}

/** First index at which two ids differ, or their shared length if one prefixes the other. */
function firstDivergence(left: string, right: string): number {
	const limit = Math.min(left.length, right.length);
	let index = 0;
	while (index < limit && left[index] === right[index]) index += 1;
	return index;
}

/**
 * For every pair of ids, the first index at which that pair differs.
 *
 * Deliberately not "every index where the ids disagree": across a handful of
 * random-looking ids that is nearly every index, which localizes nothing. A
 * pair is told apart by any excerpt covering its first divergence, so an
 * excerpt covering all of these positions tells every pair apart.
 */
function divergenceAnchors(values: readonly string[]): number[] {
	const anchors = new Set<number>();
	for (let left = 0; left < values.length; left += 1) {
		for (let right = left + 1; right < values.length; right += 1) {
			const leftValue = values[left];
			const rightValue = values[right];
			if (leftValue === undefined || rightValue === undefined) continue;
			anchors.add(firstDivergence(leftValue, rightValue));
		}
	}
	return [...anchors].sort((left, right) => left - right);
}

/** Starts of `width`-wide windows covering `anchors`, dropping those an earlier window already spans. */
function anchorWindowStarts(anchors: readonly number[], width: number): number[] {
	const starts: number[] = [];
	for (const anchor of anchors) {
		const last = starts[starts.length - 1];
		if (last !== undefined && anchor < last + width) continue;
		starts.push(anchor);
	}
	return starts;
}

/**
 * A renderer that gives every distinct member id in `accountUserIds` a
 * different string, short enough to sit in a column beside the account.
 *
 * Four strategies, each capped, tried in order:
 *
 *  1. A tail. This is what the surfaces already print for `accountId`, so it
 *     is preferred wherever it works, which is wherever the ids differ near
 *     their end.
 *  2. One window anchored where the ids first diverge, for ids that share a
 *     long tail.
 *  3. Short windows at each position where some pair first differs, joined by
 *     `..`. Member ids in the one real multi-seat Business pool this was
 *     measured against diverge in more than one place - clusters 26 characters
 *     apart - so no single capped window separates them, and joining excerpts
 *     is what keeps the seat both bounded and readable off the id.
 *  4. A SHA-256 prefix. This is NOT an unreachable branch kept for tidiness:
 *     it is what remains when the divergences are too many or too spread out
 *     for (3) to cover inside the cap. What it prints is opaque - it cannot be
 *     matched against the id by eye - so any surface documenting the seat has
 *     to say this outcome exists.
 *
 * Returning the id whole is kept as the final fallback so two distinct ids can
 * never render alike; reaching it needs a 128-bit SHA-256 prefix collision.
 */
function resolveSeatRenderer(
	accountUserIds: readonly (string | undefined)[],
): (accountUserId: string) => string {
	const distinct: string[] = [];
	const seen = new Set<string>();
	for (const accountUserId of accountUserIds) {
		const normalized = normalizeSeatIdentity(accountUserId);
		if (!normalized || seen.has(normalized)) continue;
		seen.add(normalized);
		distinct.push(normalized);
	}
	const tailAtMinLength = (accountUserId: string) =>
		sliceSeatSuffix(accountUserId, SEAT_SUFFIX_MIN_LENGTH);
	if (distinct.length <= 1) return tailAtMinLength;

	const separates = (render: (accountUserId: string) => string): boolean =>
		new Set(distinct.map(render)).size === distinct.length;

	for (let length = SEAT_SUFFIX_MIN_LENGTH; length <= SEAT_RENDER_MAX_LENGTH; length += 1) {
		const render = (accountUserId: string) => sliceSeatSuffix(accountUserId, length);
		if (separates(render)) return render;
	}

	const start = commonPrefixLength(distinct);
	for (let length = SEAT_SUFFIX_MIN_LENGTH; length <= SEAT_RENDER_MAX_LENGTH; length += 1) {
		const render = (accountUserId: string) => sliceSeatWindow(accountUserId, start, length);
		if (separates(render)) return render;
	}

	const anchors = divergenceAnchors(distinct);
	for (let width = 2; width <= SEAT_RENDER_MAX_LENGTH; width += 1) {
		const starts = anchorWindowStarts(anchors, width);
		const rendered =
			starts.length * width + (starts.length - 1) * SEAT_WINDOW_SEPARATOR.length;
		// Skipped, not abandoned: a wider window can span two nearby anchors
		// that needed one window each, so the cost falls as the window count
		// does. Anchors at {5,6,7,31,32,33} cost 14 at width 2 (four windows)
		// and 8 at width 3 (two), so giving up at the first overflow loses a
		// rendering that fits comfortably.
		if (rendered > SEAT_RENDER_MAX_LENGTH) continue;
		const render = (accountUserId: string) =>
			starts
				.map((windowStart) => accountUserId.slice(windowStart, windowStart + width))
				.join(SEAT_WINDOW_SEPARATOR);
		if (separates(render)) return render;
	}

	for (const length of SEAT_HASH_LENGTHS) {
		const render = (accountUserId: string) => hashSeatIdentity(accountUserId, length);
		if (separates(render)) return render;
	}

	return (accountUserId: string) => accountUserId;
}

/**
 * Render the short seat suffix that separates two accounts sharing one
 * workspace `accountId`.
 *
 * A ChatGPT Business workspace is a single `accountId` shared by every member
 * of it; `accountUserId` is that member's own id and the only stored field
 * that tells their seats apart. Upstream meters each seat separately - its own
 * quota, its own weekly reset - so seats sharing a workspace are distinct
 * accounts, not copies of one.
 *
 * Every display surface used to render `accountId` alone, so four members of
 * one Business workspace printed an identical `id:` string and read as the
 * same account duplicated four times. Appending this suffix is what makes the
 * rendered rows match the accounts they describe.
 *
 * A six-character tail by default, matching what the surfaces already print
 * for `accountId`. Six characters are not an identity on their own - member
 * ids sharing a six-character tail were observed, which is the same false
 * "these are duplicates" reading this suffix exists to prevent - so pass
 * `peerAccountUserIds` (the other accounts rendered alongside this one) and
 * {@link resolveSeatRenderer} picks a rendering that separates them inside
 * {@link SEAT_RENDER_MAX_LENGTH}: a wider or relocated excerpt of the id
 * where one fits, and an opaque hash prefix where none does.
 *
 * Returns `undefined` when there is no member id, so a token-only record
 * renders exactly as it did before.
 */
export function formatSeatSuffix(
	accountUserId: string | undefined,
	peerAccountUserIds?: readonly (string | undefined)[],
): string | undefined {
	const trimmed = normalizeSeatIdentity(accountUserId);
	if (!trimmed) return undefined;
	if (!peerAccountUserIds) return sliceSeatSuffix(trimmed, SEAT_SUFFIX_MIN_LENGTH);
	// This id joins the set the rendering is chosen against, so the guarantee
	// holds even for a caller whose peer list is the OTHER accounts rather than
	// all of them. Already being there makes it a no-op.
	return resolveSeatRenderer([...peerAccountUserIds, trimmed])(trimmed);
}

/**
 * Seat suffixes for a whole rendered set, all built the same way so the rows
 * line up and no two distinct member ids share a rendering. Entries without a
 * member id come back `undefined`, holding their position.
 */
export function resolveSeatSuffixes(
	accountUserIds: readonly (string | undefined)[],
): (string | undefined)[] {
	const render = resolveSeatRenderer(accountUserIds);
	return accountUserIds.map((accountUserId) => {
		const trimmed = normalizeSeatIdentity(accountUserId);
		return trimmed ? render(trimmed) : undefined;
	});
}

/**
 * Whether a member id may be shown as a seat suffix next to masked fields.
 * The suffix is a bounded excerpt of the id, so on a short id it could be
 * most of the id itself - more than `maskIdentityValue` discloses of the same
 * value. Masked output shows the seat only when the id is long enough that
 * {@link SEAT_RENDER_MAX_LENGTH} characters cannot be the whole thing.
 *
 * The standalone CLI keeps its own copy as `seatIsDisclosable`; keep the two
 * in step.
 */
export function seatIsDisclosable(
	accountUserId: string | undefined,
	includeSensitive: boolean,
): boolean {
	if (!accountUserId) return false;
	return includeSensitive || accountUserId.length >= IDENTITY_MASK_MIN_LENGTH;
}
