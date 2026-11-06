# Changelog

## 0.3.1
- Jarvis: context doar cu entitățile relevante, încadrat în `num_ctx` (implicit 8192, ca integrarea Ollama din HA), răspuns brut afișat dacă modelul nu respectă formatul.

## 0.3.0
- Paznic adaptat la o casă reală: doar `unavailable` (nu `unknown`), sare peste ce e picat de peste 24h,
  nu urmărește media_player (TV-urile stinse), rezumat când pică mai multe deodată,
  alertele se închid doar când problema chiar dispare.
- Jarvis: `think: false` și modelul ținut încărcat (`keep_alive`).

## 0.2.0
- Paznic: dispozitive indisponibile și baterii slabe, cu alertă la rezolvare.
- Automatizări: camere după prezență, centrală blocată când încălzește AC-ul, robinete care urmează termostatul.
- Jarvis: asistent prin Ollama, cu listă de domenii permise.
- Panou web în sidebar (ingress).

## 0.1.0
- Schelet add-on.
