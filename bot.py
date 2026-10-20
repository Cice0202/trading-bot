"""
Trading-Bot – Phase 1: Krypto-Scanner (Top 100 nach Marktkapitalisierung)

Läuft alle 15 Minuten über GitHub Actions, sucht auffällige Bewegungen
und schickt die stärksten Chancen als Push-Nachricht über ntfy aufs iPhone.
Der Bot handelt NIE selbst – er meldet nur, was du dir anschauen solltest.
"""

import json
import os
import time

import requests

# ---------------------------------------------------------------------------
# Einstellungen (hier kannst du später nachjustieren)
# ---------------------------------------------------------------------------
MAX_MELDUNGEN = 3          # höchstens so viele Pushes pro Durchlauf
SPERRE_STUNDEN = 4         # gleicher Coin wird frühestens nach X Std. erneut gemeldet
MIN_PUNKTE_NORMAL = 3      # Signalstärke, ab der ein normaler Coin gemeldet wird
MIN_PUNKTE_MEME = 4        # strengerer Filter für Meme-Coins
AUSBRUCH_1H_PROZENT = 2.0  # Mindestbewegung in 1 Std. für ein Ausbruchssignal
VOLUMEN_SPRUNG_PROZENT = 5.0   # Anstieg des 24h-Volumens seit der letzten Prüfung
UMSATZ_QUOTE = 0.5         # 24h-Volumen im Verhältnis zur Marktkapitalisierung

# Stablecoins und "verpackte" Coins bewegen sich nicht sinnvoll -> ignorieren
IGNORIEREN = {
    "usdt", "usdc", "dai", "fdusd", "usde", "tusd", "usdd", "pyusd", "usds",
    "usd1", "busd", "frax", "eurc", "rlusd", "usdtb", "susds", "susde",
    "bsc-usd", "usdf", "usdg", "usd0", "gho", "crvusd", "xaut", "paxg",
    "wbtc", "weth", "steth", "wsteth", "weeth", "cbbtc", "wbeth", "reth",
    "jitosol", "msol", "lbtc", "solvbtc", "bnsol", "meth", "ezeth", "rseth",
    "cbeth", "jupsol", "bbsol", "tbtc", "wbnb", "clbtc", "lseth",
}

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CG_KEY = os.environ.get("COINGECKO_KEY", "").strip()
MANUELL = os.environ.get("MANUELL") == "true"
STATE_DATEI = "state.json"
CG_URL = "https://api.coingecko.com/api/v3/coins/markets"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def hole_maerkte(**extra):
    """Holt Marktdaten von CoinGecko (mit Wiederholung bei Limit)."""
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "price_change_percentage": "1h,24h",
    }
    params.update(extra)
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    for _ in range(3):
        antwort = requests.get(CG_URL, params=params, headers=headers, timeout=30)
        if antwort.status_code == 429:  # zu viele Anfragen -> kurz warten
            time.sleep(20)
            continue
        antwort.raise_for_status()
        return antwort.json()
    raise RuntimeError("CoinGecko-Limit erreicht, nächster Versuch in 15 Minuten.")


def lade_zustand():
    try:
        with open(STATE_DATEI, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"volumen": {}, "gemeldet": {}}


def speichere_zustand(zustand):
    with open(STATE_DATEI, "w", encoding="utf-8") as f:
        json.dump(zustand, f, indent=1)


def preis(p):
    return f"${p:,.2f}" if p >= 1 else f"${p:.6g}"


def sende_push(titel, text, tags, prioritaet=3, link=None):
    headers = {
        # Kopfzeilen dürfen nur einfache Zeichen enthalten
        "Title": titel.encode("latin-1", "ignore").decode("latin-1"),
        "Tags": ",".join(tags),
        "Priority": str(prioritaet),
    }
    if link:
        headers["Click"] = link
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=text.encode("utf-8"),
        headers=headers,
        timeout=30,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Signal-Bewertung
# ---------------------------------------------------------------------------
def bewerte(coin, zustand, jetzt, meme_ids):
    p = coin.get("current_price")
    hoch = coin.get("high_24h")
    tief = coin.get("low_24h")
    vol = coin.get("total_volume") or 0
    mcap = coin.get("market_cap") or 0
    ch1 = coin.get("price_change_percentage_1h_in_currency") or 0
    ch24 = coin.get("price_change_percentage_24h_in_currency") or 0
    if not p or not hoch or not tief:
        return None

    punkte, gruende, richtung = 0, [], "Beobachten"

    # 1) Ausbruch über 24h-Hoch bzw. unter 24h-Tief, mit Schwung
    if p >= hoch * 0.99 and ch1 >= AUSBRUCH_1H_PROZENT:
        punkte += 2
        richtung = "Long-Chance"
        gruende.append(f"Am 24h-Hoch ({preis(hoch)}), {ch1:+.1f} % in 1 Std.")
    elif p <= tief * 1.01 and ch1 <= -AUSBRUCH_1H_PROZENT:
        punkte += 2
        richtung = "Short-Chance"
        gruende.append(f"Am 24h-Tief ({preis(tief)}), {ch1:+.1f} % in 1 Std.")

    # 2) Volumen-Explosion seit der letzten Prüfung
    vorher = zustand["volumen"].get(coin["id"])
    if vorher:
        vorher_zeit, vorher_vol = vorher
        abstand = jetzt - vorher_zeit
        if 10 * 60 <= abstand <= 75 * 60 and vorher_vol > 0:
            sprung = (vol / vorher_vol - 1) * 100
            if sprung >= VOLUMEN_SPRUNG_PROZENT:
                punkte += 2
                minuten = round(abstand / 60)
                gruende.append(f"Volumen {sprung:+.0f} % in {minuten} Min.")

    # 3) Ungewöhnlich hoher Umsatz im Verhältnis zur Größe
    if mcap and vol / mcap >= UMSATZ_QUOTE:
        punkte += 1
        gruende.append(f"Sehr hoher Umsatz ({vol / mcap * 100:.0f} % der Marktkap.)")

    ist_meme = coin["id"] in meme_ids
    grenze = MIN_PUNKTE_MEME if ist_meme else MIN_PUNKTE_NORMAL
    if punkte < grenze:
        return None

    return {
        "id": coin["id"],
        "symbol": coin["symbol"].upper(),
        "name": coin["name"],
        "preis": p,
        "ch1": ch1,
        "ch24": ch24,
        "punkte": punkte,
        "richtung": richtung,
        "gruende": gruende,
        "meme": ist_meme,
    }


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------
def main():
    jetzt = time.time()
    zustand = lade_zustand()

    coins = hole_maerkte()
    try:
        meme_ids = {c["id"] for c in hole_maerkte(category="meme-token", per_page=250)}
    except Exception:
        meme_ids = set()  # Meme-Liste ist "nice to have"

    coins = [c for c in coins if c["symbol"].lower() not in IGNORIEREN]

    # Alte Sperren aufräumen
    zustand["gemeldet"] = {
        k: v for k, v in zustand["gemeldet"].items() if jetzt - v < 24 * 3600
    }

    treffer = []
    for coin in coins:
        signal = bewerte(coin, zustand, jetzt, meme_ids)
        gesperrt = jetzt - zustand["gemeldet"].get(coin["id"], 0) < SPERRE_STUNDEN * 3600
        if signal and not gesperrt:
            treffer.append(signal)

    # Volumen-Stand für den nächsten Vergleich merken
    # (nur überschreiben, wenn der alte Wert älter als 10 Min. ist)
    for coin in coins:
        alt = zustand["volumen"].get(coin["id"])
        if not alt or jetzt - alt[0] >= 10 * 60:
            zustand["volumen"][coin["id"]] = [jetzt, coin.get("total_volume") or 0]

    treffer.sort(key=lambda s: (s["punkte"], abs(s["ch1"])), reverse=True)

    for s in treffer[:MAX_MELDUNGEN]:
        zeilen = [
            f"{s['name']} – {preis(s['preis'])}",
            f"1 Std.: {s['ch1']:+.1f} %  |  24 Std.: {s['ch24']:+.1f} %",
            "",
            *[f"• {g}" for g in s["gruende"]],
            "",
        ]
        if s["meme"]:
            zeilen.append("Meme-Coin: extrem schwankend, Pump & Dump möglich!")
        zeilen.append("Kein Kaufbefehl – erst Chart prüfen, Stop festlegen.")

        if s["richtung"] == "Long-Chance":
            tags = ["chart_with_upwards_trend"]
        elif s["richtung"] == "Short-Chance":
            tags = ["chart_with_downwards_trend"]
        else:
            tags = ["eyes"]
        if s["meme"]:
            tags.append("frog")

        sende_push(
            titel=f"{s['symbol']}: {s['richtung']} (Stärke {s['punkte']}/5)",
            text="\n".join(zeilen),
            tags=tags,
            prioritaet=4 if s["punkte"] >= 4 else 3,
            link=f"https://www.coingecko.com/en/coins/{s['id']}",
        )
        zustand["gemeldet"][s["id"]] = jetzt

    if MANUELL and not treffer:
        sende_push(
            titel="Trading-Bot laeuft",
            text=f"{len(coins)} Coins geprüft – gerade keine starken Signale.",
            tags=["white_check_mark"],
            prioritaet=2,
        )

    speichere_zustand(zustand)
    print(f"{len(coins)} Coins geprüft, {len(treffer)} Signale, "
          f"{min(len(treffer), MAX_MELDUNGEN)} gemeldet.")


if __name__ == "__main__":
    main()
