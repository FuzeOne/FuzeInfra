#!/bin/bash
# FuzeInfra EC2 Instance Bootstrap Script
# This script downloads and executes the full initialization script

set -euo pipefail

# Configuration
INSTANCE_NAME="fuzeinfra-instance"
ENVIRONMENT_NAME="production"

# Logging setup
LOG_FILE="/var/log/fuzeinfra-bootstrap.log"
exec > >(tee -a $LOG_FILE)
exec 2>&1

echo "=========================================="
echo "FuzeInfra Bootstrap Started"
echo "Instance: $INSTANCE_NAME"
echo "Environment: $ENVIRONMENT_NAME"
echo "Timestamp: $(date)"
echo "=========================================="

# Update system
apt-get update -y

# Install essential tools
apt-get install -y curl wget git unzip docker.io docker-compose

# Start Docker
systemctl start docker
systemctl enable docker
usermod -aG docker ubuntu

# Create FuzeInfra directory
mkdir -p /opt/fuzeinfra
cd /opt/fuzeinfra
chown ubuntu:ubuntu /opt/fuzeinfra

# Clone FuzeInfra repository
echo "📥 Cloning FuzeInfra repository..."
git clone https://github.com/FuzeOne/FuzeInfra.git .

# Set up environment
echo "🔧 Setting up FuzeInfra environment..."
cp docker-compose.FuzeInfra.yml docker-compose.yml

# Create environment file
cat > .env << EOF
POSTGRES_DB=fuzeinfra
POSTGRES_USER=admin  
POSTGRES_PASSWORD=admin123
MONGODB_USER=admin
MONGODB_PASSWORD=admin123
REDIS_PASSWORD=admin123
AIRFLOW_FERNET_KEY=32_character_fernet_key_here_12345
AIRFLOW_ADMIN_USER=admin
AIRFLOW_ADMIN_PASSWORD=admin123
DNSMASQ_ADMIN_PASSWORD=admin123
EOF

# Start services
echo "🚀 Starting FuzeInfra services..."
docker network create FuzeInfra 2>/dev/null || true
docker-compose up -d

echo "✅ FuzeInfra Bootstrap Complete"
echo "=========================================="