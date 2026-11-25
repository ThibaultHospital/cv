
import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path


UMAMI_URL = os.environ["UMAMI_URL"].rstrip("/")
UMAMI_API_KEY = os.environ["UMAMI_API_KEY"]
WEBSITE_ID = os.environ["UMAMI_WEBSITE_ID"]

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
ALERT_EMAIL = os.environ["ALERT_EMAIL"]

STATE_FILE = Path("agents/.visiteurs_cv_state.json")
MAX_STORED_SESSIONS = 5000


def log(message):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def api_get(path, params=None):
    url = f"{UMAMI_URL}/api{path}"

    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {UMAMI_API_KEY}",
            "User-Agent": "GitHub-CV-Visitor-Monitor/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Erreur API Umami HTTP {error.code}: {body[:500]}"
        ) from error


def get_sessions(start, end):
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    result = api_get(
        f"/websites/{WEBSITE_ID}/sessions",
        {
            "startAt": start_ms,
            "endAt": end_ms,
            "page": 1,
            "pageSize": 100,
        },
    )

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        return result.get("data", [])

    raise RuntimeError("Format de réponse Umami inattendu.")


def get_activity(session_id, start, end):
    try:
        result = api_get(
            f"/websites/{WEBSITE_ID}/sessions/"
            f"{urllib.parse.quote(session_id, safe='')}/activity",
            {
                "startAt": int(start.timestamp() * 1000),
                "endAt": int(end.timestamp() * 1000),
            },
        )
        return result if isinstance(result, list) else []
    except Exception as error:
        log(f"Activité indisponible pour une session : {error}")
        return []


def load_state():
    if not STATE_FILE.exists():
        return None

    with STATE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(session_ids, initialized=True):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "initialized": initialized,
        "session_ids": list(dict.fromkeys(session_ids))[-MAX_STORED_SESSIONS:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(STATE_FILE)


def format_date(value):
    if not value:
        return "Non disponible"

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d/%m/%Y à %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


def describe_session(session, now):
    session_id = session.get("id")
    first_at = session.get("firstAt") or session.get("createdAt")

    activity = get_activity(
        session_id,
        now - timedelta(hours=1),
        now,
    ) if session_id else []

    pages = []
    referrers = []

    for event in activity:
        path = event.get("urlPath")
        if path and path not in pages:
            pages.append(path)

        referrer = event.get("referrerDomain")
        if referrer and referrer not in referrers:
            referrers.append(referrer)

    country = session.get("country") or "Non disponible"
    device = session.get("device") or "Non disponible"
    browser = session.get("browser") or "Non disponible"
    os_name = session.get("os") or "Non disponible"

    return {
        "date": format_date(first_at),
        "country": country,
        "device": device,
        "browser": browser,
        "os": os_name,
        "pages": pages[:5],
        "referrers": referrers[:3],
        "views": session.get("views", "Non disponible"),
    }


def send_email(sessions, now):
    message = EmailMessage()
    message["Subject"] = (
        f"🔔 {len(sessions)} nouvelle(s) session(s) sur ton CV"
    )
    message["From"] = GMAIL_USER
    message["To"] = ALERT_EMAIL

    lines = [
        "Bonjour Thibault,",
        "",
        f"{len(sessions)} nouvelle(s) session(s) détectée(s) sur ton CV.",
        f"Détection : {now.astimezone().strftime('%d/%m/%Y à %H:%M:%S')}",
        "",
    ]

    for index, session in enumerate(sessions, start=1):
        info = describe_session(session, now)

        lines.extend([
            f"VISITEUR {index}",
            f"Première visite : {info['date']}",
            f"Pays : {info['country']}",
            f"Appareil : {info['device']}",
            f"Navigateur : {info['browser']}",
            f"Système : {info['os']}",
            f"Pages consultées : {', '.join(info['pages']) or 'Non disponible'}",
            f"Provenance : {', '.join(info['referrers']) or 'Non disponible'}",
            f"Pages vues dans la session : {info['views']}",
            "",
        ])

    lines.extend([
        "Tableau de bord Umami :",
        UMAMI_URL,
        "",
        "Notification automatique de ton site CV.",
    ])

    message.set_content("\n".join(lines))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(message)

    log(f"Mail envoyé à {ALERT_EMAIL}.")


def main():
    now = datetime.now(timezone.utc)
    state = load_state()

    # Première exécution : on initialise l'historique sans alerte.
    if state is None or not state.get("initialized"):
        log("Première exécution : initialisation de l'historique.")

        historical_sessions = get_sessions(
            now - timedelta(hours=24),
            now,
        )

        ids = [
            item["id"]
            for item in historical_sessions
            if item.get("id")
        ]

        save_state(ids)
        log(f"Initialisation terminée : {len(ids)} sessions mémorisées.")
        return

    known_ids = set(state.get("session_ids", []))

    # Fenêtre de 30 minutes pour tolérer les retards de GitHub Actions.
    recent_sessions = get_sessions(
        now - timedelta(minutes=30),
        now,
    )

    new_sessions = [
        item for item in recent_sessions
        if item.get("id") and item["id"] not in known_ids
    ]

    if not new_sessions:
        log("Aucune nouvelle session.")
        return

    # Les plus anciennes sont présentées en premier.
    new_sessions.sort(
        key=lambda item: item.get("firstAt") or item.get("createdAt") or ""
    )

    log(f"{len(new_sessions)} nouvelle(s) session(s) détectée(s).")

    # En cas d'échec Gmail, on ne mémorise pas les sessions :
    # le prochain passage pourra retenter l'envoi.
    send_email(new_sessions, now)

    updated_ids = list(known_ids)
    updated_ids.extend(item["id"] for item in new_sessions)

    save_state(updated_ids)
    log("Historique des sessions mis à jour.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"ERREUR : {error}")
        sys.exit(1)
