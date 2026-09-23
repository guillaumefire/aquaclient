# AquaClient — Site client terrain

Application pour techniciens piscine : trouver le client le plus proche (GPS), créer un client (adresse → coordonnées), prendre des notes et facturer à la voix.

## Lancer l'appli

```bash
cd site_client
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Ouvrir : [http://localhost:8000](http://localhost:8000)

> La géolocalisation et le micro nécessitent **HTTPS** ou **localhost**, et un navigateur type Chrome / Edge.

## Fonctionnement

| Action | Comportement |
|--------|----------------|
| **Client** | GPS → client dans **1 km**, sinon propose le **plus proche** + liste manuelle |
| **Liste clients** | Choix manuel si le GPS PC est imprécis |
| **Recaler GPS** | Sur la fiche : enregistre ta position actuelle sur le client |
| **Micro** | Dictée **locale** (Whisper) — sans Google ; 1re fois = téléchargement du modèle |

## Fichiers CSV (`data/`)

- `clients.csv` — `id,nom,prenom,adresse,latitude,longitude`
- `notes.csv` — notes par client
- `factures.csv` — lignes de facturation

## Tests hors terrain

Les clients d'exemple sont autour de Montpellier. Pour tester « Client » près de chez vous, créez un **Nouveau client** avec votre adresse réelle (ou déplacez-vous près d'un client existant).
