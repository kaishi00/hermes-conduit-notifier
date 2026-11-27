(function () {
  "use strict";
  // conduit_push's backend routes serve the Conduit iOS app. The dashboard
  // gets one thing from it: a Conduit card at the top of the Sessions page
  // (slot sessions:top), beside Hermes' own Connected Platforms card, which
  // lists only the gateway's messaging adapters. It reads /link-status: is
  // the app reaching this host, and do its pushes get through the relay?
  // It also reads /sessions/plan-quota to show the Claude usage quota (the
  // same gauges the Watch relay injects into every job_news response since
  // the job-news-claude-usage-quota capability was added).
  // The tab component stays empty (the manifest hides the tab); registering
  // it keeps the dashboard from reporting the plugin as failing to load.
  var plugins = window.__HERMES_PLUGINS__;
  if (!plugins) return;
  plugins.register("conduit_push", function () {
    return null;
  });

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || typeof plugins.registerSlot !== "function") return;
  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var ui = SDK.components || {};
  var POLL_MS = 30000;
  var QUOTA_POLL_MS = 60000; // matches backend CLAUDE_QUOTA_TTL_S

  function ago(seconds) {
    if (typeof seconds !== "number") return null;
    try {
      return SDK.utils.timeAgo(seconds * 1000);
    } catch (error) {
      return null;
    }
  }

  function appRow(app) {
    var seen = ago(app.seen_at);
    if (app.state === "connected") {
      return {
        tone: "success",
        badge: "Connected",
        detail: app.open_sockets > 0 ? "The app is open on this host" : "Last seen " + (seen || "just now"),
      };
    }
    if (app.state === "idle") return { tone: "warning", badge: "Idle", detail: "Last seen " + seen };
    return { tone: "outline", badge: "Not seen", detail: "The app hasn't reached this host yet" };
  }

  function pushRow(push) {
    switch (push.state) {
      case "ok":
        return { tone: "success", badge: "Delivering", detail: "Last push " + ago(push.ok_at) };
      case "failing":
        return {
          tone: "destructive",
          badge: "Failing",
          detail: [push.failed_reason || "The last push failed.", ago(push.failed_at)].filter(Boolean).join(" · "),
        };
      case "refused":
        return { tone: "destructive", badge: "Refused", detail: "The relay no longer knows this host. Pair Conduit again." };
      case "blocked":
        return { tone: "destructive", badge: "Blocked", detail: "Something in front of the relay is blocking this host." };
      case "waiting":
        return { tone: "outline", badge: "Paired", detail: "No push sent yet" };
      default:
        return { tone: "outline", badge: "Not paired", detail: "Pair from Conduit's Settings to get notifications" };
    }
  }

  function Row(props) {
    var row = props.row;
    var dot =
      row.tone === "success"
        ? "text-success"
        : row.tone === "destructive"
          ? "text-destructive"
          : row.tone === "warning"
            ? "text-warning"
            : "text-muted-foreground";
    return h(
      "div",
      {
        className:
          "flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 border border-border p-3 w-full",
      },
      h(
        "div",
        { className: "flex items-center gap-3 min-w-0 w-full" },
        h("span", { className: "h-4 w-4 shrink-0 text-center leading-4 " + dot, "aria-hidden": true }, "●"),
        h(
          "div",
          { className: "flex flex-col gap-0.5 min-w-0" },
          h("span", { className: "font-mondwest normal-case text-sm font-medium truncate" }, props.label),
          h("span", { className: "font-mondwest normal-case text-xs text-muted-foreground" }, row.detail)
        )
      ),
      ui.Badge ? h(ui.Badge, { tone: row.tone, className: "shrink-0 self-start sm:self-center" }, row.badge) : null
    );
  }

  // Renders a single usage-window gauge as a labeled progress bar.
  // gauge: { title, short, used_bp, full, resets_at }
  //   used_bp: 0–10000 (basis points; 10000 = 100%)
  function QuotaBar(props) {
    var gauge = props.gauge;
    var pct = typeof gauge.used_bp === "number" ? Math.min(100, gauge.used_bp / 100) : 0;
    var full = gauge.full;
    var barColor = full
      ? "bg-destructive"
      : pct >= 80
        ? "bg-warning"
        : "bg-success";
    var resetText = null;
    if (typeof gauge.resets_at === "number") {
      var resetAgo = ago(gauge.resets_at);
      if (resetAgo) resetText = "Resets " + resetAgo;
    }
    return h(
      "div",
      { className: "flex flex-col gap-1 w-full" },
      h(
        "div",
        { className: "flex items-center justify-between gap-2" },
        h("span", { className: "font-mondwest normal-case text-xs font-medium truncate" }, gauge.title || gauge.short || gauge.id),
        h(
          "span",
          { className: "font-mondwest normal-case text-xs text-muted-foreground shrink-0 tabular-nums" },
          pct.toFixed(0) + "%"
        )
      ),
      h(
        "div",
        {
          className: "h-1.5 w-full rounded-full bg-muted overflow-hidden",
          role: "progressbar",
          "aria-valuenow": pct,
          "aria-valuemin": 0,
          "aria-valuemax": 100,
        },
        h("div", {
          className: "h-full rounded-full transition-all " + barColor,
          style: { width: pct + "%" },
        })
      ),
      resetText
        ? h("span", { className: "font-mondwest normal-case text-xs text-muted-foreground" }, resetText)
        : null
    );
  }

  // Renders the Claude usage quota section: a list of gauges.
  // quota: null (not fetched), false (unavailable/not on usage-window plan),
  //        or { plan_kind, gauges: [...] }
  function QuotaSection(props) {
    var quota = props.quota;
    if (!quota || quota.plan_kind !== "usage_window" || !Array.isArray(quota.gauges) || quota.gauges.length === 0) {
      return null;
    }
    return h(
      "div",
      { className: "flex flex-col gap-2 border border-border p-3 w-full" },
      h(
        "div",
        { className: "flex items-center gap-2 mb-1" },
        h("span", { className: "font-mondwest normal-case text-xs font-semibold text-foreground" }, "Claude usage")
      ),
      h(
        "div",
        { className: "flex flex-col gap-2.5" },
        quota.gauges.map(function (gauge) {
          return h(QuotaBar, { key: gauge.id, gauge: gauge });
        })
      )
    );
  }

  function ConduitCard() {
    var pair = useState(null);
    var status = pair[0];
    var setStatus = pair[1];

    var quotaPair = useState(null);
    var quota = quotaPair[0];
    var setQuota = quotaPair[1];

    useEffect(function () {
      var stopped = false;
      function load() {
        SDK.fetchJSON("/api/plugins/conduit_push/link-status")
          .then(function (body) {
            if (!stopped) setStatus(body && body.ok ? body : null);
          })
          .catch(function () {
            if (!stopped) setStatus(null);
          });
      }
      load();
      var timer = setInterval(load, POLL_MS);
      return function () {
        stopped = true;
        clearInterval(timer);
      };
    }, []);

    useEffect(function () {
      var stopped = false;
      function loadQuota() {
        SDK.fetchJSON("/api/plugins/conduit_push/sessions/plan-quota")
          .then(function (body) {
            if (!stopped) setQuota(body && body.ok ? body : false);
          })
          .catch(function () {
            if (!stopped) setQuota(false);
          });
      }
      loadQuota();
      var timer = setInterval(loadQuota, QUOTA_POLL_MS);
      return function () {
        stopped = true;
        clearInterval(timer);
      };
    }, []);

    // Nothing to say on a host no Conduit has used.
    if (!status || (status.app.state === "never" && status.push.state === "not_paired")) return null;
    if (!ui.Card || !ui.CardHeader || !ui.CardTitle || !ui.CardContent) return null;
    var title = status.push.gateway_name ? "Conduit · " + status.push.gateway_name : "Conduit";
    return h(
      ui.Card,
      null,
      h(ui.CardHeader, null, h(ui.CardTitle, { className: "text-base" }, title)),
      h(
        ui.CardContent,
        { className: "grid gap-3" },
        h(Row, { label: "App", row: appRow(status.app) }),
        h(Row, { label: "Push notifications", row: pushRow(status.push) }),
        h(QuotaSection, { quota: quota })
      )
    );
  }

  plugins.registerSlot("conduit_push", "sessions:top", ConduitCard);
})();
