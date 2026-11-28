
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
from zoneinfo import ZoneInfo


UMAMI_URL = os.environ["UMAMI_URL"].rstrip("/")
UMAMI_API_KEY = os.environ["UMAMI_API_KEY"]
WEBSITE_ID = os.environ["UMAMI_WEBSITE_ID"]

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
ALERT_EMAIL = os.environ["ALERT_EMAIL"]

STATE_FILE = Path("agents/.visiteurs_cv_state.json")
MAX_STORED_SESSIONS = 5000
PARIS = ZoneInfo("Europe/Paris")


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
            "User-Agent": "GitHub-CV-Visitor-Monitor/2.0",
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
    result = api_get(
        f"/websites/{WEBSITE_ID}/sessions",
        {
            "startAt": int(start.timestamp() * 1000),
            "endAt": int(end.timestamp() * 1000),
            "page": 1,
            "pageSize": 100,
        },
    )

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        return result.get("data", [])

    raise RuntimeError("Réponse inattendue de l'API Umami.")


def get_session_details(session_id):
    return api_get(
        f"/websites/{WEBSITE_ID}/sessions/"
        f"{urllib.parse.quote(session_id, safe='')}"
    )


def get_activity(session_id, first_at, last_at):
    start = first_at - timedelta(minutes=5)
    end = last_at + timedelta(minutes=5)

    result = api_get(
        f"/websites/{WEBSITE_ID}/sessions/"
        f"{urllib.parse.quote(session_id, safe='')}/activity",
        {
            "startAt": int(start.timestamp() * 1000),
            "endAt": int(end.timestamp() * 1000),
        },
    )

    return result if isinstance(result, list) else []


def parse_date(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return None


def format_date(value):
    date = parse_date(value)

    if not date:
        return "Non disponible"

    return date.astimezone(PARIS).strftime("%d/%m/%Y à %H:%M:%S")


def format_duration(seconds):
    if seconds is None:
        return "Non disponible"

    try:
        seconds = max(0, int(seconds))
    except (ValueError, TypeError):
        return "Non disponible"

    minutes, seconds = divmod(seconds, 60)

    if minutes:
        return f"{minutes} min {seconds} s"

    return f"{seconds} s"


def safe_value(value):
    if value is None or value == "":
        return "Non disponible"
    return str(value)


def extract_campaign(activity):
    campaigns = {}

    for event in activity:
        query = event.get("urlQuery") or ""
        values = urllib.parse.parse_qs(query)

        for key in (
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "utm_term",
        ):
            if values.get(key):
                campaigns[key] = values[key][0]

    return campaigns


def build_report(session):
    session_id = session.get("id")

    details = {}
    activity = []

    try:
        details = get_session_details(session_id)
    except Exception as error:
        log(f"Détails indisponibles pour une session : {error}")

    first_at = parse_date(
        details.get("firstAt") or session.get("firstAt")
    )
    last_at = parse_date(
        details.get("lastAt") or session.get("lastAt")
    )

    if first_at and last_at:
        try:
            activity = get_activity(session_id, first_at, last_at)
        except Exception as error:
            log(f"Activité indisponible : {error}")

    pages = []
    events = []
    referrers = []

    for item in activity:
        path = item.get("urlPath") or "/"
        title = item.get("pageTitle") or ""
        timestamp = format_date(item.get("createdAt"))

        if item.get("eventType") == 2:
            event_name = item.get("eventName") or "Événement sans nom"
            events.append(f"{event_name} ({path})")
            continue

        label = f"{title} — {path}" if title else path

        if label not in [page["label"] for page in pages]:
            pages.append({
                "label": label,
                "time": timestamp,
            })

        domain = item.get("referrerDomain")
        ref_path = item.get("referrerPath") or ""

        if domain:
            source = domain + ref_path
            if source not in referrers:
                referrers.append(source)

    campaigns = extract_campaign(activity)

    subdivision = (
        details.get("subdivision1")
        or details.get("region")
        or session.get("subdivision1")
        or session.get("region")
    )

    city = details.get("city") or session.get("city")

    location = [
        value for value in (city, subdivision, details.get("country") or session.get("country"))
        if value
    ]

    return {
        "first_at": format_date(details.get("firstAt") or session.get("firstAt")),
        "last_at": format_date(details.get("lastAt") or session.get("lastAt")),
        "location": " – ".join(location) or "Non disponible",
        "country": safe_value(details.get("country") or session.get("country")),
        "device": safe_value(details.get("device") or session.get("device")),
        "browser": safe_value(details.get("browser") or session.get("browser")),
        "os": safe_value(details.get("os") or session.get("os")),
        "language": safe_value(details.get("language") or session.get("language")),
        "screen": safe_value(details.get("screen") or session.get("screen")),
        "hostname": safe_value(details.get("hostname") or session.get("hostname")),
        "duration": format_duration(details.get("totaltime")),
        "visits": safe_value(details.get("visits") or session.get("visits")),
        "views": safe_value(details.get("views") or session.get("views")),
        "events_count": safe_value(details.get("events") or session.get("events")),
        "pages": pages,
        "events": events,
        "referrers": referrers,
        "campaigns": campaigns,
    }


def send_email(sessions, now):
    reports = []

    for session in sessions:
        reports.append(build_report(session))

    message = EmailMessage()
    message["Subject"] = (
        f"🔔 {len(reports)} nouvelle(s) visite(s) sur ton CV"
    )
    message["From"] = GMAIL_USER
    message["To"] = ALERT_EMAIL

    lines = [
        "Bonjour Thibault,",
        "",
        f"{len(reports)} nouvelle(s) session(s) détectée(s) sur ton CV.",
        f"Détection : {now.astimezone(PARIS).strftime('%d/%m/%Y à %H:%M:%S')}",
        "",
        "=" * 48,
        "",
    ]

    for index, info in enumerate(reports, start=1):
        lines.extend([
            f"VISITEUR {index}",
            "-" * 48,
            "",
            "LOCALISATION",
            f"Zone estimée : {info['location']}",
            "",
            "APPAREIL",
            f"Type : {info['device']}",
            f"Système : {info['os']}",
            f"Navigateur : {info['browser']}",
            f"Langue : {info['language']}",
            f"Résolution : {info['screen']}",
            "",
            "ACTIVITÉ",
            f"Première visite : {info['first_at']}",
            f"Dernière activité : {info['last_at']}",
            f"Durée : {info['duration']}",
            f"Nombre de visites : {info['visits']}",
            f"Pages vues : {info['views']}",
            f"Événements : {info['events_count']}",
            "",
            "PROVENANCE",
        ])

        if info["referrers"]:
            lines.extend(f"- {item}" for item in info["referrers"])
        else:
            lines.append("Accès direct ou provenance indisponible.")

        if info["campaigns"]:
            lines.extend([
                "",
                "CAMPAGNE",
            ])
            for key, value in info["campaigns"].items():
                lines.append(f"- {key}: {value}")

        lines.extend([
            "",
            "PARCOURS SUR LE CV",
        ])

        if info["pages"]:
            for page in info["pages"]:
                lines.append(f"- {page['time']} : {page['label']}")
        else:
            lines.append("Détail des pages indisponible.")

        if info["events"]:
            lines.extend([
                "",
                "ÉVÉNEMENTS DÉTECTÉS",
            ])
            lines.extend(f"- {event}" for event in info["events"])

        lines.extend(["", "=" * 48, ""])

    lines.extend([
        "Tableau de bord Umami :",
        UMAMI_URL,
        "",
        "Rapport automatique de surveillance du CV.",
        "Les informations géographiques sont approximatives.",
    ])

    message.set_content("\n".join(lines))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(message)

    log(f"Mail envoyé à {ALERT_EMAIL}.")


def load_state():
    if not STATE_FILE.exists():
        return None

    with STATE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(session_ids):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "initialized": True,
        "session_ids": list(dict.fromkeys(session_ids))[-MAX_STORED_SESSIONS:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(STATE_FILE)


def main():
    now = datetime.now(timezone.utc)
    state = load_state()

    if state is None or not state.get("initialized"):
        log("Initialisation de l'historique des sessions.")

        historical = get_sessions(
            now - timedelta(hours=24),
            now,
        )

        ids = [
            item["id"]
            for item in historical
            if item.get("id")
        ]

        save_state(ids)
        log(f"{len(ids)} sessions mémorisées. Aucune alerte initiale.")
        return



    known_ids = set(state.get("session_ids", []))

    # Recherche sur les dernières 24 heures pour éviter
    # de manquer les sessions remontées tardivement par Umami.
    recent_sessions = get_sessions(
        now - timedelta(hours=24),
        now,
    )

    log(f"Sessions retournées par Umami : {len(recent_sessions)}")

    if recent_sessions:
        log(
            "Sessions trouvées : "
            + ", ".join(
                f"{item.get('firstAt')} | {item.get('id')}"
                for item in recent_sessions[:10]
            )
        )

    new_sessions = [
        item for item in recent_sessions
        if item.get("id") and item["id"] not in known_ids
    ]

    if not new_sessions:
        log(
            f"Aucune nouvelle session. "
            f"{len(recent_sessions)} session(s) récupérée(s), "
            f"{len(known_ids)} ID(s) déjà mémorisé(s)."
        )
        return

    new_sessions.sort(
        key=lambda item: item.get("firstAt") or ""
    )

    log(f"{len(new_sessions)} nouvelle(s) session(s).")

    # Envoi avant sauvegarde : si Gmail échoue, le prochain passage
    # pourra retenter l'envoi.
    send_email(new_sessions, now)

    updated_ids = list(known_ids)
    updated_ids.extend(item["id"] for item in new_sessions)

    save_state(updated_ids)
    log("Historique mis à jour.")




if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"ERREUR : {error}")
        sys.exit(1)
