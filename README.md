# ha-home-brain

Add-on Home Assistant pentru RusHome: paznic HA, automatizări inteligente, asistent AI (Jarvis) și panou în sidebar.

## Instalare

1. În Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Adaugă `https://github.com/RazvanR1/ha-home-brain`.
3. Instalează **Home Brain**, completează opțiunile (vezi [DOCS.md](ha_home_brain/DOCS.md)) și pornește-l.

## Dezvoltare

```bash
pip install -r ha_home_brain/requirements.txt pytest pytest-asyncio
pytest
```
Testele pornesc un Home Assistant fals (WebSocket) și un Ollama fals.
