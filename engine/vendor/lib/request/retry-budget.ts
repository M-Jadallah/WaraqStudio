/**
 * Retry budget utilities for per-error-class retry controls.
 */

export type RetryProfile = "conservative" | "balanced" | "aggressive";

export type RetryBudgetClass =
	| "authRefresh"
	| "network"
	| "server"
	| "rateLimitShort"
	| "rateLimitGlobal"
	| "emptyResponse";

export type RetryBudgetLimits = Record<RetryBudgetClass, number>;

export type RetryBudgetOverrides = Partial<RetryBudgetLimits>;

const PROFILE_LIMITS: Record<RetryProfile, RetryBudgetLimits> = {
	conservative: {
		authRefresh: 2,
		network: 2,
		server: 2,
		rateLimitShort: 2,
		rateLimitGlobal: 1,
		emptyResponse: 1,
	},
	balanced: {
		authRefresh: 4,
		network: 4,
		server: 4,
		rateLimitShort: 4,
		rateLimitGlobal: 3,
		emptyResponse: 2,
	},
	aggressive: {
		authRefresh: 8,
		network: 8,
		server: 8,
		rateLimitShort: 8,
		rateLimitGlobal: 10,
		emptyResponse: 4,
	},
};

/**
 * How much blocking one budget unit buys when a retry is charged through
 * {@link RetryBudgetTracker.consumeWait}.
 *
 * The budgets are small (1/3/10) and were being charged one unit per wait
 * regardless of length, so three consecutive sub-second waits exhausted the
 * default and hard-failed a request that one more second would have served.
 * Waiting is only expensive in proportion to the time it costs the caller, so
 * that is what a unit now measures.
 */
export const RETRY_WAIT_BUDGET_UNIT_MS = 5_000;

const RETRY_BUDGET_CLASSES: RetryBudgetClass[] = [
	"authRefresh",
	"network",
	"server",
	"rateLimitShort",
	"rateLimitGlobal",
	"emptyResponse",
];

export function normalizeRetryBudgetValue(value: unknown): number | undefined {
	if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
	if (value < 0) return undefined;
	return Math.floor(value);
}

export function resolveRetryBudgetLimits(
	profile: RetryProfile,
	overrides?: RetryBudgetOverrides,
): RetryBudgetLimits {
	const base = PROFILE_LIMITS[profile] ?? PROFILE_LIMITS.balanced;
	const merged: RetryBudgetLimits = { ...base };
	if (!overrides) return merged;

	for (const bucket of RETRY_BUDGET_CLASSES) {
		const normalized = normalizeRetryBudgetValue(overrides[bucket]);
		if (normalized === undefined) continue;
		merged[bucket] = normalized;
	}
	return merged;
}

function createUsedCounters(): RetryBudgetLimits {
	return {
		authRefresh: 0,
		network: 0,
		server: 0,
		rateLimitShort: 0,
		rateLimitGlobal: 0,
		emptyResponse: 0,
	};
}

export class RetryBudgetTracker {
	private readonly used: RetryBudgetLimits = createUsedCounters();
	private readonly waitCarryMs: RetryBudgetLimits = createUsedCounters();
	private readonly limits: RetryBudgetLimits;

	constructor(limits: RetryBudgetLimits) {
		this.limits = { ...limits };
	}

	/**
	 * Charge a retry that blocks for `waitMs` against a bucket, in proportion to
	 * how long it blocks.
	 *
	 * A wait of {@link RETRY_WAIT_BUDGET_UNIT_MS} or longer costs a full unit,
	 * so a multi-hour block stays governed exactly as before. Shorter waits
	 * accumulate on a per-bucket carry and only cost a unit once they have added
	 * up to one, so a burst of sub-second waits is effectively free.
	 *
	 * An exhausted bucket refuses even a free wait: the carry bounds how long
	 * short waits can loop, and without that check they would loop forever once
	 * the budget ran out.
	 */
	consumeWait(bucket: RetryBudgetClass, waitMs: number): boolean {
		if (this.getRemaining(bucket) <= 0) return false;

		// A non-finite or negative wait cannot be proportioned, so it costs a
		// full unit: collapsing it to zero would let it accumulate nothing and
		// retry forever.
		if (!Number.isFinite(waitMs) || waitMs < 0) return this.consume(bucket);
		if (waitMs >= RETRY_WAIT_BUDGET_UNIT_MS) return this.consume(bucket);

		const carried = this.waitCarryMs[bucket] + waitMs;
		if (carried < RETRY_WAIT_BUDGET_UNIT_MS) {
			this.waitCarryMs[bucket] = carried;
			return true;
		}

		this.waitCarryMs[bucket] = carried - RETRY_WAIT_BUDGET_UNIT_MS;
		return this.consume(bucket);
	}

	consume(bucket: RetryBudgetClass): boolean {
		const limit = this.limits[bucket];
		if (!Number.isFinite(limit)) {
			this.used[bucket] += 1;
			return true;
		}

		if (this.used[bucket] >= limit) {
			return false;
		}

		this.used[bucket] += 1;
		return true;
	}

	getLimits(): RetryBudgetLimits {
		return { ...this.limits };
	}

	getUsage(): RetryBudgetLimits {
		return { ...this.used };
	}

	getRemaining(bucket: RetryBudgetClass): number {
		const limit = this.limits[bucket];
		if (!Number.isFinite(limit)) return Number.POSITIVE_INFINITY;
		return Math.max(0, limit - this.used[bucket]);
	}
}
