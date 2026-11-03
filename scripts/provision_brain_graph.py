#!/usr/bin/env python3
"""
provision_brain_graph.py
FuzeInfra utility to provision per-brain Knowledge Graph schema and constraints in Neo4j.
"""

import os
import sys
import logging
from typing import Dict, Any

try:
    from neo4j import GraphDatabase
except ImportError:
    print("neo4j driver required: pip install neo4j")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FuzeInfra.Neo4jProvisioner")

NEO4J_URL = os.environ.get("NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

CONSTRAINTS = [
    "CREATE CONSTRAINT brain_id_unique IF NOT EXISTS FOR (b:Brain) REQUIRE b.id IS UNIQUE",
    "CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT entity_name_unique IF NOT EXISTS FOR (e:Entity) REQUIRE (e.brain_id, e.name) IS UNIQUE",
    "CREATE INDEX brain_scope_idx IF NOT EXISTS FOR (b:Brain) ON (b.scope)",
    "CREATE INDEX entity_type_idx IF NOT EXISTS FOR (e:Entity) ON (e.type)"
]

def provision_brain_graph(brain_id: str, scope: str = "agent", organization_id: str = "org_default") -> Dict[str, Any]:
    driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        # 1. Apply global graph constraints
        for query in CONSTRAINTS:
            try:
                session.run(query)
            except Exception as e:
                logger.warning(f"Constraint warning (might exist): {e}")

        # 2. Seed Brain node
        init_brain_cypher = """
        MERGE (b:Brain {id: $brain_id})
        ON CREATE SET
            b.scope = $scope,
            b.organization_id = $organization_id,
            b.created_at = datetime(),
            b.updated_at = datetime(),
            b.entity_count = 0,
            b.relation_count = 0
        ON MATCH SET
            b.updated_at = datetime()
        RETURN b.id AS id, b.scope AS scope
        """
        session.run(init_brain_cypher, brain_id=brain_id, scope=scope, organization_id=organization_id).single()
        logger.info(f"✅ Provisioned Neo4j graph partition for Brain [{brain_id}] (scope={scope})")

    driver.close()
    return {"brain_id": brain_id, "scope": scope, "status": "provisioned"}

if __name__ == "__main__":
    b_id = sys.argv[1] if len(sys.argv) > 1 else "brain_org_global"
    sc = sys.argv[2] if len(sys.argv) > 2 else "organization"
    provision_brain_graph(b_id, sc)
