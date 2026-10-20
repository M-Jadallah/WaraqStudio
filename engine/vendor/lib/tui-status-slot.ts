function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

/** Keep the host from ellipsizing an already-budgeted chronological forecast. */
export function protectQuotaStatusSlot(node: unknown): (() => void) | undefined {
	if (!isRecord(node) || !isRecord(node.parent)) return undefined;
	const parent = node.parent;
	const yoga = parent.yogaNode;
	if (typeof parent.getChildren !== "function" || !isRecord(yoga)) return undefined;
	const children: unknown = parent.getChildren();
	if (!Array.isArray(children) || children.length !== 1 || children[0] !== node) return undefined;
	const getFlexShrink = yoga.getFlexShrink;
	if (typeof getFlexShrink !== "function") return undefined;
	const previous: unknown = getFlexShrink.call(yoga);
	if (typeof previous !== "number" || !Number.isFinite(previous)) return undefined;
	parent.flexShrink = 0;
	return () => {
		if (getFlexShrink.call(yoga) === 0) parent.flexShrink = previous;
	};
}
