/*
 * Reality Check frontend: popups for gate verdicts, and the status tab
 * showing the firmware-truth cache (per-tool filament + nozzle).
 */
$(function () {
    function RealityCheckViewModel() {
        var self = this;

        self.tools = ko.observableArray([]);
        self.age = ko.observable("never");
        self.refreshing = ko.observable(false);
        self.events = ko.observableArray([]);
        self.showEvents = ko.observable(false);

        self.toggleEvents = function () {
            self.showEvents(!self.showEvents());
        };

        self._applyEvents = function (events) {
            if (!events) {
                return;
            }
            // newest first for the table
            self.events(events.slice().reverse().map(function (e) {
                return {
                    time: new Date(e.time * 1000).toLocaleTimeString(),
                    msg: e.msg,
                    level: e.level
                };
            }));
        };

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
            OctoPrint.simpleApiGet("reality_check").done(function (response) {
                self._apply(response.state);
                self._applyEvents(response.events);
            });
        };

        self.refresh = function () {
            self.refreshing(true);
            OctoPrint.simpleApiCommand("reality_check", "refresh", {})
                .done(function (response) {
                    self._apply(response.state);
                    self._applyEvents(response.events);
                })
                .always(function () {
                    self.refreshing(false);
                });
        };

        self.onStartupComplete = function () {
            self.fetch();
        };

        self.onTabChange = function (next) {
            if (next === "#tab_plugin_reality_check") {
                self.fetch();
            }
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "reality_check" || !data || !data.msg) {
                return;
            }
            self.fetch();
            if (data.silent) {
                return;
            }
            var type = data.type === "error" ? "error"
                : data.type === "success" ? "success"
                    : "info";
            new PNotify({
                title: "Reality Check",
                text: data.html || data.msg,
                type: type,
                hide: type !== "error"
            });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: RealityCheckViewModel,
        dependencies: [],
        elements: ["#tab_plugin_reality_check"]
    });
});
