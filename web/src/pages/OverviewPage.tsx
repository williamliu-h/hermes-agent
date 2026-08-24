/**
 * Overview — the dashboard landing page.
 *
 * One glanceable answer to "what is the state of my Hermes install?", built
 * from ``GET /api/overview``. Every card is a *summary* plus a deep link into
 * the page that owns the detail (Analytics, Channels, Cron, Keys, System) —
 * deliberately not a second copy of those pages. The one place with real depth
 * here is the usage/cost drill-down, because no other page breaks spend down
 * per session.
 *
 * Notes rendered under each card come from the backend verbatim (missing CLI,
 * "estimate only", never-probed) so caveats can't drift out of sync with the
 * data that motivated them.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  Activity,
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock,
  DollarSign,
  ExternalLink,
  Mail,
  MessageSquare,
  Radio,
  RefreshCw,
  Server,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { api } from "@/lib/api";
import type {
  OverviewResponse,
  OverviewSection,
  OverviewSessionRow,
  OverviewSessionsResponse,
  OverviewStatus,
} from "@/lib/api";
import { useTableSort } from "@/lib/table-sort";
import { cn, themedBody } from "@/lib/utils";

// ── formatting ───────────────────────────────────────────────────────────────

const DAY_OPTIONS = [7, 30, 90];

function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString();
}

/** Compact token counts — cache reads reach the tens of millions here. */
function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n === 0) return "$0.00";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const delta = Date.now() - then;
  if (delta < 0) {
    const ahead = Math.abs(delta);
    // Timestamps a little in the future are clock skew between the server and
    // the browser (common when the dashboard is proxied from another host),
    // not a real future event — report those as "just now" rather than "in
    // <1m". Genuinely scheduled times (a cron next_run) are minutes+ away.
    if (ahead < 90_000) return "just now";
    if (ahead < 3_600_000) return `in ${Math.round(ahead / 60_000)}m`;
    if (ahead < 86_400_000) return `in ${Math.round(ahead / 3_600_000)}h`;
    return new Date(iso).toLocaleString();
  }
  if (delta < 60_000) return "just now";
  if (delta < 3_600_000) return `${Math.round(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.round(delta / 3_600_000)}h ago`;
  return new Date(iso).toLocaleString();
}

// ── status presentation ──────────────────────────────────────────────────────

const STATUS_TONE: Record<
  OverviewStatus,
  "success" | "warning" | "destructive" | "secondary"
> = {
  ok: "success",
  warn: "warning",
  error: "destructive",
  unknown: "secondary",
};

const STATUS_GLYPH: Record<OverviewStatus, string> = {
  ok: "✓",
  warn: "⚠",
  error: "✗",
  unknown: "?",
};

const STATUS_LABEL: Record<OverviewStatus, string> = {
  ok: "healthy",
  warn: "needs attention",
  error: "error",
  unknown: "unknown",
};

function StatusBadge({ status }: { status: OverviewStatus }) {
  return (
    <Badge tone={STATUS_TONE[status]} className="shrink-0 uppercase">
      <span aria-hidden="true">{STATUS_GLYPH[status]}</span>
      <span className="ml-1">{STATUS_LABEL[status]}</span>
    </Badge>
  );
}

// ── detail shapes (narrowed from the section's loose `detail` bag) ───────────

interface CoreDetail {
  version?: string;
  release_date?: string;
  install_method?: string | null;
  hermes_home?: string;
  config_path?: string;
  active_profile?: string;
  gateway?: {
    running?: boolean;
    state?: string | null;
    pid?: number | null;
    uptime_seconds?: number | null;
    active_agents?: number | null;
    code_version?: string | null;
    code_sha?: string | null;
    exit_reason?: string | null;
    updated_at?: string | null;
  };
  config?: {
    model_provider?: string | null;
    model_default?: string | null;
    reasoning_effort?: string | null;
    max_turns?: number | null;
    terminal_backend?: string | null;
    approvals_mode?: string | null;
    prompt_cache_ttl?: string | null;
    config_version?: number | null;
  };
}

interface ClaudeDetail {
  installed?: boolean;
  logged_in?: boolean | null;
  auth_method?: string | null;
  api_provider?: string | null;
  email?: string | null;
  org_name?: string | null;
  subscription_type?: string | null;
  cli_version?: string | null;
  delegated_worktrees?: number | null;
}

interface CodexDetail {
  installed?: boolean;
  logged_in?: boolean | null;
  auth_mode?: string | null;
  last_refresh?: string | null;
  last_refresh_age_days?: number | null;
  has_api_key?: boolean;
  has_access_token?: boolean;
  has_refresh_token?: boolean;
  cli_version?: string | null;
}

interface UsageDetail {
  period_days?: number;
  totals?: {
    sessions?: number;
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    reasoning_tokens?: number;
    estimated_cost_usd?: number;
    actual_cost_usd?: number;
    api_calls?: number;
    tool_calls?: number;
    messages?: number;
  };
  aux_estimated_cost_usd?: number;
  combined_estimated_cost_usd?: number;
  by_model?: {
    model: string;
    billing_provider: string;
    sessions: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    estimated_cost_usd: number;
  }[];
  by_task?: {
    task: string;
    model: string;
    estimated_cost_usd: number;
    input_tokens: number;
    output_tokens: number;
  }[];
  by_source?: {
    source: string;
    sessions: number;
    estimated_cost_usd: number;
    messages: number;
  }[];
  unpriced_sessions?: number;
  anthropic_credentials_configured?: boolean;
}

interface DiscordDetail {
  configured?: boolean;
  platform?: {
    state?: string | null;
    error_code?: string | null;
    error_message?: string | null;
    updated_at?: string | null;
    needs_attention?: boolean | null;
  };
  channels?: { id: string; name: string; guild?: string | null }[];
  dms?: { id: string; name: string }[];
  directory_updated_at?: string | null;
}

interface EmailDetail {
  installed?: boolean;
  accounts?: { name: string; backends: string[]; default: boolean }[];
  accounts_checked_at?: string | null;
  reachability?: {
    reachable?: boolean | null;
    elapsed_seconds?: number | null;
    reason?: string | null;
  } | null;
  reachability_checked_at?: string | null;
  reachability_stale?: boolean;
}

interface IntegrationsDetail {
  cron?: {
    available?: boolean;
    jobs?: {
      id: string;
      name: string;
      schedule?: string | null;
      state?: string | null;
      paused?: boolean;
      next_run?: string | null;
      last_run?: string | null;
      last_status?: string | null;
      last_error?: string | null;
      last_delivery_error?: string | null;
      delivery?: string | null;
      runs_completed?: number | null;
    }[];
  };
  credentials?: {
    providers?: {
      provider: string;
      count: number;
      entries: {
        index: number;
        label?: string | null;
        auth_type?: string | null;
        token_preview?: string;
        has_refresh?: boolean;
        last_status?: string | null;
      }[];
    }[];
  };
  skills?: { count?: number | null; names?: string[] };
}

function detailAs<T>(section: OverviewSection | undefined): T {
  return (section?.detail ?? {}) as T;
}

// ── layout primitives ────────────────────────────────────────────────────────

/** Label/value pair. Values are `break-all` because paths and SHAs are long. */
function Field({
  label,
  value,
  mono,
  title,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div className="min-w-0">
      <div className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={cn("truncate text-sm", mono && "font-mono text-xs")}
        title={title ?? (typeof value === "string" ? value : undefined)}
      >
        {value ?? "—"}
      </div>
    </div>
  );
}

function SectionCard({
  icon: Icon,
  title,
  section,
  linkLabel,
  onRefresh,
  refreshing,
  refreshLabel,
  className,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  section: OverviewSection | undefined;
  linkLabel?: string;
  onRefresh?: () => void;
  refreshing?: boolean;
  refreshLabel?: string;
  className?: string;
  children?: React.ReactNode;
}) {
  if (!section) return null;
  return (
    <Card className={cn("flex h-full flex-col", className)}>
      <CardContent className="flex flex-1 flex-col gap-3 py-4">
        <div className="flex items-start justify-between gap-2">
          <H2
            variant="sm"
            className="flex min-w-0 items-center gap-2 text-muted-foreground"
          >
            <Icon className="h-4 w-4 shrink-0" />
            <span className="truncate">{title}</span>
          </H2>
          <StatusBadge status={section.status} />
        </div>

        <div className="text-sm font-medium">{section.headline}</div>

        {children}

        {section.notes.length > 0 && (
          <ul className="flex flex-col gap-1">
            {section.notes.map((note, i) => (
              <li
                key={i}
                className="flex gap-1.5 text-xs leading-snug text-muted-foreground"
              >
                <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0 opacity-60" />
                <span>{note}</span>
              </li>
            ))}
          </ul>
        )}

        <div className="mt-auto flex items-center justify-between gap-2 pt-1 text-xs text-muted-foreground">
          <span
            className={cn(section.stale && "italic")}
            title={section.last_checked ?? undefined}
          >
            checked {fmtWhen(section.last_checked)}
            {section.stale && " (cached)"}
          </span>
          <span className="flex items-center gap-2">
            {onRefresh && (
              <Button
                size="sm"
                ghost
                onClick={onRefresh}
                disabled={refreshing}
                prefix={
                  refreshing ? (
                    <Spinner className="h-3 w-3" />
                  ) : (
                    <RefreshCw className="h-3 w-3" />
                  )
                }
              >
                {refreshLabel ?? "Check now"}
              </Button>
            )}
            {section.link && (
              <Link
                to={section.link}
                className="flex items-center gap-1 hover:underline"
              >
                {linkLabel ?? "Details"}
                <ChevronRight className="h-3 w-3" />
              </Link>
            )}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

// ── usage drill-down ─────────────────────────────────────────────────────────

function SortTh({
  label,
  col,
  sortKey,
  sortDir,
  toggle,
  align = "right",
}: {
  label: string;
  col: string;
  sortKey: string;
  sortDir: "asc" | "desc";
  toggle: (k: string) => void;
  align?: "left" | "right";
}) {
  const active = col === sortKey;
  return (
    <th
      onClick={() => toggle(col)}
      aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
      className={cn(
        "cursor-pointer select-none whitespace-nowrap px-2 py-1.5 font-medium",
        align === "right" ? "text-right" : "text-left",
        active ? "text-foreground" : "text-muted-foreground",
      )}
    >
      {label}
      <span className="ml-1 opacity-60">
        {active ? (sortDir === "asc" ? "▲" : "▼") : ""}
      </span>
    </th>
  );
}

function UsageDrilldown({ days }: { days: number }) {
  const [data, setData] = useState<OverviewSessionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<string>("all");

  // No setState in the effect body: the call site remounts this component via
  // `key={days}`, so the initial state (loading, no error) is already correct
  // for each window.
  useEffect(() => {
    let cancelled = false;
    api
      .getOverviewSessions(days, 200)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  const sessions = useMemo(() => data?.sessions ?? [], [data]);
  const sources = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.source))).sort(),
    [sessions],
  );
  const filtered = useMemo(
    () =>
      sourceFilter === "all"
        ? sessions
        : sessions.filter((s) => s.source === sourceFilter),
    [sessions, sourceFilter],
  );

  const { sorted, sortKey, sortDir, toggle } = useTableSort<OverviewSessionRow>(
    filtered,
    "started_at",
  );

  const filteredTotal = useMemo(
    () =>
      filtered.reduce(
        (acc, s) => acc + s.estimated_cost_usd + s.aux_estimated_cost_usd,
        0,
      ),
    [filtered],
  );

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
        <Spinner className="h-4 w-4" /> Loading sessions…
      </div>
    );
  }
  if (error) {
    return (
      <div className="py-4 text-sm text-destructive">
        Could not load sessions: {error}
      </div>
    );
  }
  if (sessions.length === 0) {
    return (
      <div className="py-4 text-sm text-muted-foreground">
        No sessions recorded in the last {days} days.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            Source
          </span>
          <Segmented
            value={sourceFilter}
            onChange={setSourceFilter}
            options={[
              { value: "all", label: `All (${sessions.length})` },
              ...sources.map((s) => ({
                value: s,
                label: `${s} (${sessions.filter((x) => x.source === s).length})`,
              })),
            ]}
          />
        </div>
        <div className="text-xs text-muted-foreground">
          {filtered.length} session(s) · {fmtUsd(filteredTotal)} est.
          {data?.truncated && " · showing newest 200"}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[54rem] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border">
              <SortTh label="Started" col="started_at" sortKey={sortKey} sortDir={sortDir} toggle={toggle} align="left" />
              <SortTh label="Session" col="title" sortKey={sortKey} sortDir={sortDir} toggle={toggle} align="left" />
              <SortTh label="Source" col="source" sortKey={sortKey} sortDir={sortDir} toggle={toggle} align="left" />
              <SortTh label="Model" col="model" sortKey={sortKey} sortDir={sortDir} toggle={toggle} align="left" />
              <SortTh label="Msgs" col="message_count" sortKey={sortKey} sortDir={sortDir} toggle={toggle} />
              <SortTh label="Tools" col="tool_call_count" sortKey={sortKey} sortDir={sortDir} toggle={toggle} />
              <SortTh label="In" col="input_tokens" sortKey={sortKey} sortDir={sortDir} toggle={toggle} />
              <SortTh label="Out" col="output_tokens" sortKey={sortKey} sortDir={sortDir} toggle={toggle} />
              <SortTh label="Cache rd" col="cache_read_tokens" sortKey={sortKey} sortDir={sortDir} toggle={toggle} />
              <SortTh label="Est. cost" col="estimated_cost_usd" sortKey={sortKey} sortDir={sortDir} toggle={toggle} />
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => {
              const total = s.estimated_cost_usd + s.aux_estimated_cost_usd;
              return (
                <tr key={s.id} className="border-b border-border/50 hover:bg-midground/5">
                  <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">
                    {fmtWhen(s.started_at_iso)}
                  </td>
                  <td className="max-w-[16rem] px-2 py-1.5">
                    <div className="truncate" title={s.title ?? s.id}>
                      {s.title || <span className="text-muted-foreground">untitled</span>}
                    </div>
                    <div className="truncate font-mono text-[10px] text-muted-foreground">
                      {s.id}
                    </div>
                  </td>
                  <td className="px-2 py-1.5">
                    <Badge tone="outline" className="whitespace-nowrap">
                      {s.source}
                      {s.chat_type ? ` · ${s.chat_type}` : ""}
                    </Badge>
                  </td>
                  <td className="max-w-[12rem] truncate px-2 py-1.5" title={s.model ?? ""}>
                    {s.model || "—"}
                  </td>
                  <td className="px-2 py-1.5 text-right">{fmtInt(s.message_count)}</td>
                  <td className="px-2 py-1.5 text-right">{fmtInt(s.tool_call_count)}</td>
                  <td className="px-2 py-1.5 text-right">{fmtTokens(s.input_tokens)}</td>
                  <td className="px-2 py-1.5 text-right">{fmtTokens(s.output_tokens)}</td>
                  <td className="px-2 py-1.5 text-right">{fmtTokens(s.cache_read_tokens)}</td>
                  <td className="px-2 py-1.5 text-right font-medium">
                    {fmtUsd(total)}
                    {s.aux_estimated_cost_usd > 0 && (
                      <div
                        className="text-[10px] font-normal text-muted-foreground"
                        title="Auxiliary (vision/compression/review) spend attributed to this session"
                      >
                        incl. {fmtUsd(s.aux_estimated_cost_usd)} aux
                      </div>
                    )}
                    {s.cost_status && s.cost_status !== "estimated" && (
                      <div className="text-[10px] font-normal text-muted-foreground">
                        {s.cost_status}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── page ─────────────────────────────────────────────────────────────────────

export default function OverviewPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** Which section keys currently have an in-flight forced refresh. */
  const [refreshing, setRefreshing] = useState<string[]>([]);
  const [showSessions, setShowSessions] = useState(false);

  const load = useCallback(
    async (refresh = "") => {
      if (refresh) {
        setRefreshing((r) => [...r, ...refresh.split(",")]);
      } else {
        setLoading(true);
      }
      try {
        const res = await api.getOverview(days, refresh);
        setData(res);
        setError(null);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
        if (refresh) {
          const keys = refresh.split(",");
          setRefreshing((r) => r.filter((k) => !keys.includes(k)));
        }
      }
    },
    [days],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const sections = data?.sections;
  const core = detailAs<CoreDetail>(sections?.core);
  const claude = detailAs<ClaudeDetail>(sections?.claude_code);
  const codex = detailAs<CodexDetail>(sections?.codex);
  const usage = detailAs<UsageDetail>(sections?.api_usage);
  const discord = detailAs<DiscordDetail>(sections?.discord);
  const email = detailAs<EmailDetail>(sections?.email);
  const integrations = detailAs<IntegrationsDetail>(sections?.integrations);

  const isRefreshing = (key: string) => refreshing.includes(key);

  if (loading && !data) {
    return (
      <div className={cn(themedBody, "flex items-center gap-2 p-6 text-sm text-muted-foreground")}>
        <Spinner className="h-4 w-4" /> Loading status…
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className={cn(themedBody, "flex flex-col gap-3 p-6")}>
        <div className="flex items-center gap-2 text-sm text-destructive">
          <AlertTriangle className="h-4 w-4" /> Could not load overview: {error}
        </div>
        <Button size="sm" onClick={() => void load()} className="self-start">
          Retry
        </Button>
      </div>
    );
  }

  const cron = integrations.cron?.jobs ?? [];
  const credProviders = integrations.credentials?.providers ?? [];
  const totals = usage.totals ?? {};

  return (
    <div className={cn(themedBody, "flex flex-col gap-5 p-4 sm:p-6")}>
      {/* ── header ─────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-3">
            <H2 variant="lg" className="font-mondwest tracking-wider">
              Overview
            </H2>
            {data && <StatusBadge status={data.overall_status} />}
          </div>
          <div className="text-xs text-muted-foreground">
            {data?.generated_at
              ? `Generated ${fmtWhen(data.generated_at)}`
              : "—"}
            {core.active_profile && ` · profile ${core.active_profile}`}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            value={String(days)}
            onChange={(v) => setDays(Number(v))}
            options={DAY_OPTIONS.map((d) => ({
              value: String(d),
              label: `${d}d`,
            }))}
          />
          <Button
            size="sm"
            onClick={() => void load("all")}
            disabled={refreshing.length > 0}
            prefix={
              refreshing.length > 0 ? (
                <Spinner className="h-3.5 w-3.5" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )
            }
          >
            Refresh all
          </Button>
        </div>
      </div>

      {error && (
        <div className="flex items-center gap-2 text-xs text-destructive">
          <AlertTriangle className="h-3.5 w-3.5" /> Last refresh failed: {error}
        </div>
      )}

      {/* ── section cards ──────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {/* § 1 Hermes core */}
        <SectionCard
          icon={Server}
          title="Hermes core"
          section={sections?.core}
          linkLabel="System"
          onRefresh={() => void load("core")}
          refreshing={isRefreshing("core")}
          refreshLabel="Recheck"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3">
            <Field label="Version" value={core.version ?? "—"} />
            <Field
              label="Install"
              value={core.install_method ?? "—"}
            />
            <Field
              label="Uptime"
              value={fmtDuration(core.gateway?.uptime_seconds)}
            />
            <Field label="PID" value={core.gateway?.pid ?? "—"} mono />
            <Field
              label="Active agents"
              value={fmtInt(core.gateway?.active_agents)}
            />
            <Field
              label="Code SHA"
              value={core.gateway?.code_sha?.slice(0, 10) ?? "—"}
              mono
              title={core.gateway?.code_sha ?? undefined}
            />
            <Field
              label="Model"
              value={core.config?.model_default ?? "—"}
              title={`${core.config?.model_provider ?? ""} / ${core.config?.model_default ?? ""}`}
            />
            <Field label="Provider" value={core.config?.model_provider ?? "—"} />
            <Field
              label="Terminal"
              value={core.config?.terminal_backend ?? "—"}
            />
            <Field label="Approvals" value={core.config?.approvals_mode ?? "—"} />
            <div className="col-span-2">
              <Field label="Hermes home" value={core.hermes_home ?? "—"} mono />
            </div>
          </div>
        </SectionCard>

        {/* § 2 Claude Code */}
        <SectionCard
          icon={Bot}
          title="Claude Code CLI"
          section={sections?.claude_code}
          onRefresh={() => void load("claude_code")}
          refreshing={isRefreshing("claude_code")}
          refreshLabel="Recheck"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3">
            <Field
              label="Logged in"
              value={
                claude.logged_in === null || claude.logged_in === undefined
                  ? "unknown"
                  : claude.logged_in
                    ? "yes"
                    : "no"
              }
            />
            <Field label="Method" value={claude.auth_method ?? "—"} />
            <Field label="Organization" value={claude.org_name ?? "—"} />
            <Field label="Plan" value={claude.subscription_type ?? "—"} />
            <Field label="API provider" value={claude.api_provider ?? "—"} />
            <Field label="CLI" value={claude.cli_version ?? "—"} />
            <div className="col-span-2">
              <Field label="Account" value={claude.email ?? "—"} mono />
            </div>
            {claude.delegated_worktrees !== null &&
              claude.delegated_worktrees !== undefined && (
                <Field
                  label="Delegated worktrees"
                  value={fmtInt(claude.delegated_worktrees)}
                />
              )}
          </div>
        </SectionCard>

        {/* § 3 ChatGPT / Codex */}
        <SectionCard
          icon={Sparkles}
          title="ChatGPT / Codex CLI"
          section={sections?.codex}
          linkLabel="Keys"
          onRefresh={() => void load("codex")}
          refreshing={isRefreshing("codex")}
          refreshLabel="Recheck"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3">
            <Field
              label="Logged in"
              value={
                codex.logged_in === null || codex.logged_in === undefined
                  ? "unknown"
                  : codex.logged_in
                    ? "yes"
                    : "no"
              }
            />
            <Field label="Auth mode" value={codex.auth_mode ?? "—"} />
            <Field label="Last refresh" value={fmtWhen(codex.last_refresh)} />
            <Field label="CLI" value={codex.cli_version ?? "—"} />
            <Field
              label="Access token"
              value={codex.has_access_token ? "present" : "absent"}
            />
            <Field
              label="Refresh token"
              value={codex.has_refresh_token ? "present" : "absent"}
            />
          </div>
        </SectionCard>

        {/* § 5 Discord */}
        <SectionCard
          icon={Radio}
          title="Discord"
          section={sections?.discord}
          linkLabel="Channels"
          onRefresh={() => void load("discord")}
          refreshing={isRefreshing("discord")}
          refreshLabel="Recheck"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3">
            <Field label="State" value={discord.platform?.state ?? "—"} />
            <Field
              label="Updated"
              value={fmtWhen(discord.platform?.updated_at)}
            />
            <Field label="Channels" value={fmtInt(discord.channels?.length)} />
            <Field label="DMs" value={fmtInt(discord.dms?.length)} />
          </div>
          {(discord.channels?.length || discord.dms?.length) && (
            <div className="flex flex-wrap gap-1.5">
              {discord.channels?.map((c) => (
                <Badge key={c.id} tone="outline" className="max-w-full">
                  <span className="truncate">
                    #{c.name}
                    {c.guild ? ` · ${c.guild}` : ""}
                  </span>
                </Badge>
              ))}
              {discord.dms?.map((d) => (
                <Badge key={d.id} tone="secondary" className="max-w-full">
                  <span className="truncate">DM · {d.name}</span>
                </Badge>
              ))}
            </div>
          )}
        </SectionCard>

        {/* § 6 Email */}
        <SectionCard
          icon={Mail}
          title="Email (himalaya)"
          section={sections?.email}
          linkLabel="Config"
          onRefresh={() => void load("email")}
          refreshing={isRefreshing("email")}
          refreshLabel="Check now"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3">
            <Field
              label="Accounts"
              value={
                email.accounts?.map((a) => a.name).join(", ") || "none"
              }
            />
            <Field
              label="Backends"
              value={
                email.accounts?.[0]?.backends?.join(", ") ?? "—"
              }
            />
            <Field
              label="IMAP reachable"
              value={
                email.reachability == null
                  ? "not checked"
                  : email.reachability.reachable
                    ? "yes"
                    : "no"
              }
            />
            <Field
              label="Last IMAP check"
              value={fmtWhen(email.reachability_checked_at)}
            />
          </div>
        </SectionCard>

        {/* § 7 Cron + credentials + skills */}
        <SectionCard
          icon={Clock}
          title="Cron, credentials & skills"
          section={sections?.integrations}
          linkLabel="Cron"
          onRefresh={() => void load("integrations")}
          refreshing={isRefreshing("integrations")}
          refreshLabel="Recheck"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3">
            <Field label="Cron jobs" value={fmtInt(cron.length)} />
            <Field
              label="Skills"
              value={fmtInt(integrations.skills?.count ?? null)}
            />
          </div>

          {cron.length > 0 && (
            <div className="flex flex-col gap-1.5">
              {cron.map((job) => {
                const bad = job.last_error || job.last_delivery_error;
                return (
                  <div
                    key={job.id}
                    className="flex items-center justify-between gap-2 text-xs"
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <Badge
                        tone={
                          job.last_error
                            ? "destructive"
                            : job.last_delivery_error
                              ? "warning"
                              : job.paused
                                ? "secondary"
                                : "success"
                        }
                        className="shrink-0"
                      >
                        {job.paused ? "paused" : (job.last_status ?? "—")}
                      </Badge>
                      <span className="truncate" title={job.name}>
                        {job.name}
                      </span>
                    </span>
                    <span className="shrink-0 text-muted-foreground">
                      {job.schedule ?? "—"} · next {fmtWhen(job.next_run)}
                    </span>
                    {bad && <span className="sr-only">{bad}</span>}
                  </div>
                );
              })}
            </div>
          )}

          {credProviders.length > 0 && (
            <div className="flex flex-col gap-1">
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Credential providers
              </div>
              <div className="flex flex-wrap gap-1.5">
                {credProviders.map((p) => (
                  <Badge key={p.provider} tone="outline">
                    {p.provider} ×{p.count}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </SectionCard>

        {/* § 4 Usage & spend — spans wide; it carries the most data */}
        <SectionCard
          icon={DollarSign}
          title={`API usage & spend · last ${days}d`}
          className="lg:col-span-2 2xl:col-span-3"
          section={sections?.api_usage}
          linkLabel="Analytics"
          onRefresh={() => void load("api_usage")}
          refreshing={isRefreshing("api_usage")}
          refreshLabel="Recheck"
        >
          <div className="grid grid-cols-2 gap-y-2.5 gap-x-3 sm:grid-cols-3 xl:grid-cols-5">
            <Field
              label="Est. spend"
              value={fmtUsd(usage.combined_estimated_cost_usd)}
            />
            <Field label="Sessions" value={fmtInt(totals.sessions)} />
            <Field label="API calls" value={fmtInt(totals.api_calls)} />
            <Field label="Input" value={fmtTokens(totals.input_tokens)} />
            <Field label="Output" value={fmtTokens(totals.output_tokens)} />
            <Field
              label="Cache read"
              value={fmtTokens(totals.cache_read_tokens)}
            />
            <Field
              label="Cache write"
              value={fmtTokens(totals.cache_write_tokens)}
            />
            <Field label="Tool calls" value={fmtInt(totals.tool_calls)} />
            <Field
              label="Aux spend"
              value={fmtUsd(usage.aux_estimated_cost_usd)}
              title="Vision, compression, background review and other non-main-agent calls"
            />
          </div>

          {(usage.by_model?.length ?? 0) > 0 && (
            <div className="flex flex-col gap-1">
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                By model
              </div>
              {usage.by_model?.slice(0, 5).map((m) => (
                <div
                  key={`${m.model}:${m.billing_provider}`}
                  className="flex items-center justify-between gap-2 text-xs"
                >
                  <span className="truncate" title={m.model}>
                    {m.model}
                    {m.billing_provider ? (
                      <span className="text-muted-foreground">
                        {" "}
                        · {m.billing_provider}
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {m.sessions} sess · {fmtUsd(m.estimated_cost_usd)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {(usage.by_task?.length ?? 0) > 0 && (
            <div className="flex flex-col gap-1">
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Auxiliary tasks
              </div>
              {usage.by_task?.slice(0, 4).map((t) => (
                <div
                  key={`${t.task}:${t.model}`}
                  className="flex items-center justify-between gap-2 text-xs"
                >
                  <span className="truncate" title={`${t.task} · ${t.model}`}>
                    {t.task}
                  </span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {fmtUsd(t.estimated_cost_usd)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {(usage.by_source?.length ?? 0) > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {usage.by_source?.map((s) => (
                <Badge key={s.source} tone="outline">
                  {s.source}: {fmtUsd(s.estimated_cost_usd)}
                </Badge>
              ))}
            </div>
          )}
        </SectionCard>
      </div>

      {/* ── per-session drill-down ─────────────────────────────────── */}
      <section className="flex flex-col gap-3">
        <button
          type="button"
          onClick={() => setShowSessions((v) => !v)}
          className="flex items-center gap-2 self-start text-left"
          aria-expanded={showSessions}
        >
          <H2
            variant="sm"
            className="flex items-center gap-2 text-muted-foreground"
          >
            <Activity className="h-4 w-4" />
            Per-session usage &amp; cost
          </H2>
          {showSessions ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
        </button>
        {showSessions ? (
          <Card>
            <CardContent className="py-4">
              <UsageDrilldown key={days} days={days} />
            </CardContent>
          </Card>
        ) : (
          <p className="text-xs text-muted-foreground">
            Expand for a sortable, filterable breakdown of every session in the
            last {days} days — tokens, tool calls and estimated cost per session.
          </p>
        )}
      </section>

      {/* ── billing caveat ─────────────────────────────────────────── */}
      <Card>
        <CardContent className="flex flex-col gap-2 py-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-2">
            <CircleHelp className="h-3.5 w-3.5" />
            <span className="font-medium text-foreground">
              About these numbers
            </span>
          </div>
          <p>
            Spend is a <strong>local estimate</strong> derived from recorded
            token counts and model pricing — it is not billing data. Anthropic
            publishes no account-balance API, so there is no way to show your
            real balance or invoice total here. For authoritative billing, see{" "}
            <a
              href="https://console.anthropic.com/settings/billing"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 underline"
            >
              console.anthropic.com
              <ExternalLink className="h-3 w-3" />
            </a>
            .
          </p>
          <p>
            Secrets are never sent to this page: credential entries are masked
            server-side and token values are never included in the API response.
          </p>
          <p className="flex items-center gap-1.5">
            <MessageSquare className="h-3.5 w-3.5" />
            The same data is available as JSON from{" "}
            <code className="font-mono">/api/overview</code> — Hermes can read
            it to summarize this panel in chat.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
