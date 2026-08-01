import type { ReactNode } from "react";

export function EmptyState({
  title,
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      {title ? <p className="empty-title">{title}</p> : null}
      {children ? <p className="empty-body">{children}</p> : null}
    </div>
  );
}

/**
 * Seeded rows are always visibly distinguishable from real processing.
 *
 * The demo dataset exists so the product is explorable from a cold start; a
 * listing that cannot say which rows are ours is a listing that turns that into
 * a cost.
 */
export function SeedBadge({ isSeed }: { isSeed?: boolean }) {
  if (!isSeed) return null;
  return (
    <span className="badge badge-seed" title="Demo data, created by `make seed`">
      seed
    </span>
  );
}
