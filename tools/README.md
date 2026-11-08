# Local Development Orchestrator

A comprehensive system to eliminate port conflicts and provide clean URLs for multiple Docker projects running in parallel. The system provides automatic port allocation, nginx configuration management, DNS routing, and seamless project deployment.

## 🎯 Features

- **Automatic Port Allocation**: Scans and allocates available ports automatically
- **Clean URLs**: Access projects via `http://projectname.dev.local` instead of `localhost:3001`
- **Zero Port Conflicts**: Intelligent port management prevents conflicts
- **Environment Management**: Automatic .env file generation with port injection
- **Nginx Proxy**: Centralized nginx instance with automatic configuration
- **DNS Management**: Automatic hosts file management for local domains
- **Single Command Deployment**: Deploy entire projects with one command

## 🚀 Quick Start

### Prerequisites

- Python 3.6+
- Docker and Docker Compose
- Administrator/sudo privileges (for DNS management)

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/FuzeOne/FuzeInfra.git
cd FuzeInfra
```

2. **Initialize the envmanager submodule**
```bash
git submodule update --init --recursive
```

3. **Install Python dependencies**
```bash
pip install PyYAML
```

### Deploy Your First Project

1. **Prepare your project**
   - Ensure your project has a `docker-compose.yml` file
   - Place it in the `projects/` directory or specify the path

2. **Deploy the project**
   ```bash
   # Linux/macOS
   ./tools/orchestrator/deploy-project.sh myproject
   
   # Windows
   tools\orchestrator\deploy-project.bat myproject
   ```

3. **Access your project**
   - Open http://myproject.dev.local in your browser
   - No more remembering port numbers!

## 📁 Project Structure

```
FuzeInfra/
├── tools/
│   ├── port-allocator/          # Port discovery and allocation
│   ├── env-manager/             # Environment variable injection
│   ├── nginx-generator/         # Nginx configuration generation
│   ├── dns-manager/             # Local DNS management
│   └── orchestrator/            # Main deployment scripts
├── infrastructure/
│   └── shared-nginx/            # Shared nginx container setup
├── projects/                    # Your projects go here
└── envmanager/                  # Git submodule for env management
```

## 🔧 Components

### 1. Port Allocator (`tools/port-allocator/`)

Automatically discovers available ports and allocates blocks for projects.

```bash
# Allocate ports for a project
python tools/port-allocator/port-allocator.py allocate myproject --num-ports 5

# Scan available ports
python tools/port-allocator/port-allocator.py scan --start-port 3000 --end-port 8000

# Validate port availability
python tools/port-allocator/port-allocator.py validate --ports "3000,3001,5000"
```

### 2. Environment Manager (`tools/env-manager/`)

Integrates with the existing EnvManager to inject ports into .env files.

```bash
# Inject ports into project .env file
python tools/env-manager/env-injector.py inject /path/to/project --ports '{"frontend":3000,"backend":5000}'

# Validate project environment
python tools/env-manager/env-injector.py validate /path/to/project
```

### 3. Nginx Generator (`tools/nginx-generator/`)

Generates project-specific nginx configurations.

```bash
# Generate nginx config
python tools/nginx-generator/nginx-generator.py generate \
  --project-config '{"name":"myproject","type":"fullstack"}' \
  --ports '{"frontend":3000,"backend":5000}'

# Reload nginx
python tools/nginx-generator/nginx-generator.py reload
```

### 4. DNS Manager (`tools/dns-manager/`)

Manages local DNS entries for `.dev.local` domains.

```bash
# Add DNS entry (requires admin/sudo)
python tools/dns-manager/dns-manager.py add myproject

# Remove DNS entry
python tools/dns-manager/dns-manager.py remove myproject
```

## 📋 Project Configuration

**No additional configuration files needed!** The orchestrator automatically detects your project structure from your existing `docker-compose.yml` file.

### 📋 Port Naming Conventions

**IMPORTANT**: When using port placeholders in your `docker-compose.yml` and nginx configuration files, follow these naming conventions:

- **Use `PORT_` prefix or `_PORT` suffix** for all port variables
- **Examples**: `FRONTEND_PORT`, `BACKEND_PORT`, `DATABASE_PORT`, `REDIS_PORT`, `API_PORT`
- **Avoid**: Generic names like `HTTP_PORT`, `SERVICE_PORT` (be specific about the service)

**Supported patterns**:
- `${SERVICE_NAME_PORT}` (recommended)
- `${PORT_SERVICE_NAME}` (alternative)
- Both patterns will be automatically detected by the port allocator

Your `docker-compose.yml` should use environment variables for ports:

```yaml
version: '3.8'
services:
  frontend:
    build: ./frontend
    ports:
      - "${FRONTEND_PORT}:3000"
    
  backend:
    build: ./backend
    ports:
      - "${BACKEND_PORT}:5000"
    
  database:
    image: postgres:13
    ports:
      - "${DATABASE_PORT}:5432"
    environment:
      POSTGRES_DB: myapp
      
  redis:
    image: redis:alpine
    ports:
      - "${REDIS_PORT}:6379"
    
  # Services with multiple ports
  monitoring:
    image: prometheus:latest
    ports:
      - "${PROMETHEUS_PORT}:9090"
      - "${PROMETHEUS_METRICS_PORT}:9100"
```

The orchestrator will:
- **Automatically allocate ports** for each service
- **Inject port variables** into your `.env` file
- **Generate nginx configuration** based on detected services
- **Set up DNS routing** for clean URLs

## 🌐 Nginx Configuration

The orchestrator uses a **smart, universal nginx template** that automatically handles:

- **Frontend applications** (port 3000-3999) → Root path `/`
- **Backend APIs** (port 5000-5999) → API path `/api/`
- **WebSocket support** for development servers
- **SPA routing** with fallback handling
- **Health checks** and monitoring endpoints
- **Static asset caching** for production-ready performance

No manual nginx configuration needed!

## 🛠️ Manual Usage

### Start Shared Nginx

```bash
cd infrastructure/shared-nginx
docker-compose up -d
```

### Deploy Step by Step

```bash
# 1. Allocate ports
PORTS=$(python tools/port-allocator/port-allocator.py allocate myproject 5)

# 2. Inject environment
python tools/env-manager/env-injector.py inject ./projects/myproject --ports "$PORTS"

# 3. Generate nginx config
python tools/nginx-generator/nginx-generator.py generate \
  --project-name myproject \
  --compose-file ./projects/myproject/docker-compose.yml \
  --ports "$PORTS"

# 4. Update DNS
sudo python tools/dns-manager/dns-manager.py add myproject

# 5. Reload nginx
python tools/nginx-generator/nginx-generator.py reload

# 6. Start project
cd ./projects/myproject
docker-compose up -d
```

## 🔍 Troubleshooting

### Port Conflicts
- The system automatically finds available ports
- Check `tools/port-allocator/config.yaml` to adjust port ranges

### DNS Issues
- **Windows**: Run Command Prompt as Administrator
- **Linux/macOS**: Use `sudo` for DNS commands
- **Manual**: Add `127.0.0.1 myproject.dev.local` to your hosts file

### Nginx Issues
- Check nginx logs: `docker logs fuzeinfra-shared-nginx`
- Validate config: `python tools/nginx-generator/nginx-generator.py validate`
- Restart nginx: `docker restart fuzeinfra-shared-nginx`

### Permission Issues
- **Windows**: Run as Administrator
- **Linux/macOS**: Use `sudo` for system file modifications

## 📊 Configuration Files

### Port Allocator Config (`tools/port-allocator/config.yaml`)
```yaml
port_ranges:
  - start: 3000
    end: 3999
    description: "Frontend applications"
  - start: 5000
    end: 5999
    description: "Backend APIs"
consecutive_block_size: 5
excluded_ports: [5432, 6379, 3306, 27017]
```

### Environment Manager Config (`tools/env-manager/config.yaml`)
```yaml
port_variable_mapping:
  frontend: "FRONTEND_PORT"
  backend: "BACKEND_PORT"
  database: "DATABASE_PORT"
  cache: "CACHE_PORT"
additional_variables:
  HOST: "0.0.0.0"
  NODE_ENV: "development"
```

### DNS Manager Config (`tools/dns-manager/config.yaml`)
```yaml
domain_suffix: "dev.local"
default_ip: "127.0.0.1"
backup_enabled: true
comment_prefix: "# Local Dev Orchestrator:"
```

## 🔄 Cleanup

### Remove Project
```bash
# Remove project deployment
python tools/dns-manager/dns-manager.py remove myproject
python tools/nginx-generator/nginx-generator.py remove myproject
python tools/nginx-generator/nginx-generator.py reload

# Stop project containers
cd projects/myproject
docker-compose down
```

### Complete Cleanup
```bash
# Stop shared nginx
cd infrastructure/shared-nginx
docker-compose down

# Remove all managed DNS entries (requires admin/sudo)
python tools/dns-manager/dns-manager.py cleanup
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test with multiple projects
5. Submit a pull request

## 📝 License

This project is part of the FuzeInfra ecosystem. See the main repository for license information.

---

## 💡 Tips

- **Use descriptive project names** for easier identification
- **Keep projects in the `projects/` directory** for automatic path resolution
- **Test with simple projects first** before deploying complex applications
- **Monitor nginx logs** for debugging proxy issues
- **Use health check endpoints** in your applications for better monitoring

**Happy coding with zero port conflicts! 🎉** 