import type { SourceView } from "../api/types";

/**
 * The one-line roll-up above the merged result list.
 *
 * 🔴 It counts sources that **contributed**, not sources we asked. Those are
 * different numbers and the difference is the whole point of this surface: a
 * brand-new user with nothing connected got "3 results from 3 sources" when
 * exactly one source — the web — had returned anything and the other two were
 * not even connected. Crediting a source that contributed nothing is the same
 * class of lie as a failed source rendering like an empty one, which the status
 * chips exist to prevent.
 *
 * When every source contributed, the qualifier is noise and is dropped.
 */
export function summariseSources(resultCount: number, sources: readonly SourceView[]): string {
  const contributing = sources.filter((source) => source.result_count > 0).length;
  const results = `${resultCount} result${resultCount === 1 ? "" : "s"}`;
  const from =
    contributing === sources.length
      ? `${sources.length} source${sources.length === 1 ? "" : "s"}`
      : `${contributing} of ${sources.length} sources`;
  return `${results} from ${from}`;
}
