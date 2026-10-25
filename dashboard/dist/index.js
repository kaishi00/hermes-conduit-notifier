(function () {
  "use strict";
  // conduit_push's backend routes serve the Conduit iOS app. The dashboard
  // gets one thing from it: a Conduit card at the top of the Sessions page
  // (slot sessions:top), beside Hermes' own Connected Platforms card, which
  // lists only the gateway's messaging adapters. It reads /link-status: is
  // the app reaching this host, and do its pushes get through the relay?
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

  function ConduitCard() {
    var pair = useState(null);
    var status = pair[0];
    var setStatus = pair[1];

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
        h(Row, { label: "Push notifications", row: pushRow(status.push) })
      )
    );
  }

  plugins.registerSlot("conduit_push", "sessions:top", ConduitCard);
})();
