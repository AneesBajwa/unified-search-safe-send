export function EmptyState({ children }: { children: React.ReactNode }) {
  return <p className="empty muted">{children}</p>;
}

/** Seeded rows are always visibly distinguishable from real processing. */
export function SeedBadge({ isSeed }: { isSeed?: boolean }) {
  if (!isSeed) return null;
  return <span className="badge badge-seed">seed</span>;
}
