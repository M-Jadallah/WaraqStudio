import { withAccountStorageTransaction, type AccountMetadataV3 } from "../storage.js";
import { findAccountIndexByIdentity } from "../tools/refresh-account.js";
import { getModelFamily } from "../prompts/codex.js";
import { getQuotaKey } from "./rate-limits.js";
import { clearUnchangedRecoveryState } from "./stale-state.js";
import {
	fetchCodexUsage, parseCodexUsagePayload, isUsageQuotaRecovered,
	persistUsageQuotaRecovery, resolveCodexUsageAccountId,
} from "../codex-usage.js";

export interface WarmRecoveryObservation {
	account: AccountMetadataV3;
	model: string;
	accessToken: string;
}

export async function recoverWarmedAccount(
	observation: WarmRecoveryObservation,
	onCleared?: (snapshot: AccountMetadataV3) => void,
): Promise<boolean> {
	const { account, model, accessToken } = observation;
	let quotaCleared = false;
	// The suppression snapshot handed to the caller carries the full volatile
	// state observed before the warm, not just what this recovery cleared. A
	// superseded manager holding those exact pre-warm values must not
	// resurrect them over a concurrent clear, while evidence recorded after
	// the observation carries different values and still merges through.
	const clearedSnapshot: AccountMetadataV3 = { ...account,
		rateLimitResetTimes: { ...account.rateLimitResetTimes } };
	// A warm 200 may spend credits. Confirm suspected subscription recovery
	// through usage rather than treating a successful model call as free quota.
	// A usage failure withholds only the quota verdict: the model-marker
	// cleanup below is independent evidence, so it still runs and the error
	// is rethrown afterwards for separate reporting.
	let quotaError: unknown;
	if (account.quotaExhaustedUntil !== undefined) {
		try {
			const accountId = resolveCodexUsageAccountId({ account, accessToken });
			if (!accountId) throw new Error("Cannot identify warmed account for quota recovery");
			const usage = parseCodexUsagePayload(await fetchCodexUsage({
				accountId, accessToken, organizationId: account.organizationId,
			}));
			if (isUsageQuotaRecovered([usage.primary, usage.secondary])) {
				quotaCleared = await persistUsageQuotaRecovery(account);
				if (quotaCleared) onCleared?.({ ...clearedSnapshot });
			}
		} catch (error) {
			quotaError = error;
		}
	}
	const family = getModelFamily(model);
	const familyKey = getQuotaKey(family);
	const modelKey = getQuotaKey(family, model);
	const modelCleared = await withAccountStorageTransaction(async (current, persist) => {
		if (!current) return false;
		const record = current.accounts[findAccountIndexByIdentity(current.accounts, account)];
		if (!record || record.enabled === false) return false;
		// A 429 stamps both the family-wide key and the model key, and the
		// family blanket blocks the very model that just warmed, so the clear
		// scope covers both. Other models' and other families' markers stay.
		const cleared = clearUnchangedRecoveryState(record, {
			rateLimitResetTimes: {
				[familyKey]: account.rateLimitResetTimes?.[familyKey],
				[modelKey]: account.rateLimitResetTimes?.[modelKey],
			},
			coolingDownUntil: account.coolingDownUntil, cooldownReason: account.cooldownReason,
		});
		if (!cleared) return false;
		await persist(current);
		onCleared?.(clearedSnapshot);
		return true;
	});
	if (quotaError !== undefined) throw quotaError;
	return quotaCleared || modelCleared;
}
