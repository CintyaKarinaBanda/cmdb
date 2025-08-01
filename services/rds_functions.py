from botocore.exceptions import ClientError
from datetime import datetime
import time
from services.utils import create_aws_client, get_db_connection, log_change

FIELD_EVENT_MAP = {
    "dbname": ["CreateDBInstance", "ModifyDBInstance"],
    "enginetype": ["CreateDBInstance"],
    "engineversion": ["ModifyDBInstance"],
    "storagesize": ["ModifyDBInstance"],
    "instancetype": ["ModifyDBInstance"],
    "status": ["StartDBInstance", "StopDBInstance", "RebootDBInstance", "CreateDBInstance", "DeleteDBInstance"],
    "endpoint": ["CreateDBInstance", "ModifyDBInstance"],
    "port": ["CreateDBInstance", "ModifyDBInstance"],
    "hasreplica": ["CreateDBInstanceReadReplica", "DeleteDBInstance"]
}

def normalize_list_comparison(old_val, new_val):
    """Normaliza listas para comparación, ignorando orden"""
    if isinstance(new_val, list) and isinstance(old_val, (list, str)):
        old_list = old_val if isinstance(old_val, list) else str(old_val).split(',') if old_val else []
        return sorted([str(x).strip() for x in old_list]) == sorted([str(x).strip() for x in new_val])
    return str(old_val) == str(new_val)

def get_instance_changed_by(instance_id, field_name):
    """Busca el usuario que cambió un campo específico"""
    conn = get_db_connection()
    if not conn:
        return "unknown"
    
    try:
        with conn.cursor() as cursor:
            possible_events = FIELD_EVENT_MAP.get(field_name, [])
            
            if possible_events:
                placeholders = ','.join(['%s'] * len(possible_events))
                query = f"""
                    SELECT user_name FROM cloudtrail_events
                    WHERE resource_name = %s AND resource_type = 'RDS'
                    AND event_name IN ({placeholders})
                    ORDER BY event_time DESC LIMIT 1
                """
                cursor.execute(query, (instance_id, *possible_events))
            else:
                cursor.execute("""
                    SELECT user_name FROM cloudtrail_events
                    WHERE resource_name = %s AND resource_type = 'RDS'
                    ORDER BY event_time DESC LIMIT 1
                """, (instance_id,))
            
            if result := cursor.fetchone():
                return result[0]
            return "unknown"
    except Exception as e:
        pass
        return "unknown"
    finally:
        conn.close()

def extract_rds_data(db, account_name, account_id, region):
    endpoint = db.get("Endpoint", {})
    return {
        "AccountName": account_name,
        "AccountID": account_id,
        "DbInstanceId": db["DBInstanceIdentifier"],
        "DbName": db.get("DBName", "N/A"),
        "EngineType": db["Engine"],
        "EngineVersion": db.get("EngineVersion", "N/A"),
        "StorageSize": db.get("AllocatedStorage", "N/A"),
        "InstanceType": db["DBInstanceClass"],
        "Status": db["DBInstanceStatus"],
        "Region": region,
        "Endpoint": endpoint.get("Address", "N/A"),
        "Port": endpoint.get("Port", "N/A"),
        "HasReplica": bool(db.get("ReadReplicaDBInstanceIdentifiers"))
    }

def get_rds_instances(region, credentials, account_id, account_name):
    rds_client = create_aws_client("rds", region, credentials)
    if not rds_client:
        return []

    try:
        paginator = rds_client.get_paginator('describe_db_instances')
        instances_info = []

        for page in paginator.paginate():
            for db in page.get("DBInstances", []):
                info = extract_rds_data(db, account_name, account_id, region)
                instances_info.append(info)
        return instances_info
    except ClientError as e:
        return []

def insert_or_update_rds_data(rds_data):
    if not rds_data:
        return {"processed": 0, "inserted": 0, "updated": 0}

    conn = get_db_connection()
    if not conn:
        return {"error": "DB connection failed", "processed": 0, "inserted": 0, "updated": 0}

    query_insert = """
        INSERT INTO rds (
            AccountName, AccountID, DbInstanceId, DbName, EngineType,
            EngineVersion, StorageSize, InstanceType, Status, Region,
            Endpoint, Port, HasReplica, last_updated
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, CURRENT_TIMESTAMP
        )
    """



    inserted = 0
    updated = 0
    processed = 0

    try:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM rds")
        columns = [desc[0].lower() for desc in cursor.description]
        existing_data = {(row[columns.index("dbinstanceid")], row[columns.index("accountid")]): dict(zip(columns, row)) for row in cursor.fetchall()}

        for rds in rds_data:
            instance_id = rds["DbInstanceId"]
            processed += 1

            insert_values = (
                rds["AccountName"], rds["AccountID"], rds["DbInstanceId"],
                rds["DbName"], rds["EngineType"], rds["EngineVersion"],
                rds["StorageSize"], rds["InstanceType"], rds["Status"],
                rds["Region"], rds["Endpoint"], rds["Port"],
                rds["HasReplica"]
            )

            if (instance_id, rds["AccountID"]) not in existing_data:
                cursor.execute(query_insert.replace('CURRENT_TIMESTAMP', 'NOW()'), insert_values)
                inserted += 1
            else:
                # Siempre actualizar last_updated para registros existentes
                cursor.execute("""
                    UPDATE rds SET last_updated = NOW() WHERE dbinstanceid = %s AND accountid = %s
                """, (instance_id, rds["AccountID"]))
                updated += 1

        conn.commit()
        return {
            "processed": processed,
            "inserted": inserted,
            "updated": updated
        }

    except Exception as e:
        conn.rollback()
        pass
        return {"error": str(e), "processed": 0, "inserted": 0, "updated": 0}
    finally:
        conn.close()