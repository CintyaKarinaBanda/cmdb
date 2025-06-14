# Servicios/cloudtrail_functions.py
import json
from datetime import datetime, timedelta
from services.utils import create_aws_client, get_db_connection

# Eventos importantes que queremos rastrear
IMPORTANT_EC2_EVENTS = {
    "StartInstances", "StopInstances", "RebootInstances", "TerminateInstances", 
    "ModifyInstanceAttribute", "CreateTags", "DeleteTags", "RunInstances", 
    "AttachVolume", "DetachVolume"
}

IMPORTANT_RDS_EVENTS = {
    "CreateDBInstance", "DeleteDBInstance", "ModifyDBInstance", "RebootDBInstance",
    "StartDBInstance", "StopDBInstance", "RestoreDBInstanceFromDBSnapshot",
    "CreateDBSnapshot", "DeleteDBSnapshot", "AddTagsToResource", "RemoveTagsFromResource"
}

IMPORTANT_VPC_EVENTS = {
    "CreateVpc", "DeleteVpc", "ModifyVpcAttribute", "CreateSubnet", "DeleteSubnet", 
    "ModifySubnetAttribute", "CreateRouteTable", "DeleteRouteTable", "CreateRoute", 
    "DeleteRoute", "CreateInternetGateway", "DeleteInternetGateway", "AttachInternetGateway", 
    "DetachInternetGateway", "CreateNatGateway", "DeleteNatGateway"
}

def extract_resource_id(event, resource_type):
    """Extrae el ID del recurso del evento según el tipo."""
    req = event.get("requestParameters", {})
    res = event.get("responseElements", {})
    event_name = event.get("eventName", "")
    
    # Para recursos EC2
    if resource_type == "EC2":
        # Buscar en lugares específicos según el tipo de evento
        if event_name in ["StartInstances", "StopInstances", "RebootInstances", "TerminateInstances"]:
            instances = req.get("instancesSet", {}).get("items", [])
            if instances and len(instances) > 0:
                return instances[0].get("instanceId", "unknown")
        
        if event_name == "ModifyInstanceAttribute":
            return req.get("instanceId", "unknown")
        
        # Buscar en campos comunes
        for key in ["instanceId", "resourceId"]:
            if key in req and req[key]:
                return req[key]
    
    # Para recursos RDS
    elif resource_type == "RDS":
        # Buscar en campos específicos de RDS
        if "dBInstanceIdentifier" in req:
            return req["dBInstanceIdentifier"]
        
        if "dBInstanceIdentifier" in res:
            return res["dBInstanceIdentifier"]
        
        # Para eventos de snapshot
        if "dBSnapshotIdentifier" in req:
            return req["dBSnapshotIdentifier"]
        
        # Para eventos de tags
        if event_name in ["AddTagsToResource", "RemoveTagsFromResource"]:
            resource_arn = req.get("resourceName")
            if resource_arn and "rds:db:" in resource_arn:
                parts = resource_arn.split(":")
                if len(parts) > 6:
                    return parts[6]
    
    # Para recursos VPC
    elif resource_type == "VPC":
        # Buscar en campos específicos de VPC
        if "vpcId" in req:
            return req["vpcId"]
        
        if "vpc" in res and "vpcId" in res["vpc"]:
            return res["vpc"]["vpcId"]
        
        # Para eventos de subnet
        if "subnetId" in req:
            return req["subnetId"]
        
        if "subnet" in res and "subnetId" in res["subnet"]:
            return res["subnet"]["subnetId"]
        
        # Para eventos de internet gateway
        if "internetGatewayId" in req:
            return req["internetGatewayId"]
        
        if "internetGateway" in res and "internetGatewayId" in res["internetGateway"]:
            return res["internetGateway"]["internetGatewayId"]
        
        # Para eventos de NAT gateway
        if "natGatewayId" in req:
            return req["natGatewayId"]
        
        if "natGateway" in res and "natGatewayId" in res["natGateway"]:
            return res["natGateway"]["natGatewayId"]
    
    return "unknown"

def extract_changes(event, resource_type):
    """Extrae cambios relevantes del evento."""
    event_name = event.get("eventName", "")
    req = event.get("requestParameters", {})
    res = event.get("responseElements", {})
    
    changes = {"eventType": event_name, "details": {}}
    
    # Para recursos EC2
    if resource_type == "EC2":
        if event_name in ["StartInstances", "StopInstances", "RebootInstances", "TerminateInstances"]:
            instances = res.get("instancesSet", {}).get("items", [])
            if instances:
                changes["details"]["state"] = instances[0].get("currentState", {}).get("name")
        
        elif event_name == "ModifyInstanceAttribute":
            for key, value in req.items():
                if key not in ["instanceId", "attribute", "value"]:
                    changes["details"][key] = value
    
    # Para recursos RDS
    elif resource_type == "RDS":
        if event_name == "CreateDBInstance":
            changes["details"].update({
                "engine": req.get("engine"),
                "dbInstanceClass": req.get("dbInstanceClass"),
                "allocatedStorage": req.get("allocatedStorage"),
                "multiAZ": req.get("multiAZ")
            })
        
        elif event_name == "ModifyDBInstance":
            for key in ["dbInstanceClass", "allocatedStorage", "multiAZ", "engineVersion"]:
                if key in req:
                    changes["details"][key] = req[key]
        
        elif event_name in ["StartDBInstance", "StopDBInstance", "RebootDBInstance"]:
            changes["details"]["action"] = event_name.replace("DBInstance", "")
        
        elif event_name in ["AddTagsToResource", "RemoveTagsFromResource"]:
            if "tags" in req:
                changes["details"]["tags"] = req["tags"]
    
    # Para recursos VPC
    elif resource_type == "VPC":
        if event_name == "CreateVpc":
            changes["details"].update({
                "cidrBlock": req.get("cidrBlock"),
                "instanceTenancy": req.get("instanceTenancy", "default")
            })
            if "vpc" in res:
                changes["details"]["vpcId"] = res["vpc"].get("vpcId")
        
        elif event_name == "ModifyVpcAttribute":
            for key, value in req.items():
                if key not in ["vpcId", "attribute"]:
                    changes["details"][key] = value
        
        elif event_name == "CreateSubnet":
            changes["details"].update({
                "vpcId": req.get("vpcId"),
                "cidrBlock": req.get("cidrBlock"),
                "availabilityZone": req.get("availabilityZone")
            })
            if "subnet" in res:
                changes["details"]["subnetId"] = res["subnet"].get("subnetId")
        
        elif event_name == "CreateInternetGateway":
            if "internetGateway" in res:
                changes["details"]["internetGatewayId"] = res["internetGateway"].get("internetGatewayId")
        
        elif event_name in ["AttachInternetGateway", "DetachInternetGateway"]:
            changes["details"].update({
                "vpcId": req.get("vpcId"),
                "internetGatewayId": req.get("internetGatewayId")
            })
        
        elif event_name == "CreateNatGateway":
            changes["details"].update({
                "subnetId": req.get("subnetId"),
                "allocationId": req.get("allocationId")
            })
            if "natGateway" in res:
                changes["details"]["natGatewayId"] = res["natGateway"].get("natGatewayId")
    
    return changes

def get_ec2_cloudtrail_events(region, credentials):
    """Obtiene eventos de CloudTrail relacionados con EC2."""
    return get_cloudtrail_events(region, credentials, "ec2.amazonaws.com", IMPORTANT_EC2_EVENTS, "EC2")

def get_rds_cloudtrail_events(region, credentials):
    """Obtiene eventos de CloudTrail relacionados con RDS."""
    return get_cloudtrail_events(region, credentials, "rds.amazonaws.com", IMPORTANT_RDS_EVENTS, "RDS")

def get_vpc_cloudtrail_events(region, credentials):
    """Obtiene eventos de CloudTrail relacionados con VPC."""
    return get_cloudtrail_events(region, credentials, "ec2.amazonaws.com", IMPORTANT_VPC_EVENTS, "VPC")

def get_cloudtrail_events(region, credentials, event_source, important_events, resource_type):
    """Obtiene eventos de CloudTrail según el tipo de recurso."""
    try:
        client = create_aws_client("cloudtrail", region, credentials)
        if not client:
            return {"error": f"Error al crear cliente CloudTrail para {resource_type}", "events": []}

        start_time = datetime.utcnow() - timedelta(days=3)
        response = client.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": event_source}],
            StartTime=start_time,
            EndTime=datetime.utcnow(),
            MaxResults=100
        )
        
        events = response.get("Events", [])
        
        parsed_events = []
        for raw_event in events:
            try:
                detail = json.loads(raw_event.get("CloudTrailEvent", "{}"))
                event_name = detail.get("eventName")
                
                if event_name not in important_events:
                    continue
                
                # Obtener usuario
                user_identity = detail.get("userIdentity", {})
                user_name = user_identity.get("userName") or user_identity.get("principalId") or "unknown"
                
                # Obtener recurso afectado
                resource_name = extract_resource_id(detail, resource_type)
                
                # Extraer cambios
                changes = extract_changes(detail, resource_type)
                
                parsed_event = {
                    "event_id": raw_event.get("EventId"),
                    "event_time": raw_event.get("EventTime"),
                    "event_name": event_name,
                    "event_source": detail.get("eventSource", "unknown"),
                    "user_name": user_name,
                    "resource_name": resource_name,
                    "resource_type": resource_type,
                    "changes": changes,
                    "region": region
                }
                
                parsed_events.append(parsed_event)
                
            except Exception as e:
                print(f"[ERROR] Evento {resource_type}: {str(e)}")
        
        return {"events": parsed_events}
        
    except Exception as e:
        print(f"[ERROR] CloudTrail {resource_type}: {str(e)}")
        return {"error": str(e), "events": []}

def insert_or_update_cloudtrail_events(events):
    """Inserta eventos de CloudTrail en la base de datos."""
    if not events:
        return {"inserted": 0, "updated": 0}
    
    conn = get_db_connection()
    if not conn:
        return {"error": "No se pudo conectar a la base de datos"}
    
    cursor = conn.cursor()
    inserted = 0
    
    try:
        # Verificar si la tabla existe
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'cloudtrail_events'
            )
        """)
        table_exists = cursor.fetchone()[0]
        
        # Crear tabla si no existe
        if not table_exists:
            cursor.execute("""
                CREATE TABLE cloudtrail_events (
                    id SERIAL PRIMARY KEY,
                    event_id VARCHAR(255) UNIQUE,
                    event_time TIMESTAMP,
                    event_name VARCHAR(255),
                    event_source VARCHAR(255),
                    user_name VARCHAR(255),
                    resource_name VARCHAR(255),
                    resource_type VARCHAR(50),
                    region VARCHAR(50),
                    changes JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        
        # Insertar eventos
        for event in events:
            try:
                cursor.execute(
                    """
                    INSERT INTO cloudtrail_events 
                    (event_id, event_time, event_name, event_source, user_name, resource_name, 
                     resource_type, region, changes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        event["event_id"],
                        event["event_time"],
                        event["event_name"],
                        event["event_source"],
                        event["user_name"],
                        event["resource_name"],
                        event.get("resource_type", "EC2"), 
                        event["region"],
                        json.dumps(event["changes"])
                    )
                )
                inserted += 1
            except Exception as e:
                print(f"[ERROR] DB: evento_id={event.get('event_id')} - {str(e)}")
        
        conn.commit()
        return {"inserted": inserted, "updated": 0}
        
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] DB: {str(e)}")
        return {"error": str(e), "inserted": 0, "updated": 0}
        
    finally:
        cursor.close()
        conn.close()