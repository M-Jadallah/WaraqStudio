export const REQUIRED_OAUTH_SCOPES = [
	"openid",
	"profile",
	"email",
	"offline_access",
] as const;

export const SCOPE = REQUIRED_OAUTH_SCOPES.join(" ");

function parseOAuthScope(scope: string | undefined): Set<string> {
	return new Set(
		(scope ?? "")
			.split(/\s+/)
			.map((entry) => entry.trim())
			.filter((entry) => entry.length > 0),
	);
}

/**
 * Canonicalizes a raw scope string: trims, collapses whitespace, drops
 * duplicates, and returns `undefined` when nothing is left. Blank and absent
 * scope must be indistinguishable, because an empty string that survives into
 * storage overwrites known-good metadata through `scope ?? existing` chains
 * (issue #213).
 */
export function normalizeScope(scope: string | undefined): string | undefined {
	const entries = [...parseOAuthScope(scope)];
	return entries.length > 0 ? entries.join(" ") : undefined;
}

export function getMissingRequiredOAuthScopes(scope: string | undefined): string[] {
	const granted = parseOAuthScope(scope);
	return REQUIRED_OAUTH_SCOPES.filter((required) => !granted.has(required));
}

export function hasRequiredOAuthScopes(scope: string | undefined): boolean {
	return getMissingRequiredOAuthScopes(scope).length === 0;
}
