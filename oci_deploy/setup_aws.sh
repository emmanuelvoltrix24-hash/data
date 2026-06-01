#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# AWS EC2 Bootstrap — VFL Prediction Engine
# Run ONCE on a fresh Amazon Linux 2023 EC2 instance
# ============================================================

REPO_URL="https://github.com/emmanuelvoltrix24-hash/data.git"
APP_DIR="/opt/vfl-deploy"
DB_NAME="vfl_data"
DB_USER="vfl_user"
DB_PASS="$(openssl rand -base64 24 | tr -d '\n/+= ' | head -c 24)"

echo "[1/6] System packages..."
dnf install -y -q postgresql15-server postgresql15-devel postgresql15-contrib python3-pip python3-devel git nginx
pip3 install --upgrade pip -q

echo "[2/6] PostgreSQL..."
/usr/bin/postgresql15-setup --initdb 2>/dev/null || true
systemctl enable --now postgresql15

# Configure PostgreSQL to trust local connections
sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" 2>/dev/null || true

DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"

echo "[3/6] Clone app..."
git clone ${REPO_URL} ${APP_DIR}
cd ${APP_DIR}
python3 -m venv venv
source venv/bin/activate
# Patch psycopg2-binary to work on Amazon Linux
pip install --only-binary psycopg2-binary psycopg2-binary -q 2>/dev/null || pip install psycopg2-binary -q
pip install -r requirements.txt -q

echo "[4/6] Environment..."
cat > /etc/systemd/system/vfl-app.env << ENVFILE
DATABASE_URL=${DATABASE_URL}
AWS=true
ENVFILE

echo "[5/6] Systemd services — app + learner..."
cp ${APP_DIR}/oci_deploy/vfl-app.service /etc/systemd/system/
cp ${APP_DIR}/oci_deploy/vfl-learner.service /etc/systemd/system/
cp ${APP_DIR}/oci_deploy/vfl-learner.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vfl-app vfl-learner.timer

echo "[6/6] Nginx reverse proxy..."
cat > /etc/nginx/nginx.conf << 'NGINX'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    server {
        listen 80;
        server_name _;
        location / {
            proxy_pass http://127.0.0.1:8080;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }
    }
}
NGINX
systemctl enable --now nginx

PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<your-ec2-public-ip>")

echo ""
echo "=========================================="
echo "  VFL DEPLOY COMPLETE"
echo "=========================================="
echo "  App:  http://${PUBLIC_IP}"
echo "  DB:   ${DATABASE_URL}"
echo "  Repo: ${REPO_URL}"
echo "=========================================="
echo ""
echo "To view logs: sudo journalctl -u vfl-app -f"
echo "To restart:   sudo systemctl restart vfl-app"
echo "=========================================="
