# Home Brain

Creierul casei RusHome, într-un singur add-on:

- **Paznic** – verifică la fiecare minut dispozitivele `unavailable`/`unknown` (după un prag de minute) și bateriile slabe. Trimite o alertă o singură dată și anunță când problema s-a rezolvat.
- **Automatizări** – reguli care pornesc doar dacă le completezi entitățile.
- **Jarvis** – asistent AI prin Ollama: răspunde despre casă și execută comenzi pe domeniile permise.
- **Panou** – pagina „Home Brain” din sidebar: alerte, automatizări, chat, istoric.

## Configurare

### Notificări
```yaml
notify_services:
  - notify.mobile_app_telefon_razvan
  - notify.mobile_app_telefon_hajni
ntfy_url: "http://ntfy.local/rushome"   # opțional
```

### Paznic
```yaml
guard:
  unavailable_minutes: 30     # după cât timp e alertă
  max_unavailable_hours: 24   # ce e picat de mai mult e considerat cunoscut
  bad_states: [unavailable]   # adaugă unknown dacă vrei
  battery_threshold: 20       # %
  interval_seconds: 60
  ignore:                     # modele fnmatch
    - "sensor.*_linkquality"
    - "media_player.*"
```

### Camere doar când nu e nimeni acasă
```yaml
presence_camera:
  persons: [person.razvan, person.hajni]
  camera_switches: [switch.frigate_detect]
```
Dacă vreo persoană e `unknown`/`unavailable`, regula nu face nimic (ca să nu oprească camerele din greșeală).

### Centrala oprită cât încălzește AC-ul
```yaml
heating_ac_lock:
  ac_climates: [climate.ac_living, climate.ac_dormitor]
  boiler_climate: climate.termostat_centrala
```
Când un AC e pe `heat`, termostatul centralei trece pe `off`. Când niciun AC nu mai încălzește, revine la modul anterior.

### Robinete care urmează termostatul
```yaml
follow_thermostat:
  leader_climate: climate.termostat_centrala
  follower_climates: [climate.robinet_bucatarie]
```

### Jarvis
```yaml
assistant:
  ollama_url: "http://IP-OLLAMA:11434"
  model: "llama3.1:8b"
  allowed_domains: [light, switch, climate, cover, fan, media_player, scene, script]
```
Domeniile care nu sunt în listă (de ex. `lock`, `alarm_control_panel`) sunt refuzate chiar dacă modelul le cere.

## Securitate
Panoul răspunde doar cererilor venite prin ingress-ul Home Assistant.
