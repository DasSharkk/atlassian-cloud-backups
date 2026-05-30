# Atlassian Confluence Cloud Backup to S3-Compatible Object Storage

Dieses Setup sichert Atlassian Confluence Cloud (Full-Site-Backup) wöchentlich in S3-kompatibles Object Storage.

Aktueller Zielzustand:

```text
Confluence Cloud Site
        ↓
Python CLI (Trigger → Poll → Download)
        ↓
lokaler Snapshot
        ↓
rclone crypt
        ↓
Hetzner Object Storage Bucket mit Object Lock
        ↓
später zusätzlich IONOS / drittes Ziel
```

## Prinzip

Es wird **kein destruktiver Sync** verwendet.

Stattdessen wird pro Backup-Lauf ein neuer datierter Snapshot geschrieben:

```text
confluence-weekly/
  2026-05-16/
    confluence-backup-2026-05-16.zip
    metadata.json
  2026-05-23/
    confluence-backup-2026-05-23.zip
    metadata.json
```

Dadurch überschreibt oder löscht ein Backup-Lauf keine alten Backup-Sätze.

## Komponenten

| Komponente | Zweck |
|---|---|
| `confluence_backup_cli.py` | Python CLI — triggert Backup, pollt Status, lädt ZIP herunter |
| `backup-confluence-weekly.sh` | Bash-Wrapper — steuert CLI, rclone-Upload, Cleanup |
| `rclone` | Upload in S3-kompatibles Object Storage |
| `rclone crypt` | clientseitige Verschlüsselung von Dateinamen und Inhalten |
| Hetzner Object Storage | erstes Backup-Ziel |
| Object Lock | schützt Objektversionen gegen Löschen innerhalb der Retention |
| systemd Timer | startet das Backup automatisch wöchentlich |

## Atlassian API

Das Backup nutzt die undokumentierte OBM-API (Online Backup Manager):

```text
POST /wiki/rest/obm/1.0/runbackup     → Backup triggern
GET  /wiki/rest/obm/1.0/getprogress.json → Status abfragen
GET  /wiki/download/{fileName}         → ZIP herunterladen
```

Authentifizierung: HTTP Basic Auth mit E-Mail + API Token.

API Token erstellen: https://id.atlassian.com/manage-profile/security/api-tokens

Es wird ein dedizierter Managed Account empfohlen (z.B. `backup@example.com`), kein persönlicher User.

## Remotes

Beispielhafte rclone-Remotes:

```text
hetzner-s3-infra:
hetzner-s3-infra-crypt:
```

Bedeutung:

```text
hetzner-s3-infra:
  Raw S3-kompatibler Hetzner-Bucket

hetzner-s3-infra-crypt:
  Verschlüsselte Sicht auf den Hetzner-Bucket
```

Die produktiven Backups sollten über die `crypt`-Remote geschrieben werden.

## Object Lock

Der Bucket muss mit Object Lock erstellt worden sein. Object Lock kann bei S3-kompatiblen Buckets in der Regel nicht nachträglich aktiviert werden.

Default Retention prüfen:

```bash
mc retention info --default hetzner/titanom-backup
```

Default Retention setzen, Beispiel 60 Tage:

```bash
mc retention set GOVERNANCE 60d --default hetzner/titanom-backup
```

Für Tests kann temporär 1 Tag gesetzt werden:

```bash
mc retention set GOVERNANCE 1d --default hetzner/titanom-backup
```

Nach Tests wieder produktiv setzen:

```bash
mc retention set GOVERNANCE 60d --default hetzner/titanom-backup
```

### Wichtiges Verhalten bei Delete

Bei Versioning + Object Lock kann ein normaler Delete trotzdem erfolgreich aussehen:

```text
rclone deletefile ...
INFO : file.txt: Deleted
```

Das bedeutet meistens nur, dass ein **Delete Marker** erstellt wurde.

Die geschützte Objektversion bleibt erhalten.

Versionen prüfen:

```bash
mc ls --versions hetzner/titanom-backup/<prefix>/
```

Eine geschützte Version sollte vor Ablauf der Retention nicht löschbar sein:

```bash
mc rm --vid '<VERSION_ID>' hetzner/titanom-backup/<path-to-file>
```

Erwartung:

```text
AccessDenied
```

## Installation

```bash
# 1. Repo klonen
sudo git clone https://github.com/DasSharkk/atlassian-cloud-backups.git /opt/atlassian-cloud-backups

# 2. Python venv erstellen
cd /opt/atlassian-cloud-backups
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Verzeichnisse anlegen
sudo mkdir -p /srv/confluence-snapshot
sudo mkdir -p /var/log/m365-backup
sudo mkdir -p /etc/m365-backup

# 4. Env-Datei erstellen
sudo tee /etc/m365-backup/confluence.env > /dev/null <<'EOF'
ATLASSIAN_SITE_URL="https://TENANT.atlassian.net"
ATLASSIAN_EMAIL="backup@example.com"
ATLASSIAN_API_TOKEN="your-api-token-here"
LOCAL_SNAPSHOT_ROOT="/srv/confluence-snapshot"
CONFLUENCE_DESTS="hetzner-s3-infra-crypt:confluence-weekly"
LOG_FILE="/var/log/m365-backup/confluence-weekly.log"
EOF
sudo chmod 600 /etc/m365-backup/confluence.env

# 5. Wrapper-Script installieren
sudo install -m 700 scripts/backup-confluence-weekly.sh /usr/local/sbin/backup-confluence-weekly.sh

# 6. systemd Units installieren
sudo cp systemd/confluence-weekly-backup.service /etc/systemd/system/
sudo cp systemd/confluence-weekly-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

## Konfiguration

Env-Datei:

```text
/etc/m365-backup/confluence.env
```

| Variable | Beschreibung |
|---|---|
| `ATLASSIAN_SITE_URL` | Confluence-Site-URL, z.B. `https://myco.atlassian.net` |
| `ATLASSIAN_EMAIL` | E-Mail des API-Token-Owners |
| `ATLASSIAN_API_TOKEN` | Atlassian API Token |
| `LOCAL_SNAPSHOT_ROOT` | Lokales Verzeichnis für Snapshots |
| `CONFLUENCE_DESTS` | rclone-Ziele, kommagetrennt (remote:path,remote:path,...) |
| `LOG_FILE` | Pfad zur Log-Datei |

Optionale Variablen:

| Variable | Default | Beschreibung |
|---|---|---|
| `RETENTION_DAYS` | `14` | Tage, die lokale Snapshots aufbewahrt werden |
| `CLI_SCRIPT` | `/opt/atlassian-cloud-backups/confluence_backup_cli.py` | Pfad zum Python CLI |
| `PYTHON` | `/opt/atlassian-cloud-backups/.venv/bin/python3` | Pfad zum Python-Interpreter |

## Backup-Script

Pfad:

```text
/usr/local/sbin/backup-confluence-weekly.sh
```

Das Script führt folgende Schritte aus:

1. Env-Datei laden und Variablen prüfen
2. Snapshot-Verzeichnis anlegen (`/srv/confluence-snapshot/YYYY-MM-DD/`)
3. Python CLI ausführen (Backup triggern, Status pollen, ZIP herunterladen)
4. Snapshot mit `rclone copy` hochladen
5. Upload mit `rclone check --one-way --size-only` verifizieren
6. Lokale Snapshots älter als `RETENTION_DAYS` löschen
7. Alles nach `LOG_FILE` loggen

## Lokales Output-Format

Jeder Snapshot enthält:

```text
/srv/confluence-snapshot/2026-05-16/
├── confluence-backup-2026-05-16.zip
└── metadata.json
```

`metadata.json` Beispiel:

```json
{
  "timestamp": "2026-05-16T23:30:00+00:00",
  "site_url": "https://titanom.atlassian.net",
  "backup_type": "confluence",
  "downloaded_filename": "confluence-backup-2026-05-16.zip",
  "size_bytes": 12359278429,
  "atlassian_task_file": "temp/filestore/e5e4f74a-...",
  "tool": "atlassian-cloud-backups",
  "tool_version": "2.0.0",
  "date": "2026-05-16"
}
```

## Manuelles Backup starten

```bash
sudo /usr/local/sbin/backup-confluence-weekly.sh
```

Oder nur den Python CLI direkt:

```bash
/opt/atlassian-cloud-backups/.venv/bin/python3 \
    /opt/atlassian-cloud-backups/confluence_backup_cli.py \
    --site-url "https://TENANT.atlassian.net" \
    --email "backup@example.com" \
    --api-token "$ATLASSIAN_API_TOKEN" \
    --output-dir "/srv/confluence-snapshot/$(date -u +%Y-%m-%d)"
```

Live-Log:

```bash
sudo tail -f /var/log/m365-backup/confluence-weekly.log
```

## Backup prüfen

Aktuelles Datum:

```bash
DATE="$(date +%F)"
```

Ziel listen:

```bash
sudo rclone lsd "hetzner-s3-infra-crypt:confluence-weekly/"
```

Dateien im Snapshot:

```bash
sudo rclone ls "hetzner-s3-infra-crypt:confluence-weekly/$DATE/"
```

Größe prüfen:

```bash
sudo rclone size "hetzner-s3-infra-crypt:confluence-weekly/$DATE"
```

## Restore-Test

Lokalen Restore-Testordner anlegen:

```bash
mkdir -p /tmp/confluence-restore-test
```

Backup lokal zurückkopieren:

```bash
DATE="2026-05-16"

sudo rclone copy \
  "hetzner-s3-infra-crypt:confluence-weekly/$DATE" \
  /tmp/confluence-restore-test \
  --log-level INFO
```

Prüfen:

```bash
ls -la /tmp/confluence-restore-test/
du -sh /tmp/confluence-restore-test/
```

Die ZIP-Datei kann über die Confluence-Admin-Oberfläche importiert werden (Site Administration → Backup & Restore).

## systemd Service

Pfad:

```text
/etc/systemd/system/confluence-weekly-backup.service
```

Inhalt:

```ini
[Unit]
Description=Confluence Cloud Weekly Backup
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/backup-confluence-weekly.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/srv/confluence-snapshot /var/log/m365-backup
PrivateTmp=true
```

## systemd Timer

Pfad:

```text
/etc/systemd/system/confluence-weekly-backup.timer
```

Inhalt:

```ini
[Unit]
Description=Weekly Confluence Cloud Backup Timer

[Timer]
OnCalendar=Sun 04:30
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
```

Aktivieren:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now confluence-weekly-backup.timer
```

Status prüfen:

```bash
systemctl list-timers | grep confluence
systemctl status confluence-weekly-backup.timer
```

Manuell via systemd starten:

```bash
sudo systemctl start confluence-weekly-backup.service
```

Logs:

```bash
journalctl -u confluence-weekly-backup.service -n 100 --no-pager
sudo tail -f /var/log/m365-backup/confluence-weekly.log
```

## Download-Verhalten bei großen Backups

Der Python CLI unterstützt automatisches Retry mit Resume bei Verbindungsabbrüchen:

- Bis zu 5 Wiederholungsversuche
- HTTP `Range`-Header für Resume ab der letzten Position
- Inkrementelles Backoff (30s, 60s, 90s, 120s)
- Wichtig bei großen Sites (>10 GB)

## Weitere Backup-Ziele hinzufügen

Einfach in der env-Datei kommagetrennt ergänzen:

```bash
CONFLUENCE_DESTS="hetzner-s3-infra-crypt:confluence-weekly,ionos-s3-crypt:confluence-weekly,third-s3-crypt:confluence-weekly"
```

Das Script iteriert automatisch über alle Einträge und führt pro Ziel `rclone copy` + `rclone check` aus.

## Was gesichert wird

Das Confluence-Cloud-Full-Site-Backup enthält:

```text
Spaces (alle)
Seiten und Blogposts
Anhänge (optional, Standard: ein)
Kommentare
Benutzer und Gruppen
Space-Berechtigungen
```

Nicht enthalten:

```text
Marketplace-App-Daten (je nach App)
Analytics-Daten
Audit-Logs
Automation-Regeln
```

Dieses Setup ist ein Archiv der Confluence-Site-Backups, kein vollständiges Disaster-Recovery-System. Ein Restore erfolgt über die Confluence-Admin-Oberfläche.

## Limitierungen

- Das Backup nutzt die undokumentierte `rest/obm/1.0`-API. Verhalten kann sich ohne Vorankündigung ändern.
- Es wird **kein AWS Lambda** mehr verwendet. Die Legacy-Lambda-Scripts bleiben im Repo als Referenz.
- Es wird **kein direkter S3-Upload aus Python** durchgeführt. Upload erfolgt über rclone.
- Object Lock / Retention Policies werden durch die Bucket-Konfiguration gesteuert, nicht durch dieses Script.
- Bei einer `crypt`-Remote verschlüsselt rclone Dateinamen und Inhalte clientseitig.
- Pro Confluence-Site kann nur ein Backup gleichzeitig laufen. Ein erneutes Triggern während eines laufenden Backups wird automatisch erkannt und übersprungen.
- Full-Site-Backups können bei großen Sites mehrere Stunden dauern und >10 GB groß sein.

## Betriebsregeln

Keine destruktiven rclone-Befehle im produktiven Backup verwenden:

```text
nicht verwenden:
rclone sync
rclone delete
rclone purge
```

Verwenden:

```text
rclone copy
rclone check
rclone size
rclone lsd
rclone ls
rclone lsf
```

Object-Lock-Retention muss vor produktiven Backups korrekt gesetzt sein.

Restore muss regelmäßig getestet werden.
