# Changelog

## 0.4.0 – creierul
- Memorie locală (SQLite) cu originea fiecărei schimbări; import de istoric la pornire.
- Obiceiuri de oră și de secvență, doar din acțiunile omului, testate pe istoric, filtrate față de
  automatizările existente; automatizări native create la aprobare, cu Undo.
- Detector de neobișnuit: valori anormale pe oră, senzori tăcuți, flapping, stare prea lungă,
  casă deschisă când nu e nimeni; învață din reacții.
- Aprobări din notificare (Aprobă / Mereu / Respinge / Anulează), moduri Shadow / Co-pilot / Autonom,
  guardrails care nu pot fi ocolite.
- Agenți AI locali: Climă, Energie, Securitate, Server & homelab; rulează doar când s-a schimbat ceva.
- Panou nou cu tab-uri; Jarvis știe și ce a observat creierul.

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
