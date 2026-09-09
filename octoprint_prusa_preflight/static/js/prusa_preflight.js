/*
 * Surface Prusa Preflight's server-side messages as OctoPrint popups.
 * Levels map straight onto PNotify types; successes auto-hide, problems stay.
 */
$(function () {
    function PrusaPreflightViewModel() {
        var self = this;

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "prusa_preflight" || !data || !data.msg) {
                return;
            }
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
        elements: []
    });
});
