# 🧠 Home Brain for Home Assistant

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FRazvanR1%2Fha-home-brain)
![Version](https://img.shields.io/badge/version-0.4.4-blue)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green)
![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green)
![Local only](https://img.shields.io/badge/cloud-none-success)

**A local "brain" for your home.** Home Brain watches what happens in Home Assistant, learns what is
*normal* for **your** house, turns your habits into native HA automations (after you approve them),
tells you when something is unusual, and lets local AI agents look at the harder questions.
Everything runs on your hardware. The AI part (Ollama) is optional and only used when it adds value.

> 🇷🇴 [Descriere în română mai jos](#-pe-scurt-în-română)

---

## What it does

| | |
|---|---|
| 🧭 **Learns your habits** | Only from what *people* do (app, dashboard, wall switch), never from what automations do. Two kinds: *time* habits ("on weekdays around 22:30 you turn on the bedside lamp") and *sequence* habits ("after the front door opens you turn on the hallway light"). |
| 🧪 **Backtests before proposing** | Every habit is replayed on your history: on how many days the automation would have done exactly what you did, and on how many it would have done something extra. Habits already covered by one of your automations are skipped. |
| ✅ **Native automations, with Undo** | When you approve, Home Brain creates a normal Home Assistant automation (`home_brain_…`) that you can read, edit or delete like any other — and undo from the panel. |
| 👀 **Notices what's unusual** | No device lists, no thresholds to set. It learns per-hour baselines and flags: abnormal values for the time of day, sensors that went silent, devices flapping on/off, doors or lights left in a state far longer than usual, house open while nobody is home. |
| 📱 **Approve from your phone** | Actionable notifications: **Approve / Always / Reject**, **Undo** after something it did on its own, **Seen / Not a problem** for observations. It learns from your answers. |
| 🛡️ **Safety rails that can't be bypassed** | Locks, alarm, valves, gates and "important" devices (boiler, fridge, server, NAS…) always need approval. Shell commands, Supervisor calls, restarting HA, disabling automations and temperatures outside your limits are blocked outright. |
| 🤖 **Local AI agents** (optional) | Climate, Energy, Security and Server/homelab agents analyse the house every few hours **only if something changed**, one at a time, on a single Ollama model. Their findings go to the panel and a single evening digest. |
| 💬 **Jarvis chat** | Ask about the house or give commands in the panel; it also knows what the brain has observed. Commands go through the same safety rails. |
| 🚨 **Guard** | Devices that become unavailable, low batteries — one alert per problem, a summary when many things fail at once, and a "resolved" message when it's fixed. Not fooled by HA restarts. |
| 🖥️ **Sidebar panel** | Home, Approvals, Habits, Unusual, Agents, Jarvis and Log (with Undo). |

### How it thinks

```
Observe ─► Learn locally (no AI) ─► Decide what matters ─► Think (AI, only if needed) ─► Act or ask
  state       habits, baselines,        new / unusual          domain agents               shadow / co-pilot /
  changes     dwell times, rates        only                   on your Ollama              autonomous + Undo
```

Every state change is stored with its **origin** — a person, the device itself, one of your automations,
or Home Brain. That is what lets it learn habits without "learning" your existing automations, and
what keeps it from learning from its own actions.

---

## Requirements

- Home Assistant **OS** or **Supervised** (it is an add-on / app).
- The **Home Assistant Companion app** on your phone for actionable notifications (any `notify.mobile_app_*` service).
- *Optional:* an **[Ollama](https://ollama.com)** server on your network for the AI agents and Jarvis.
  A 7–9B model works (tested with `qwen3.5:9b` on a 12 GB GPU). Without Ollama, everything except
  the agents and Jarvis still works.

## Installation

1. Click the **Add repository** button above, or in Home Assistant go to
   **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add:
   ```
   https://github.com/RazvanR1/ha-home-brain
   ```
2. Install **Home Brain**.
3. In the **Configuration** tab set at least:
   ```yaml
   notify_services:
     - notify.mobile_app_your_phone
   learning:
     persons: [person.you, person.partner]
   assistant:
     ollama_url: "http://192.168.1.50:11434"   # optional
     model: "qwen3.5:9b"
   ```
4. Start it and turn on **Show in sidebar**.

On first start it imports the last 10 days of history, so habits and baselines are available within
minutes instead of weeks. Habits keep improving as more days accumulate.

## Configuration (most useful options)

| Option | Default | What it does |
|---|---|---|
| `site_context` | — | A few sentences about your home (who lives there, schedule, heating…). Given to the AI agents and Jarvis. |
| `notify_services` | `[]` | Where alerts and approval requests go. Buttons work with `mobile_app_*`. |
| `learning.persons` | `[]` | Used for "only when someone is home" conditions and presence-aware checks. |
| `learning.max_proposals_per_day` | `2` | How many new habit proposals may reach your phone per day. |
| `unusual.open_away` | `false` | Alert when doors/windows are open and nobody is home (off by default in case you already have such an automation). |
| `unusual.max_push_per_day` | `5` | Cap on "unusual" notifications per day; more than 3 at once become one summary. |
| `safety.autonomy` | `copilot` | `shadow` (only log what it would do), `copilot` (ask), `autonomous` (do safe things, offer Undo). Per domain: `autonomy_overrides: ["light:autonomous"]`. |
| `safety.min/max_temperature` | `16` / `26` | Hard limits for any temperature it may set. |
| `agents.notify` | `digest` | `none`, `digest` (one summary at `digest_hour`), or `critical`. |
| `agents.allow_actions` | `false` | Let agents propose device actions (always via approval). |
| `assistant.num_ctx` | `8192` | Keep it equal to your Ollama integration in HA so the model isn't reloaded. |

The full list with explanations is in the add-on's **Documentation** tab ([DOCS.md](ha_home_brain/DOCS.md)).

## Privacy

- No cloud, no telemetry. Data stays in the add-on's `/data/brain.db` (SQLite) on your HA machine.
- The only outbound calls are to your own Ollama server (if configured) and to Home Assistant.
- The panel only answers requests that come through Home Assistant's ingress.

## Current limitations

- **The UI, notifications and AI prompts are in Romanian.** English is planned; PRs welcome.
- Small local models can misread data. That is why agents only make observations by default and
  everything that touches a device goes through approval.
- Habit learning needs a few days of history with consistent behaviour (default: ≥ 4 days, ≥ 60 %).

## Development

```bash
pip install -r ha_home_brain/requirements.txt pytest pytest-asyncio
pytest
```

The tests start a fake Home Assistant (WebSocket + REST) and a fake Ollama, and cover habit mining
and backtesting, anomaly detection, approvals from notification buttons, guardrails, Undo, and the
guard surviving HA restarts.

Code map: `ha.py` (HA client, origin tracking) · `recorder.py` + `store.py` (memory) ·
`habits.py` (habit mining, backtest, automation builder) · `anomaly.py` (baselines, detectors) ·
`approvals.py` (notifications, guardrails, Undo) · `agents.py` + `llm.py` (AI agents) ·
`assistant.py` (Jarvis) · `guard.py` · `web.py` + `static/index.html` (panel).

Inspired by [gabipintea/ha-ai-brain](https://github.com/gabipintea/ha-ai-brain).

## License

[MIT](LICENSE)

---

## 🇷🇴 Pe scurt, în română

**Home Brain** e un add-on pentru Home Assistant care rulează complet local și face patru lucruri:

- **Învață ce faci tu.** Pornește doar de la acțiunile oamenilor, nu de la cele făcute de automatizări. Fiecare obicei îl testează întâi pe istoric și abia apoi ți-l propune ca automatizare nativă în HA, pe care o poți anula oricând.
- **Îți spune când ceva e neobișnuit**, fără să-i dai vreo listă de dispozitive. Învață singur ce e normal în casa ta.
- **Te întreabă pe telefon** înainte să facă ceva, cu butoanele Aprobă / Mereu / Respinge / Anulează. Unele acțiuni nu le poate face nici dacă vrea: are reguli de siguranță fixe.
- **Folosește agenți AI locali** (climă, energie, securitate, server) prin Ollama, doar când s-a schimbat ceva în casă.

**Instalare:** în Settings → Add-ons → Add-on Store → ⋮ → Repositories adaugi
`https://github.com/RazvanR1/ha-home-brain`, instalezi **Home Brain**, completezi `notify_services`,
`learning.persons` și, opțional, `assistant.ollama_url`, apoi îl pornești.
Toate detaliile sunt în tab-ul **Documentation** al add-on-ului.
