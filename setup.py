# coding=utf-8

plugin_identifier = "reality_check"
plugin_package = "octoprint_reality_check"
plugin_name = "OctoPrint-Reality-Check"
plugin_version = "0.3.0"
plugin_description = """Pre-print gate for Prusa Buddy printers on serial: compares the
gcode's declared filament type and nozzle size against what the printer's firmware
reports (M865 / M862.1 Q) and blocks mismatched prints - the check the printer only
performs for file-based prints, restored for OctoPrint streaming."""
plugin_author = "Nitzan Raz"
plugin_author_email = "nitz.raz@gmail.com"
plugin_url = "https://github.com/BackSlasher/OctoPrint-Reality-Check"
plugin_license = "AGPLv3"
plugin_requires = []

plugin_additional_data = []
plugin_additional_packages = []
plugin_ignored_packages = []
additional_setup_parameters = {}

########################################################################################################################

from setuptools import setup

try:
    import octoprint_setuptools
except Exception:
    print("Could not import OctoPrint's setuptools, are you sure you are running that under "
          "the same python installation that OctoPrint is installed under?")
    import sys
    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier=plugin_identifier,
    package=plugin_package,
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    mail=plugin_author_email,
    url=plugin_url,
    license=plugin_license,
    requires=plugin_requires,
    additional_packages=plugin_additional_packages,
    ignored_packages=plugin_ignored_packages,
    additional_data=plugin_additional_data,
)

if len(additional_setup_parameters):
    from octoprint.util import dict_merge

    setup_parameters = dict_merge(setup_parameters, additional_setup_parameters)

setup(**setup_parameters)
