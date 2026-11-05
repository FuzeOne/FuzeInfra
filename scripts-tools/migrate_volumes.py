#!/usr/bin/env python3
"""
Volume Migration Script for FuzeInfra
Migrates data from old shared-* volumes to new fuzeinfra_* volumes
"""

import subprocess
import shlex

def run_command(cmd, description=""):
    """Run a command and return the result"""
    print(f"🔄 {description}")
    print(f"   Command: {shlex.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout:
            print(f"   ✅ {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Error: {e.stderr.strip()}")
        return False

def volume_exists(volume_name):
    """Check if a volume exists"""
    try:
        subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            text=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False

def migrate_volume_data(source_vol, target_vol):
    """Migrate data from source volume to target volume"""
    if not volume_exists(source_vol):
        print(f"   ⚠️  Source volume {source_vol} doesn't exist, skipping...")
        return True
    
    # Create target volume if it doesn't exist
    if not volume_exists(target_vol):
        if not run_command(["docker", "volume", "create", target_vol], f"Creating target volume {target_vol}"):
            return False
    
    # Use a temporary container to copy data
    copy_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{source_vol}:/source",
        "-v",
        f"{target_vol}:/target",
        "alpine",
        "cp",
        "-a",
        "/source/.",
        "/target/",
    ]
    
    return run_command(copy_cmd, f"Copying data from {source_vol} to {target_vol}")

def main():
    print("🚀 FuzeInfra Volume Migration Script")
    print("=" * 50)
    
    # Volume mappings: old_name -> new_name
    volume_mappings = {
        "shared-postgres-data": "fuzeinfra_postgres_data",
        "shared-redis-data": "fuzeinfra_redis_data", 
        "shared-mongodb-data": "fuzeinfra_mongodb_data",
        "shared-neo4j-data": "fuzeinfra_neo4j_data",
        "shared-neo4j-logs": "fuzeinfra_neo4j_logs",
        "shared-elasticsearch-data": "fuzeinfra_elasticsearch_data",
        "shared-kafka-data": "fuzeinfra_kafka_data",
        "shared-zookeeper-data": "fuzeinfra_zookeeper_data",
        "shared-zookeeper-logs": "fuzeinfra_zookeeper_logs",
        "shared-rabbitmq-data": "fuzeinfra_rabbitmq_data",
        "shared-prometheus-data": "fuzeinfra_prometheus_data",
        "shared-alertmanager-data": "fuzeinfra_alertmanager_data",
        "shared-grafana-data": "fuzeinfra_grafana_data",
        "shared-loki-data": "fuzeinfra_loki_data"
    }
    
    print(f"📋 Planning to migrate {len(volume_mappings)} volumes...")
    
    # Show what will be migrated
    for old_vol, new_vol in volume_mappings.items():
        exists = "✅" if volume_exists(old_vol) else "❌"
        print(f"   {exists} {old_vol} -> {new_vol}")
    
    print("\n🔄 Starting migration...")
    
    success_count = 0
    for old_vol, new_vol in volume_mappings.items():
        print(f"\n📦 Migrating {old_vol} -> {new_vol}")
        if migrate_volume_data(old_vol, new_vol):
            success_count += 1
        else:
            print(f"   ❌ Failed to migrate {old_vol}")
    
    print(f"\n✅ Migration completed!")
    print(f"   Successfully migrated: {success_count}/{len(volume_mappings)} volumes")
    
    if success_count == len(volume_mappings):
        print("\n🎉 All volumes migrated successfully!")
        print("   You can now start FuzeInfra with the new volume names.")
    else:
        print(f"\n⚠️  Some migrations failed. Please check the errors above.")

if __name__ == "__main__":
    main() 