# atlassian-cloud-backups

Automate backups of Jira and Confluence Cloud.

This repository contains:

- **Legacy AWS Lambda scripts** in `confluence/`, `jira/`, and `packages/` (upload-ready ZIPs).
- **CLI/systemd backup tool** for Confluence — runs on any Linux server, stores backups locally, and uploads via rclone.

## Video Tutorials (Legacy Lambda)

- Jira: https://youtu.be/0zMJCQZe3xw
- Confluence: https://youtu.be/t_ODiHXi3us

---

## Confluence CLI Backup Tool

### Overview

`confluence_backup_cli.py` triggers a Confluence Cloud backup, polls until complete, and downloads the resulting ZIP to a local directory. A Bash wrapper script handles rclone upload and log management. A systemd timer runs it weekly.

### Prerequisites

- Debian/Ubuntu Linux server
- Python 3.10+
- `rclone` installed and configured with the target remote
- An Atlassian API token (create at https://id.atlassian.com/manage-profile/security/api-tokens)

### Installation

```bash
# 1. Clone the repo
sudo git clone https://github.com/agile-innovations-tech/atlassian-cloud-backups.git /opt/atlassian-cloud-backups

# 2. Create a Python venv and install dependencies
cd /opt/atlassian-cloud-backups
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Create directories
sudo mkdir -p /srv/confluence-snapshot
sudo mkdir -p /var/log/m365-backup
sudo mkdir -p /etc/m365-backup

# 4. Create the env file
sudo tee /etc/m365-backup/confluence.env > /dev/null <<'ENVEOF'
ATLASSIAN_SITE_URL="https://TENANT.atlassian.net"
ATLASSIAN_EMAIL="backup@example.com"
ATLASSIAN_API_TOKEN="your-api-token-here"
LOCAL_SNAPSHOT_ROOT="/srv/confluence-snapshot"
CONFLUENCE_DEST="hetzner-s3-infra-crypt:confluence-weekly"
LOG_FILE="/var/log/m365-backup/confluence-weekly.log"
ENVEOF
sudo chmod 600 /etc/m365-backup/confluence.env

# 5. Install the wrapper script
sudo install -m 755 scripts/backup-confluence-weekly.sh /usr/local/sbin/backup-confluence-weekly.sh

# 6. Install systemd units
sudo cp systemd/confluence-weekly-backup.service /etc/systemd/system/
sudo cp systemd/confluence-weekly-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### Configuration

Edit `/etc/m365-backup/confluence.env`:

| Variable | Description |
|---|---|
| `ATLASSIAN_SITE_URL` | Your Confluence site URL, e.g. `https://myco.atlassian.net` |
| `ATLASSIAN_EMAIL` | Email of the API token owner |
| `ATLASSIAN_API_TOKEN` | Atlassian API token |
| `LOCAL_SNAPSHOT_ROOT` | Local directory for daily snapshots |
| `CONFLUENCE_DEST` | rclone destination (remote:path) |
| `LOG_FILE` | Path to the log file |

Optional variables:

| Variable | Default | Description |
|---|---|---|
| `RETENTION_DAYS` | `14` | Days to keep local snapshots |
| `CLI_SCRIPT` | `/opt/atlassian-cloud-backups/confluence_backup_cli.py` | Path to the Python CLI |

### Wrapper script

The wrapper script at `scripts/backup-confluence-weekly.sh` (installed to `/usr/local/sbin/`) performs:

1. Sources the env file
2. Creates the date-stamped snapshot directory
3. Runs the Python CLI to trigger + download the backup
4. Uploads the snapshot via `rclone copy`
5. Verifies the upload via `rclone check --one-way --size-only`
6. Deletes local snapshots older than `RETENTION_DAYS`
7. Logs everything to `LOG_FILE`

### Manual Test

```bash
# Run directly (adjust the venv path in the wrapper or use the CLI directly):
sudo /usr/local/sbin/backup-confluence-weekly.sh

# Or run only the Python CLI:
/opt/atlassian-cloud-backups/.venv/bin/python3 \
    /opt/atlassian-cloud-backups/confluence_backup_cli.py \
    --site-url "https://TENANT.atlassian.net" \
    --email "backup@example.com" \
    --api-token "$ATLASSIAN_API_TOKEN" \
    --output-dir "/srv/confluence-snapshot/$(date -u +%Y-%m-%d)"
```

### Inspect Logs

```bash
tail -f /var/log/m365-backup/confluence-weekly.log
```

### Verify rclone Destination

```bash
rclone ls hetzner-s3-infra-crypt:confluence-weekly/
```

### Restore Test

```bash
# Copy a backup back from remote to /tmp for verification
rclone copy "hetzner-s3-infra-crypt:confluence-weekly/2026-05-16" /tmp/confluence-restore-test/
ls -la /tmp/confluence-restore-test/
```

### Enable systemd Timer

```bash
sudo systemctl enable --now confluence-weekly-backup.timer
# Verify:
systemctl list-timers confluence-weekly-backup.timer
```

### Local Output Format

Each snapshot directory contains:

```
/srv/confluence-snapshot/2026-05-16/
├── confluence-backup-2026-05-16.zip
└── metadata.json
```

`metadata.json` includes: timestamp, site URL, backup type, downloaded filename, size in bytes, Atlassian task/file reference, and tool version.

### Limitations

- This tool archives Atlassian/Confluence backup ZIPs using the undocumented `rest/obm/1.0` API.
- It **does not** use AWS Lambda anymore. The legacy Lambda scripts remain in the repo for reference.
- It **does not** directly write to S3 from Python. Upload is handled by rclone.
- Object Lock / retention policies are handled by the object storage bucket configuration, not by this script.
- If the configured rclone destination is a crypt remote, rclone crypt hides filenames and content from the storage provider.
- The Atlassian backup API is undocumented and unsupported. Behavior may change without notice.
- Only one Confluence backup can run at a time per site. Triggering a new backup while one is in progress may fail.

---

## Jira Backup (Legacy Lambda)

The Jira Lambda scripts in `jira/` remain unchanged. See the video tutorial above for Lambda-based usage.

## License

MIT — see [LICENSE](LICENSE).
