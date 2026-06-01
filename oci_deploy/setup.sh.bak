#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# OCI Bootstrap — VFL Prediction Engine
# Run ONCE on a fresh Ubuntu 24.04 OCI compute instance
# ============================================================

REPO_URL="https://github.com/emmanuelvoltrix24-hash/data.git"
APP_DIR="/opt/vfl-deploy"
DB_NAME="vfl_data"
DB_USER="vfl_user"
DB_PASS="$(openssl rand -base64 24 | tr -d '\n/+= ' | head -c 24)"

echo "[1/6] System packages..."
apt-get update -qq
apt-get install -y -qq postgresql postgresql-client python3-pip python3-venv git nginx

echo "[2/6] PostgreSQL..."
systemctl start postgresql
sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" 2>/dev/null || true

DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"

echo "[3/6] Clone app..."
git clone ${REPO_URL} ${APP_DIR}
cd ${APP_DIR}
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -q

echo "[4/6] Environment..."
cat > /etc/systemd/system/vfl-app.env << ENVFILE
DATABASE_URL=${DATABASE_URL}
OCI=true
ENVFILE

echo "[5/6] Systemd services — app + learner + backup..."
cp ${APP_DIR}/oci_deploy/vfl-app.service /etc/systemd/system/
cp ${APP_DIR}/oci_deploy/vfl-learner.service /etc/systemd/system/
cp ${APP_DIR}/oci_deploy/vfl-learner.timer /etc/systemd/system/
cp ${APP_DIR}/oci_deploy/vfl-backup.service /etc/systemd/system/
cp ${APP_DIR}/oci_deploy/vfl-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vfl-app vfl-learner.timer vfl-backup.timer

echo "[6/6] Nginx reverse proxy..."
cat > /etc/nginx/sites-available/vfl << 'NGINX'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
    location /static/ {
        alias /opt/vfl-deploy/dashboard/;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/vfl /etc/nginx/sites-enabled/
nginx -t && systemctl restart nginx

PUBLIC_IP=$(curl -s http://169.254.169.254/opc/v1/instance/ 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('publicIp','<ip>'))" 2>/dev/null || echo "<your-instance-ip>")

echo ""
echo "=========================================="
echo "  VFL DEPLOY COMPLETE"
echo "=========================================="
echo "  App:  http://${PUBLIC_IP}"
echo "  DB:   ${DATABASE_URL}"
echo "  Repo: ${REPO_URL}"
echo "=========================================="
echo ""
echo "To view logs: journalctl -u vfl-app -f"
echo "To restart:   systemctl restart vfl-app"
echo "=========================================="
