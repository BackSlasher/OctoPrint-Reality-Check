/*
 * Prusa Preflight frontend: popups for gate verdicts, and the status tab
 * showing the firmware-truth cache (per-tool filament + nozzle).
 */
$(function () {
    function PrusaPreflightViewModel() {
        var self = this;

        self.tools = ko.observableArray([]);
        self.age = ko.observable("never");
        self.lastVerdict = ko.observable("-");
        self.refreshing = ko.observable(false);

        self._apply = function (state) {
            if (!state) {
                return;
            }
            var filaments = state.filaments || {};
            var nozzles = state.nozzles || {};
            var keys = Object.keys(filaments);
            Object.keys(nozzles).forEach(function (k) {
                if (keys.indexOf(k) < 0) keys.push(k);
            });
            keys.sort();
            self.tools(keys.map(function (k) {
                var n = nozzles[k];
                var flags = [];
                if (n && n.high_flow) flags.push("high-flow");
                if (n && n.hardened) flags.push("hardened");
                return {
                    tool: k,
                    filament: filaments[k] || "(none loaded)",
                    nozzle: n ? n.diameter.toFixed(2) + " mm" : "?",
                    flags: flags.join(", ")
                };
            }));
            self.age(state.age == null ? "never polled" : Math.round(state.age) + "s");
        };

        self.fetch = function () {
            OctoPrint.simpleApiGet("prusa_preflight").done(function (response) {
                self._apply(response.state);
            });
        };

        self.refresh = function () {
            self.refreshing(true);
            OctoPrint.simpleApiCommand("prusa_preflight", "refresh", {})
                .done(function (response) {
                    self._apply(response.state);
                })
                .always(function () {
                    self.refreshing(false);
                });
        };

        self.onStartupComplete = function () {
            self.fetch();
        };

        self.onTabChange = function (next) {
            if (next === "#tab_plugin_prusa_preflight") {
                self.fetch();
            }
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "prusa_preflight" || !data || !data.msg) {
                return;
            }
            self.lastVerdict(data.msg);
            self.fetch();
            var type = data.type === "error" ? "error"
                : data.type === "success" ? "success"
                    : "info";
            new PNotify({
                title: "Prusa Preflight",
                text: data.msg,
                type: type,
                hide: type !== "error"
            });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PrusaPreflightViewModel,
        dependencies: [],
        elements: ["#tab_plugin_prusa_preflight"]
    });
});
