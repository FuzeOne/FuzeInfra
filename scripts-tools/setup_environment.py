#!/usr/bin/env python3
"""
Environment Setup Script for FuzeInfra Platform

This script helps users create and configure their .env file
from the environment.template file and sets up required databases.
"""

import shutil
import secrets
import string
import base64
import re
import subprocess
import time
from pathlib import Path

_POSTGRES_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def generate_secure_password(length=16):
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def generate_secret_key(length=32):
    """Generate a secure secret key."""
    return secrets.token_urlsafe(length)


def generate_fernet_key():
    """Generate a Fernet key for Airflow encryption."""
    # Generate 32 random bytes and encode as base64
    key = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(key).decode('utf-8')


def quote_postgres_identifier(value, setting_name):
    """Return a safely quoted PostgreSQL identifier."""
    if not _POSTGRES_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{setting_name} must start with a letter or underscore and contain only letters, numbers, and underscores."
        )
    return f'"{value}"'


def quote_postgres_literal(value):
    """Return a safely quoted PostgreSQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def run_docker_command(command, description=""):
    """Run a Docker command and return success status."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def check_postgres_container():
    """Check if PostgreSQL container is running."""
    success, output = run_docker_command([
        "docker",
        "ps",
        "--filter",
        "name=fuzeinfra-postgres",
        "--format",
        "{{.Names}}",
    ])
    return success and "fuzeinfra-postgres" in output


def setup_databases(non_interactive=False):
    """Setup required databases in PostgreSQL."""
    if not non_interactive:
        print("\n🗄️  Setting up databases...")
    
    # Check if PostgreSQL container is running
    if not check_postgres_container():
        if not non_interactive:
            print("⚠️  PostgreSQL container not running. Please start the infrastructure first.")
            print("   Run: docker-compose -f docker-compose.FuzeInfra.yml up -d postgres")
        return False
    
    # Wait a moment for PostgreSQL to be ready
    time.sleep(2)
    
    # Read environment variables to get database configuration
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        if not non_interactive:
            print("❌ .env file not found. Please run environment setup first.")
        return False
    
    # Parse .env file to get database settings
    env_vars = {}
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env_vars[key] = value
    
    postgres_user = env_vars.get('POSTGRES_USER', 'fuzeinfra')
    postgres_password = env_vars.get('POSTGRES_PASSWORD', 'fuzeinfra_secure_password')
    postgres_db = env_vars.get('POSTGRES_DB', 'fuzeinfra_db')
    try:
        postgres_user_ident = quote_postgres_identifier(postgres_user, "POSTGRES_USER")
    except ValueError as exc:
        if not non_interactive:
            print(f"❌ {exc}")
        return False

    postgres_user_literal = quote_postgres_literal(postgres_user)
    postgres_password_literal = quote_postgres_literal(postgres_password)
    
    databases_to_create = [
        {
            'name': postgres_db,
            'purpose': 'Main application database for FuzeInfra platform'
        },
        {
            'name': 'airflow',
            'purpose': 'Apache Airflow workflow orchestration database'
        }
    ]
    
    if not non_interactive:
        print(f"📋 Creating databases for user: {postgres_user}")
    
    # Create user if it doesn't exist
    create_user_sql = (
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = {postgres_user_literal}) THEN "
        f"CREATE USER {postgres_user_ident} WITH PASSWORD {postgres_password_literal}; "
        "END IF; "
        "END $$;"
    )
    run_docker_command([
        "docker",
        "exec",
        "fuzeinfra-postgres",
        "psql",
        "-U",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        create_user_sql,
    ])
    
    # Create databases
    for db_info in databases_to_create:
        db_name = db_info['name']
        purpose = db_info['purpose']
        
        if not non_interactive:
            print(f"   📦 Creating database: {db_name}")
            print(f"      Purpose: {purpose}")
        try:
            db_name_ident = quote_postgres_identifier(db_name, "database name")
        except ValueError as exc:
            if not non_interactive:
                print(f"      ❌ {exc}")
            return False
        db_name_literal = quote_postgres_literal(db_name)
        
        # Check if database exists using PostgreSQL query
        success, output = run_docker_command([
            "docker",
            "exec",
            "fuzeinfra-postgres",
            "psql",
            "-U",
            "postgres",
            "-t",
            "-c",
            f"SELECT 1 FROM pg_database WHERE datname = {db_name_literal}",
        ])
        db_exists = success and output.strip() == '1'
        
        if db_exists:
            if not non_interactive:
                print(f"      ✅ Database {db_name} already exists")
        else:
            # Create database
            success, output = run_docker_command([
                "docker",
                "exec",
                "fuzeinfra-postgres",
                "psql",
                "-U",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"CREATE DATABASE {db_name_ident} OWNER {postgres_user_ident};",
            ])
            
            if success:
                # Grant privileges
                run_docker_command([
                    "docker",
                    "exec",
                    "fuzeinfra-postgres",
                    "psql",
                    "-U",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f"GRANT ALL PRIVILEGES ON DATABASE {db_name_ident} TO {postgres_user_ident};",
                ])
                
                if not non_interactive:
                    print(f"      ✅ Database {db_name} created successfully")
            else:
                if not non_interactive:
                    print(f"      ❌ Failed to create database {db_name}: {output}")
                return False
    
    if not non_interactive:
        print("✅ Database setup completed!")
        print("\n📊 Database Summary:")
        print(f"   • {postgres_db}: Main application database")
        print(f"   • airflow: Workflow orchestration database")
        print(f"   • Owner: {postgres_user}")
    
    return True


def setup_environment(non_interactive=False):
    """Setup environment configuration file."""
    if not non_interactive:
        print("🔧 FuzeInfra Platform Environment Setup")
        print("=" * 50)
    
    project_root = Path(__file__).parent.parent
    template_file = project_root / "environment.template"
    env_file = project_root / ".env"
    
    # Check if template exists
    if not template_file.exists():
        print(f"❌ Template file not found: {template_file}")
        return False
    
    # Check if .env already exists
    if env_file.exists() and not non_interactive:
        response = input("⚠️  .env file already exists. Overwrite? (y/N): ")
        if response.lower() != 'y':
            print("✅ Setup cancelled. Existing .env file preserved.")
            return True
    
    try:
        # Copy template to .env
        shutil.copy2(template_file, env_file)
        if not non_interactive:
            print(f"✅ Created .env file from template")
        
        # Read the content
        with open(env_file, 'r') as f:
            content = f.read()
        
        # Ask user if they want to generate secure passwords (or auto-generate in non-interactive mode)
        if non_interactive:
            generate_passwords_flag = True
        else:
            generate_passwords = input("\n🔐 Generate secure passwords for services? (Y/n): ")
            generate_passwords_flag = generate_passwords.lower() != 'n'
            
        if generate_passwords_flag:
            if not non_interactive:
                print("🔄 Generating secure passwords...")
            
            # Replace default passwords with secure ones
            replacements = {
                'your_secure_postgres_password': generate_secure_password(),
                'your_secure_mongodb_password': generate_secure_password(),
                'your_secure_admin_ui_password': generate_secure_password(),
                'your_secure_redis_password': generate_secure_password(),
                'your_secure_mariadb_password': generate_secure_password(),
                'your_secure_neo4j_password': generate_secure_password(),
                'your_secure_rabbitmq_password': generate_secure_password(),
                'your_secure_grafana_password': generate_secure_password(),
                'your_secure_airflow_password': generate_secure_password(),
                'your_secure_fernet_key_here': generate_fernet_key(),
                'your_jwt_secret_key_here_change_in_production': generate_secret_key(),
                'your_encryption_key_here_change_in_production': generate_secret_key()
            }
            
            for old_value, new_value in replacements.items():
                content = content.replace(old_value, new_value)
        
        # Project customization (use defaults in non-interactive mode)
        if not non_interactive:
            print("\n📝 Project Customization")
            project_name = input("Enter project name (default: fuzeinfra): ").strip()
            if project_name:
                content = content.replace('COMPOSE_PROJECT_NAME=fuzeinfra', f'COMPOSE_PROJECT_NAME={project_name}')
            
            # Ask for database name
            db_name = input("Enter database name (default: fuzeinfra_db): ").strip()
            if db_name:
                content = content.replace('POSTGRES_DB=fuzeinfra_db', f'POSTGRES_DB={db_name}')
        
        # Write the updated content back
        with open(env_file, 'w') as f:
            f.write(content)
        
        if not non_interactive:
            print(f"\n✅ Environment file created successfully: {env_file}")
            print("\n📋 Next Steps:")
            print("1. Review and customize the .env file as needed")
            print("2. Start the infrastructure: docker-compose -f docker-compose.FuzeInfra.yml up -d")
            print("3. Run database setup: python scripts-tools/setup_environment.py --setup-databases")
            print("4. Access services using the URLs in the .env file")
            
            print("\n⚠️  SECURITY REMINDER:")
            print("- Never commit the .env file to version control")
            print("- Keep your passwords secure")
            print("- Change default passwords in production")
        
        return True
        
    except Exception as e:
        print(f"❌ Error setting up environment: {e}")
        return False


def show_help():
    """Show help information."""
    print("Environment Setup Script for FuzeInfra Platform")
    print("")
    print("Usage:")
    print("  python setup_environment.py [options]")
    print("")
    print("Options:")
    print("  --help, -h              Show this help message")
    print("  --non-interactive       Run without user prompts")
    print("  --setup-databases       Setup required databases in PostgreSQL")
    print("  --setup-only-databases  Only setup databases (skip environment file)")
    print("")
    print("This script will:")
    print("1. Copy environment.template to .env")
    print("2. Generate secure passwords for services")
    print("3. Allow customization of project settings")
    print("4. Setup required databases in PostgreSQL")
    print("")
    print("Database Information:")
    print("• fuzeinfra_db: Main application database for your platform")
    print("• airflow: Apache Airflow workflow orchestration database")
    print("")


def main():
    """Main function."""
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] in ['--help', '-h']:
        show_help()
        return
    
    # Check for flags
    non_interactive = '--non-interactive' in sys.argv
    setup_databases_only = '--setup-only-databases' in sys.argv
    setup_databases_flag = '--setup-databases' in sys.argv or setup_databases_only
    
    if setup_databases_only:
        # Only setup databases
        success = setup_databases(non_interactive=non_interactive)
        sys.exit(0 if success else 1)
    
    # Setup environment file
    success = setup_environment(non_interactive=non_interactive)
    if not success:
        sys.exit(1)
    
    # Setup databases if requested
    if setup_databases_flag:
        success = setup_databases(non_interactive=non_interactive)
        sys.exit(0 if success else 1)
    
    sys.exit(0)


if __name__ == "__main__":
    main() 