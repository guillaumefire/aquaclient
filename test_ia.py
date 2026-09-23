"""Tests parse_commande / comprendre_commande (notes / fournitures / analyses + dates)."""
from datetime import datetime, timedelta

from main import comprendre_commande, corriger_transcription, parse_commande, parse_date_fr


def test_note():
    r = parse_commande("note le filtre est sale")
    assert r["ok"] and r["intent"] == "note"
    assert "filtre" in r["texte"].lower()


def test_fourniture_sans_prix():
    r = parse_commande("facture 2 bidon de chlore le 16 septembre")
    assert r["ok"] and r["intent"] == "fourniture"
    assert r["quantite"] == 2
    assert "chlore" in r["description"]
    assert "septembre" not in r["description"].lower()
    assert r["date_ref"] == "2026-09-16"
    assert r["date"]  # date de saisie = aujourd'hui


def test_fourniture_orale():
    r = parse_commande("fourniture 3 bidon de chlore")
    assert r["ok"] and r["intent"] == "fourniture"
    assert r["quantite"] == 3
    assert "chlore" in r["description"].lower()
    assert r["date_ref"] == ""

    r2 = parse_commande("trois bidons de chlore")
    assert r2["ok"] and r2["quantite"] == 3

    r3 = parse_commande(corriger_transcription("furniture 3 biden de clore"))
    assert r3["ok"] and r3["quantite"] == 3


def test_analyse():
    r = parse_commande("analyse pH 7.2 chlore 1.5")
    assert r["ok"] and r["intent"] == "analyse"
    assert "ph" in r["texte"].lower() or "7.2" in r["texte"]


def test_date():
    assert parse_date_fr("bidon le 16 septembre") == "2026-09-16"
    assert parse_date_fr("fourniture 2 bidon de chlore le 21/09/2026") == "2026-09-21"
    assert parse_date_fr("fourniture 2 bidon de chlore le 21 09 2026") == "2026-09-21"
    assert parse_date_fr("fourniture 2 bidon le vingt et un septembre") == "2026-09-21"
    hier = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert parse_date_fr("fourniture 2 bidon hier") == hier


def test_fourniture_deux_dates():
    for phrase in (
        "fourniture 2 bidon de chlore le 21/09/2026",
        "fourniture 2 bidon de chlore le 21 09 2026",
        "fourniture 2 bidon de chlore le vingt et un septembre",
        "fourniture 2 bidon de chlore le 21 septembre",
    ):
        r = comprendre_commande(phrase)
        assert r["ok"], phrase
        assert r["date_ref"] == "2026-09-21", (phrase, r)
        assert "chlore" in r["description"].lower() or "bidon" in r["description"].lower()
        assert "21" not in r["description"]
        assert "septembre" not in r["description"].lower()


def test_ia_locale_hier():
    hier = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    r = comprendre_commande("fourniture 2 bidon de chlore hier")
    assert r["ok"]
    assert r["date_ref"] == hier
    assert "hier" not in r["description"].lower()


def test_ia_locale_naturelle():
    hier = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    r = comprendre_commande("j ai mis 2 bidons de chlore hier")
    assert r["ok"] and r["intent"] == "fourniture"
    assert r["quantite"] == 2
    assert "chlore" in r["description"]
    assert "bidon" in r["description"]
    assert "mis" not in r["description"].lower()
    assert r["date_ref"] == hier

    r2 = comprendre_commande("ajoute 3 sac de sel le 21 septembre")
    assert r2["ok"] and r2["quantite"] == 3
    assert "sel" in r2["description"].lower()
    assert "sac" in r2["description"].lower()
    assert r2["date_ref"].endswith("-09-21")

    r3 = comprendre_commande("fourniture chlore 2 bidons pour le 21 septembre")
    assert r3["ok"] and r3["quantite"] == 2
    assert "chlore" in r3["description"].lower()
    assert r3["date_ref"].endswith("-09-21")


def test_intent_force():
    r = parse_commande("2 bidons de chlore", intent_force="fourniture")
    assert r["ok"] and r["intent"] == "fourniture"


if __name__ == "__main__":
    test_note()
    test_fourniture_sans_prix()
    test_fourniture_orale()
    test_analyse()
    test_date()
    test_fourniture_deux_dates()
    test_ia_locale_hier()
    test_ia_locale_naturelle()
    test_intent_force()
    print("OK — tous les tests passent")
