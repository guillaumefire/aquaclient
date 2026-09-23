# AquaClient — App terrain techniciens

Application pour techniciens piscine : client à proximité (GPS), liste / carte / agenda, notes vocales, fournitures / factures, check-up de visite.

## Lancer l'appli

```bash
cd aquaclient
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Ouvrir : [http://localhost:8000](http://localhost:8000)

> La géolocalisation et le micro nécessitent **HTTPS** ou **localhost** (Chrome / Edge).

## Fonctionnalités

- **Client à proximité** — GPS dans un rayon de 1 km
- **Liste / Carte / Agenda** — clients et passages planifiés
- **Factures** — suivi envoyée / payée
- **Visite** — checklist, vocal (note / fourniture / analyse), dates orales
- **Nouveau client** — adresse, GPS, type d’entretien, équipements

## Données

Les CSV sont créés automatiquement dans `data/` au premier lancement (non versionnés).
