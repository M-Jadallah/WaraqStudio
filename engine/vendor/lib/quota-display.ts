/**
 * How a quota percentage is worded on the surfaces a person reads.
 *
 * Codex reports the headroom an account still has, so `free` is the default
 * and the wording every surface here already used. `used` inverts it for
 * people who track consumption rather than headroom.
 *
 * This governs presentation only. Exhaustion, rotation blocks, notification
 * thresholds and the warning/danger colouring all stay keyed on the remaining
 * percentage, because those decisions are about headroom regardless of how the
 * number is worded.
 *
 * This module is deliberately a leaf (no imports): the prompt status line
 * (`lib/tui-status.ts`) and the usage surfaces (`lib/codex-usage.ts`) both
 * depend on it, and it must not drag either into the other.
 */

export type QuotaDisplayMode = "free" | "used";

export const QUOTA_DISPLAY_MODES: readonly QuotaDisplayMode[] = ["free", "used"];

export const DEFAULT_QUOTA_DISPLAY_MODE: QuotaDisplayMode = "free";

/**
 * The number to print for a window, given the percentage still free.
 *
 * The used figure is derived from the free one rather than from the raw
 * `used_percent` the backend sent, so the two readings of one window always
 * add up to 100. Rounding them independently would let the same window render
 * as `88% left` on one surface and `13% used` on another in one session.
 */
export function toQuotaDisplayPercent(
	leftPercent: number,
	mode: QuotaDisplayMode,
): number {
	return mode === "used" ? 100 - leftPercent : leftPercent;
}

/**
 * `72%` — for surfaces with no room to name what the number counts.
 *
 * The compact prompt status line and the desktop notification both print a
 * bare percentage today, and they keep doing so in either mode: their budget
 * is spent on the account hint and the reset time, and the mode is an explicit
 * opt-in rather than something a reader has to infer per line.
 */
export function formatQuotaPercent(
	leftPercent: number,
	mode: QuotaDisplayMode,
): string {
	return `${toQuotaDisplayPercent(leftPercent, mode)}%`;
}

/** `72% left` / `28% used` — wherever there is room to say which it is. */
export function formatNamedQuotaPercent(
	leftPercent: number,
	mode: QuotaDisplayMode,
): string {
	return `${formatQuotaPercent(leftPercent, mode)} ${
		mode === "used" ? "used" : "left"
	}`;
}
