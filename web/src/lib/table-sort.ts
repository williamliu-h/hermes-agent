import { useCallback, useMemo, useState } from "react";

export type SortDir = "asc" | "desc";

/**
 * Client-side sort state for a data table.
 *
 * Null/undefined values always sort last regardless of direction, so a column
 * with missing values (an unfinished session's ``ended_at``, a model with no
 * pricing) never pushes empty rows to the top.
 */
export function useTableSort<T extends object>(
  data: T[],
  defaultKey: keyof T & string,
  defaultDir: SortDir = "desc",
) {
  const [sortKey, setSortKey] = useState<string>(defaultKey);
  const [sortDir, setSortDir] = useState<SortDir>(defaultDir);

  const sorted = useMemo(() => {
    return [...data].sort((a, b) => {
      const aVal = a[sortKey as keyof T];
      const bVal = b[sortKey as keyof T];
      if (aVal === null || aVal === undefined) return 1;
      if (bVal === null || bVal === undefined) return -1;
      if (aVal === bVal) return 0;
      // Strings compare case-insensitively so "Discord" and "cli" order the
      // way a reader expects rather than by ASCII codepoint.
      if (typeof aVal === "string" && typeof bVal === "string") {
        const cmp = aVal.localeCompare(bVal, undefined, { sensitivity: "base" });
        return sortDir === "asc" ? cmp : -cmp;
      }
      const cmp = aVal > bVal ? 1 : -1;
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [data, sortKey, sortDir]);

  const toggle = useCallback(
    (key: string) => {
      if (key === sortKey) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      } else {
        setSortKey(key);
        setSortDir("desc");
      }
    },
    [sortKey],
  );

  return { sorted, sortKey, sortDir, toggle };
}
