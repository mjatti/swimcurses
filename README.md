# Swimify TUI

Tekstipohjainen (TUI) sovellus live.swimify.com -tulosten seuraamiseen.

## Ominaisuudet
- Käynnissä olevien kilpailujen lista
- Reaaliaikainen websocket-seuranta nykyisestä erästä
- Näkymä yksittäisten erien tuloksille ja väliajoille
- Värikoodaus muuttuneille riveille
- Event–Heat-valinta (H)
- Paluu nykyiseen erään (C)
- GraphQL-lokitus (`--api-log`)
- Verbose-näkymä (`--verbose`)

## Käyttö
```bash
python3 swimify_tui.py --api-log logs/api.log --verbose
