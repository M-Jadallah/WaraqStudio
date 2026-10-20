import { getPlanWeight } from "./plan-allotment.js";
import type { QuotaOverviewAccount, QuotaOverviewWindow } from "./quota-overview.js";

export function resolveGoverningWindow(
	account: QuotaOverviewAccount,
): QuotaOverviewWindow | undefined {
	let governing: QuotaOverviewWindow | undefined;
	for (const window of account.windows) {
		if (
			typeof window.leftPercent !== "number" ||
			!Number.isFinite(window.leftPercent)
		) continue;
		if (!governing || window.leftPercent < (governing.leftPercent ?? 100)) {
			governing = window;
			continue;
		}
		if (
			window.leftPercent === governing.leftPercent &&
			typeof window.resetAtMs === "number" && Number.isFinite(window.resetAtMs) &&
			(governing.resetAtMs === undefined ||
				!Number.isFinite(governing.resetAtMs) ||
				window.resetAtMs > governing.resetAtMs)
		) {
			governing = window;
		}
	}
	return governing;
}

export function computeWeightedLeftPercent(
	accounts: readonly QuotaOverviewAccount[],
	precision: "rounded" | "exact" = "rounded",
): number | undefined {
	let weighted = 0;
	let totalWeight = 0;
	for (const account of accounts) {
		const governing = resolveGoverningWindow(precision === "exact" ? {
			...account,
			windows: account.windows.map((window) => ({
				...window, leftPercent: window.exactLeftPercent ?? window.leftPercent,
			})),
		} : account);
		if (governing?.leftPercent === undefined) continue;
		const weight = getPlanWeight(account.planType);
		if (!Number.isFinite(weight) || weight <= 0) continue;
		weighted += weight * governing.leftPercent;
		totalWeight += weight;
	}
	if (totalWeight === 0) return undefined;
	const percent = weighted / totalWeight;
	return precision === "exact" ? percent : Math.round(percent);
}
