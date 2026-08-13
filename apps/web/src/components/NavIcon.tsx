/**
 * Four icons, written by hand rather than pulled from a library.
 *
 * They earn their place in the bottom tab bar, which is text-only today and is
 * the least finished surface in the app. `aria-hidden` throughout: the text
 * label beside them is the accessible name, and an icon that also announces
 * itself just says everything twice.
 */
export type NavIconName = "search" | "compose" | "history" | "accounts";

const PATHS: Record<NavIconName, string> = {
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14ZM20 20l-4.1-4.1",
  compose: "M4 20h16M6 16.5 16.8 5.7a2 2 0 0 1 2.8 2.8L8.8 19.3 4.5 20l.7-4.3Z",
  history: "M12 7v5l3.2 2M3.5 12a8.5 8.5 0 1 0 2.6-6.1M5.5 3.5v3h3",
  accounts: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4.5 20a7.5 7.5 0 0 1 15 0",
};

export function NavIcon({ name }: { name: NavIconName }) {
  return (
    <svg
      className="navicon"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
