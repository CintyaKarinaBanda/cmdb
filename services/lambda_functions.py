from botocore.exceptions import ClientError
from datetime import datetime
import time
from services.utils import create_aws_client, get_db_connection, log_change

def get_local_time():
    return 'NOW()'

FIELD_EVENT_MAP = {
    "functionname": ["CreateFunction", "UpdateFunctionConfiguration"],
    "description": ["UpdateFunctionConfiguration"],
    "handler": ["UpdateFunctionConfiguration"],
    "runtime": ["UpdateFunctionConfiguration"],
    "memorysize": ["UpdateFunctionConfiguration"],
    "timeout": ["UpdateFunctionConfiguration"],
    "role": ["UpdateFunctionConfiguration"],
    "environment": ["UpdateFunctionConfiguration"],
    "vpcconfig": ["UpdateFunctionConfiguration"],
    "tags": ["TagResource", "UntagResource"]
}

def normalize_list_comparison(old_val, new_val):
    """Normaliza listas para comparación, ignorando orden"""
    if isinstance(new_val, list) and isinstance(old_val, (list, str)):
        old_list = old_val if isinstance(old_val, list) else str(old_val).split(',') if old_val else []
        return sorted([str(x).strip() for x in old_list]) == sorted([str(x).strip() for x in new_val])
    return str(old_val) == str(new_val)

def get_function_changed_by(function_name, field_name):
    conn = get_db_connection()
    if not conn:
        return "unknown"
    try:
        with conn.cursor() as cursor:
            events = FIELD_EVENT_MAP.get(field_name, [])
            if events:
                placeholders = ','.join(['%s'] * len(events))
                cursor.execute(f"SELECT user_name FROM cloudtrail_events WHERE resource_name = %s AND resource_type = 'LAMBDA' AND event_name IN ({placeholders}) ORDER BY event_time DESC LIMIT 1", (function_name, *events))
            else:
                cursor.execute("SELECT user_name FROM cloudtrail_events WHERE resource_name = %s AND resource_type = 'LAMBDA' ORDER BY event_time DESC LIMIT 1", (function_name,))
            return cursor.fetchone()[0] if cursor.fetchone() else "unknown"
    except:
        return "unknown"
    finally:
        conn.close()

def extract_lambda_data(function, lambda_client, account_name, account_id, region):
    function_name = function["FunctionName"]
    
    # Get function configuration
    try:
        config = lambda_client.get_function_configuration(FunctionName=function_name)
        vpc_config = config.get("VpcConfig", {})
        vpc_info = f"VPC: {vpc_config.get('VpcId', 'N/A')}, Subnets: {len(vpc_config.get('SubnetIds', []))}" if vpc_config.get('VpcId') else "N/A"
        env_vars = len(config.get("Environment", {}).get("Variables", {}))
    except:
        config = function
        vpc_info = "N/A"
        env_vars = 0
    
    # Get triggers
    try:
        triggers_response = lambda_client.list_event_source_mappings(FunctionName=function_name)
        triggers = len(triggers_response.get("EventSourceMappings", []))
    except:
        triggers = 0
    
    # Get tags
    try:
        tags_response = lambda_client.list_tags(Resource=function.get("FunctionArn", ""))
        tags = tags_response.get("Tags", {})
        get_tag = lambda key: tags.get(key, "N/A")
    except:
        tags = {}
        get_tag = lambda key: "N/A"
    
    return {
        "AccountName": account_name,
        "AccountID": account_id,
        "FunctionID": function.get("FunctionArn", "").split(":")[-1] if function.get("FunctionArn") else function_name,
        "FunctionName": function_name,
        "Description": config.get("Description", "N/A"),
        "Handler": config.get("Handler", "N/A"),
        "Runtime": config.get("Runtime", "N/A"),
        "MemorySize": config.get("MemorySize", 0),
        "Timeout": config.get("Timeout", 0),
        "Role": config.get("Role", "N/A").split("/")[-1] if config.get("Role") else "N/A",
        "Environment": env_vars,
        "Triggers": triggers,
        "VPCConfig": vpc_info,
        "Region": region,
        "Tags": tags
    }

def get_lambda_functions(region, credentials, account_id, account_name):
    lambda_client = create_aws_client("lambda", region, credentials)
    if not lambda_client:
        return []
    try:
        functions_info = []
        for page in lambda_client.get_paginator('list_functions').paginate():
            for function in page.get("Functions", []):
                try:
                    functions_info.append(extract_lambda_data(function, lambda_client, account_name, account_id, region))
                except:
                    continue
        return functions_info
    except:
        return []

def insert_or_update_lambda_data(lambda_data):
    if not lambda_data:
        return {"processed": 0, "inserted": 0, "updated": 0}
    conn = get_db_connection()
    if not conn:
        return {"error": "DB connection failed", "processed": 0, "inserted": 0, "updated": 0}
    
    inserted = updated = processed = 0
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM lambda_functions")
        columns = [desc[0].lower() for desc in cursor.description]
        existing = {(row[columns.index("functionname")], row[columns.index("accountid")]): dict(zip(columns, row)) for row in cursor.fetchall()}
        
        for func in lambda_data:
            processed += 1
            function_name = func["FunctionName"]
            values = (func["AccountName"], func["AccountID"], func["FunctionID"], func["FunctionName"], func["Description"], func["Handler"], func["Runtime"], func["MemorySize"], func["Timeout"], func["Role"], func["Environment"], func["Triggers"], func["VPCConfig"], func["Region"], func["Tags"])
            
            if (function_name, func["AccountID"]) not in existing:
                cursor.execute("INSERT INTO lambda_functions (AccountName, AccountID, FunctionID, FunctionName, Description, Handler, Runtime, MemorySize, Timeout, Role, Environment, Triggers, VPCConfig, Region, Tags, last_updated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())", values)
                inserted += 1
            else:
                db_row = existing[(function_name, func["AccountID"])]
                updates = []
                vals = []
                campos = {"accountname": func["AccountName"], "accountid": func["AccountID"], "functionid": func["FunctionID"], "functionname": func["FunctionName"], "description": func["Description"], "handler": func["Handler"], "runtime": func["Runtime"], "memorysize": func["MemorySize"], "timeout": func["Timeout"], "role": func["Role"], "environment": func["Environment"], "triggers": func["Triggers"], "vpcconfig": func["VPCConfig"], "region": func["Region"], "tags": func["Tags"]}
                
                # Verificar si cambió el account_id o function_name (campos de identificación)
                if (str(db_row.get('accountid')) != str(func["AccountID"]) or 
                    str(db_row.get('functionname')) != str(func["FunctionName"])):
                    # Si cambió la identificación, insertar como nuevo registro
                    cursor.execute("INSERT INTO lambda_functions (AccountName, AccountID, FunctionID, FunctionName, Description, Handler, Runtime, MemorySize, Timeout, Role, Environment, Triggers, VPCConfig, Region, Tags, last_updated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())", values)
                    inserted += 1
                    continue
                
                for col, new_val in campos.items():
                    # Saltar campos de identificación para actualizaciones
                    if col in ['accountid', 'functionname']:
                        continue
                    
                    old_val = db_row.get(col)
                    if not normalize_list_comparison(old_val, new_val):
                        updates.append(f"{col} = %s")
                        vals.append(new_val)
                        log_change('LAMBDA', function_name, col, old_val, new_val, get_function_changed_by(function_name, col), func["AccountID"], func["Region"])
                
                if updates:
                    cursor.execute(f"UPDATE lambda_functions SET {', '.join(updates)}, last_updated = NOW() WHERE functionname = %s", vals + [function_name])
                    updated += 1
        
        conn.commit()
        return {"processed": processed, "inserted": inserted, "updated": updated}
    except Exception as e:
        conn.rollback()
        return {"error": str(e), "processed": 0, "inserted": 0, "updated": 0}
    finally:
        conn.close()