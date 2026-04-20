# Southern Company HACS

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![pre-commit][pre-commit-shield]][pre-commit]
[![Black][black-shield]][black]

[![hacs][hacsbadge]][hacs]
[![Project Maintenance][maintenance-shield]][user_profile]

[![Discord][discord-shield]][discord]
[![Community Forum][forum-shield]][forum]

**This component will set up the following platforms.**

| Platform        | Description                               |
| --------------- | ----------------------------------------- |
| `binary_sensor` | Show something `True` or `False`.         |
| `sensor`        | Show info from Southern Company HACS API. |
| `switch`        | Switch something `True` or `False`.       |

## Installation

### HACS

You can add this repository to HACS if you have it installed:

1. Navigate to HACS. Click the 3 dots at the top right, then select "Custom Repositories".
2. Enter the URL below for Repository:
```txt
https://github.com/Southern-Company-HA/southern-company-hacs
```
3. Select the Type "Integration".
4. Click the "Add" button.
5. Search for "Southern Company" in HACS, then download and install. You may need to restart HA.

### Manual

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
2. If you do not have a `custom_components` directory (folder) there, you need to create it.
3. In the `custom_components` directory (folder) create a new folder called `southern_company_hacs`.
4. Download _all_ the files from the `custom_components/southern_company_hacs/` directory (folder) in this repository.
5. Place the files you downloaded in the new directory (folder) you created.
6. Restart Home Assistant.

Using your HA configuration directory (folder) as a starting point you should now also have this:

```text
custom_components/southern_company_hacs/translations/en.json
custom_components/southern_company_hacs/translations/fr.json
custom_components/southern_company_hacs/translations/nb.json
custom_components/southern_company_hacs/translations/sensor.en.json
custom_components/southern_company_hacs/translations/sensor.fr.json
custom_components/southern_company_hacs/translations/sensor.nb.json
custom_components/southern_company_hacs/translations/sensor.nb.json
custom_components/southern_company_hacs/__init__.py
custom_components/southern_company_hacs/api.py
custom_components/southern_company_hacs/binary_sensor.py
custom_components/southern_company_hacs/config_flow.py
custom_components/southern_company_hacs/const.py
custom_components/southern_company_hacs/manifest.json
custom_components/southern_company_hacs/sensor.py
custom_components/southern_company_hacs/switch.py
```

## Configuration

In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Southern Company HACS"

Configuration is done in the UI.

<!---->

## Tariff Windows

The integration can split hourly usage and cost statistics into per-tariff buckets. Configure tariff windows through **Settings > Devices & Services > Southern Company > Configure**.

Each tariff window defines:
- **Name** — a label for the rate tier (e.g. `on_peak`, `off_peak`, `super_off_peak`)
- **Days** — which days of the week (0 = Monday through 6 = Sunday)
- **Start hour / End hour** — the time window (half-open interval, e.g. 14–19 means 2 PM–7 PM)
- **Months** — (optional) restrict the window to specific months (1 = January through 12 = December)
- **Rate** — (optional) cost per kWh in dollars. When set, the integration computes cost as `kWh × rate` instead of using the API's actual cost for that hour

Hours not matching any tariff window fall into the `default` tier and use the actual API cost.

### Example: Georgia Power Overnight Advantage

| Tier | Rate ($/kWh) | Hours | Days | Months |
|------|-------------|-------|------|--------|
| on_peak | 0.2979 | 14–19 | Mon–Fri | Jun–Sep |
| off_peak | 0.1017 | 7–23 | Every day | — |
| super_off_peak | 0.0219 | 0–7 | Every day | — |

Add a fourth entry for the off-peak evening transition on summer weekdays:
- off_peak, days 0–4, months 6–9, start_hour 19, end_hour 23

When `rate` is set, per-tariff cost statistics use `kWh × rate`. When `rate` is omitted, the actual hourly cost from the API is used instead. This lets you use published flat rates for segmentation while the API cost data is still lagging, and switch to actual costs later.

## Energy Dashboard and the 48-hour lag

Southern Company's hourly usage API typically lags about 48 hours behind real time. This means the Energy Dashboard would show blank for today and yesterday even though historical data is present.

The integration automatically **extrapolates** estimated hourly statistics to fill this gap. It spreads the difference between the current monthly total (which is near-real-time) and the last known hourly sum evenly across the missing hours. These estimates appear as a flat average — they won't reflect your actual per-hour usage patterns, but they keep the Energy Dashboard populated.

When real hourly data becomes available from Southern Company (typically within 48 hours), the integration overwrites the estimates with actual readings. You may see a small adjustment in the Energy Dashboard at that point as the flat estimate is replaced by the real hourly values.

## Energy Dashboard Setup

The integration writes two types of long-term statistics per account:

- **`southern_company:energy_usage_<account>`** — total kWh (and per-tariff if configured)
- **`southern_company:energy_cost_<account>`** — total cost in USD (and per-tariff if configured)

To add them to the Energy Dashboard, go to **Settings > Dashboards > Energy** and add each statistic as a grid consumption source. Per-tariff statistics (e.g. `energy_usage_on_peak_*`) can be added as separate sources with their corresponding rate for cost tracking.

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

## Credits

This project was generated from [@oncleben31](https://github.com/oncleben31)'s [Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component) template.

Code template was mainly taken from [@Ludeeus](https://github.com/ludeeus)'s [integration_blueprint][integration_blueprint] template

---

[integration_blueprint]: https://github.com/custom-components/integration_blueprint
[black]: https://github.com/psf/black
[black-shield]: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
[buymecoffee]: https://www.buymeacoffee.com/LashL
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/Lash-L/southern-company-hacs.svg?style=for-the-badge
[commits]: https://github.com/Lash-L/southern-company-hacs/commits/main
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[discord]: https://discord.gg/Qa5fW2R
[discord-shield]: https://img.shields.io/discord/330944238910963714.svg?style=for-the-badge
[exampleimg]: example.png
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[license-shield]: https://img.shields.io/github/license/Lash-L/southern-company-hacs.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40Lash-L-blue.svg?style=for-the-badge
[pre-commit]: https://github.com/pre-commit/pre-commit
[pre-commit-shield]: https://img.shields.io/badge/pre--commit-enabled-brightgreen?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/Lash-L/southern-company-hacs.svg?style=for-the-badge
[releases]: https://github.com/Lash-L/southern-company-hacs/releases
[user_profile]: https://github.com/Lash-L
