import { computeWeightedLeftPercent } from "./quota-capacity.js";
import type { QuotaOverviewAccount, QuotaOverviewRecovery, QuotaOverviewWindow } from "./quota-overview.js";

function isFutureReset(window: QuotaOverviewWindow, now: number): boolean {
	return typeof window.leftPercent === "number" &&
		Number.isFinite(window.leftPercent) && window.leftPercent >= 0 && window.leftPercent < 100 &&
		typeof window.resetAtMs === "number" && Number.isFinite(window.resetAtMs) && window.resetAtMs > now;
}

export function resolveNextQuotaRecovery(
	accounts: readonly QuotaOverviewAccount[],
	now: number,
): QuotaOverviewRecovery | undefined {
	const current = computeWeightedLeftPercent(accounts);
	if (current === undefined) return undefined;
	let earliest: number | undefined;
	for (const account of accounts) {
		for (const window of account.windows) {
			if (isFutureReset(window, now) && window.resetAtMs !== undefined) {
				earliest = Math.min(earliest ?? Infinity, window.resetAtMs);
			}
		}
	}
	if (earliest === undefined) return undefined;
	const atMs = earliest;
	const refilled = accounts.map((account) => ({
		...account,
		windows: account.windows.map((window) =>
			// Only readable future resets refill: an unreadable window would add
			// an account `current` never counted, and a past reset is not news.
			isFutureReset(window, now) && window.resetAtMs !== undefined && window.resetAtMs <= atMs
				? { ...window, leftPercent: 100 } : window),
	}));
	const recovered = computeWeightedLeftPercent(refilled);
	if (recovered === undefined || recovered - current < 1) return undefined;
	return { atMs, deltaPercent: recovered - current };
}

/** Simulate known ordinary windows once; deltas telescope rather than repeating cumulative gains. */
export function resolveQuotaRecoveryEvents(
	accounts: readonly QuotaOverviewAccount[],
	now: number = Date.now(),
): QuotaOverviewRecovery[] {
	let previous = computeWeightedLeftPercent(accounts);
	if (previous === undefined) return [];
	const timestamps = new Set<number>();
	for (const account of accounts) {
		for (const window of account.windows) {
			if (isFutureReset(window, now) && window.resetAtMs !== undefined) timestamps.add(window.resetAtMs);
		}
	}
	let simulated = accounts;
	const events: QuotaOverviewRecovery[] = [];
	for (const atMs of [...timestamps].sort((left, right) => left - right)) {
		simulated = simulated.map((account) => ({
			...account,
			windows: account.windows.map((window) =>
				window.resetAtMs === atMs && isFutureReset(window, now)
					? { ...window, leftPercent: 100 }
					: window),
		}));
		const recovered = computeWeightedLeftPercent(simulated);
		if (recovered === undefined) continue;
		const deltaPercent = recovered - previous;
		previous = recovered;
		if (deltaPercent > 0) events.push({ atMs, deltaPercent });
	}
	return events;
}
